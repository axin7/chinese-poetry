package importer

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCheckpointRoundTripUsesArrayKey(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	store := NewCheckpointStore(path, "v1", "sha256:source")
	progress := Progress{
		Generation: "v1", SourceFingerprint: "sha256:source",
		Scope:   []string{"tangsong"},
		LastKey: &Key{Dataset: "tangsong", RowID: 8, RawIndex: 2}, Indexed: 13,
	}

	if err := store.Save(progress); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) == "" || !contains(string(data), `"last_key": [`) {
		t.Fatalf("checkpoint key must remain an array: %s", data)
	}
	loaded, err := store.Load(false, []string{"tangsong"})
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Indexed != 13 || loaded.LastKey == nil || loaded.LastKey.RowID != 8 {
		t.Fatalf("unexpected checkpoint: %#v", loaded)
	}
}

func TestCheckpointRejectsGenerationAndScopeMismatch(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	store := NewCheckpointStore(path, "v1", "sha256:source")
	progress := Progress{
		Generation: "v1", SourceFingerprint: "sha256:source",
		Scope: []string{"tangsong"},
	}
	if err := store.Save(progress); err != nil {
		t.Fatal(err)
	}
	if _, err := NewCheckpointStore(path, "v2", "sha256:source").Load(
		false, progress.Scope,
	); err == nil {
		t.Fatal("expected generation mismatch")
	}
	if _, err := NewCheckpointStore(path, "v1", "sha256:changed").Load(
		false, progress.Scope,
	); err == nil {
		t.Fatal("expected source fingerprint mismatch")
	}
	if _, err := store.Load(false, []string{"songci"}); err == nil {
		t.Fatal("expected scope mismatch")
	}
}

func TestCheckpointResetIgnoresExistingFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	if err := os.WriteFile(path, []byte("invalid"), 0o644); err != nil {
		t.Fatal(err)
	}
	progress, err := NewCheckpointStore(path, "v1", "sha256:source").Load(
		true, []string{"__all__"},
	)
	if err != nil {
		t.Fatal(err)
	}
	if progress.Indexed != 0 || progress.LastKey != nil {
		t.Fatalf("unexpected reset progress: %#v", progress)
	}
}

func TestCheckpointCompletedRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	store := NewCheckpointStore(path, "v1", "sha256:source")
	progress := Progress{
		Generation: "v1", SourceFingerprint: "sha256:source",
		Scope: []string{"__all__"}, Completed: true,
	}
	if err := store.Save(progress); err != nil {
		t.Fatal(err)
	}
	loaded, err := store.Load(false, progress.Scope)
	if err != nil {
		t.Fatal(err)
	}
	if !loaded.Completed {
		t.Fatal("completed checkpoint was not preserved")
	}
}

func contains(value, fragment string) bool {
	for index := 0; index+len(fragment) <= len(value); index++ {
		if value[index:index+len(fragment)] == fragment {
			return true
		}
	}
	return false
}
