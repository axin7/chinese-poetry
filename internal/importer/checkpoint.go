package importer

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"syscall"
)

type Key struct {
	Dataset  string
	RowID    int64
	RawIndex int
}

type Progress struct {
	Generation        string
	SourceFingerprint string
	Scope             []string
	LastKey           *Key
	Indexed           int64
	Completed         bool
}

type CheckpointStore struct {
	path              string
	generation        string
	sourceFingerprint string
}

type checkpointJSON struct {
	Generation        string            `json:"generation"`
	SourceFingerprint string            `json:"source_fingerprint"`
	Scope             []string          `json:"scope"`
	LastKey           []json.RawMessage `json:"last_key"`
	Indexed           int64             `json:"indexed"`
	Completed         bool              `json:"completed"`
}

func NewCheckpointStore(path, generation, sourceFingerprint string) *CheckpointStore {
	return &CheckpointStore{
		path: path, generation: generation, sourceFingerprint: sourceFingerprint,
	}
}

func (s *CheckpointStore) Load(reset bool, scope []string) (Progress, error) {
	fresh := Progress{
		Generation: s.generation, SourceFingerprint: s.sourceFingerprint,
		Scope: cloneStrings(scope),
	}
	if reset {
		return fresh, nil
	}
	data, err := os.ReadFile(s.path)
	if os.IsNotExist(err) {
		return fresh, nil
	}
	if err != nil {
		return Progress{}, fmt.Errorf("read checkpoint: %w", err)
	}
	return s.decode(data, scope)
}

func (s *CheckpointStore) decode(data []byte, scope []string) (Progress, error) {
	var payload checkpointJSON
	if err := json.Unmarshal(data, &payload); err != nil {
		return Progress{}, fmt.Errorf("decode checkpoint: %w", err)
	}
	if payload.Generation != s.generation {
		return Progress{}, fmt.Errorf("checkpoint generation does not match")
	}
	if payload.SourceFingerprint != s.sourceFingerprint {
		return Progress{}, fmt.Errorf("checkpoint source fingerprint does not match")
	}
	if !equalStrings(payload.Scope, scope) {
		return Progress{}, fmt.Errorf("checkpoint dataset scope does not match")
	}
	if payload.Indexed < 0 {
		return Progress{}, fmt.Errorf("checkpoint indexed count is invalid")
	}
	key, err := decodeKey(payload.LastKey)
	if err != nil {
		return Progress{}, err
	}
	return Progress{
		Generation: payload.Generation, SourceFingerprint: payload.SourceFingerprint,
		Scope: cloneStrings(payload.Scope), LastKey: key,
		Indexed: payload.Indexed, Completed: payload.Completed,
	}, nil
}

func (s *CheckpointStore) Save(progress Progress) error {
	if err := validateProgress(progress, s.generation); err != nil {
		return err
	}
	payload := map[string]any{
		"generation":         progress.Generation,
		"source_fingerprint": progress.SourceFingerprint,
		"scope":              progress.Scope,
		"last_key":           encodeKey(progress.LastKey),
		"indexed":            progress.Indexed,
		"completed":          progress.Completed,
	}
	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return fmt.Errorf("encode checkpoint: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return fmt.Errorf("create checkpoint directory: %w", err)
	}
	temporary, err := writeTemporary(filepath.Dir(s.path), filepath.Base(s.path), data)
	if err != nil {
		return err
	}
	defer os.Remove(temporary)
	if err := os.Rename(temporary, s.path); err != nil {
		return fmt.Errorf("replace checkpoint: %w", err)
	}
	return syncDirectory(filepath.Dir(s.path))
}

func (s *CheckpointStore) Lock() (func(), error) {
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return nil, fmt.Errorf("create checkpoint directory: %w", err)
	}
	file, err := os.OpenFile(s.path+".lock", os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open checkpoint lock: %w", err)
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		return nil, fmt.Errorf("checkpoint is locked by another importer: %w", err)
	}
	return func() {
		_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
		_ = file.Close()
	}, nil
}

func writeTemporary(directory, name string, data []byte) (string, error) {
	file, err := os.CreateTemp(directory, name+".*.tmp")
	if err != nil {
		return "", fmt.Errorf("create checkpoint file: %w", err)
	}
	path := file.Name()
	defer func() {
		if file != nil {
			_ = file.Close()
			_ = os.Remove(path)
		}
	}()
	if err := file.Chmod(0o644); err != nil {
		return path, fmt.Errorf("set checkpoint permissions: %w", err)
	}
	if _, err := file.Write(append(data, '\n')); err != nil {
		return path, fmt.Errorf("write checkpoint: %w", err)
	}
	if err := file.Sync(); err != nil {
		return path, fmt.Errorf("sync checkpoint: %w", err)
	}
	if err := file.Close(); err != nil {
		return path, fmt.Errorf("close checkpoint: %w", err)
	}
	file = nil
	return path, nil
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open checkpoint directory: %w", err)
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil && err != syscall.EINVAL {
		return fmt.Errorf("sync checkpoint directory: %w", err)
	}
	return nil
}

func decodeKey(raw []json.RawMessage) (*Key, error) {
	if raw == nil {
		return nil, nil
	}
	if len(raw) != 3 {
		return nil, fmt.Errorf("checkpoint last_key is invalid")
	}
	var key Key
	if err := json.Unmarshal(raw[0], &key.Dataset); err != nil {
		return nil, fmt.Errorf("checkpoint last_key is invalid")
	}
	if err := json.Unmarshal(raw[1], &key.RowID); err != nil {
		return nil, fmt.Errorf("checkpoint last_key is invalid")
	}
	if err := json.Unmarshal(raw[2], &key.RawIndex); err != nil {
		return nil, fmt.Errorf("checkpoint last_key is invalid")
	}
	if key.Dataset == "" || key.RowID <= 0 || key.RawIndex < 0 {
		return nil, fmt.Errorf("checkpoint last_key is invalid")
	}
	return &key, nil
}

func encodeKey(key *Key) any {
	if key == nil {
		return nil
	}
	return []any{key.Dataset, key.RowID, key.RawIndex}
}

func validateProgress(progress Progress, generation string) error {
	if progress.Generation != generation || progress.SourceFingerprint == "" ||
		progress.Indexed < 0 || len(progress.Scope) == 0 {
		return fmt.Errorf("checkpoint progress is invalid")
	}
	if progress.Completed && progress.LastKey == nil && progress.Indexed != 0 {
		return fmt.Errorf("checkpoint progress is invalid")
	}
	if progress.LastKey != nil {
		key := progress.LastKey
		if key.Dataset == "" || key.RowID <= 0 || key.RawIndex < 0 {
			return fmt.Errorf("checkpoint progress is invalid")
		}
	}
	return nil
}

func equalStrings(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func cloneStrings(values []string) []string {
	return append([]string(nil), values...)
}
