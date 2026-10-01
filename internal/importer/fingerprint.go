package importer

import (
	"crypto/sha256"
	"fmt"
	"hash"
	"io"
	"os"
)

func SourceFingerprint(databasePath, datasetsPath string) (string, error) {
	digest := sha256.New()
	for _, source := range []struct {
		label string
		path  string
	}{
		{label: "database", path: databasePath},
		{label: "database-wal", path: databasePath + "-wal"},
		{label: "datasets", path: datasetsPath},
	} {
		optional := source.label == "database-wal"
		if err := hashStableFile(digest, source.label, source.path, optional); err != nil {
			return "", err
		}
	}
	return fmt.Sprintf("sha256:%x", digest.Sum(nil)), nil
}

func hashStableFile(digest hash.Hash, label, path string, optional bool) error {
	before, err := os.Stat(path)
	if optional && os.IsNotExist(err) {
		_, _ = io.WriteString(digest, label+":missing\n")
		return nil
	}
	if err != nil {
		return fmt.Errorf("inspect %s for fingerprint: %w", label, err)
	}
	if before.IsDir() {
		return fmt.Errorf("fingerprint source %s is a directory", label)
	}
	file, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open %s for fingerprint: %w", label, err)
	}
	_, _ = io.WriteString(digest, fmt.Sprintf("%s:%d\n", label, before.Size()))
	_, copyErr := io.Copy(digest, file)
	closeErr := file.Close()
	if copyErr != nil {
		return fmt.Errorf("hash %s: %w", label, copyErr)
	}
	if closeErr != nil {
		return fmt.Errorf("close %s after fingerprint: %w", label, closeErr)
	}
	after, err := os.Stat(path)
	if err != nil || !os.SameFile(before, after) || before.Size() != after.Size() ||
		!before.ModTime().Equal(after.ModTime()) {
		return fmt.Errorf("%s changed while fingerprint was calculated", label)
	}
	return nil
}
