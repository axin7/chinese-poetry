package importer

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSourceFingerprintChangesWithDatabaseOrConfig(t *testing.T) {
	directory := t.TempDir()
	database := filepath.Join(directory, "poetry.db")
	datasets := filepath.Join(directory, "datas.json")
	write := func(path, value string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write(database, "database-v1")
	write(datasets, "config-v1")
	first, err := SourceFingerprint(database, datasets)
	if err != nil {
		t.Fatal(err)
	}
	write(database, "database-v2")
	second, err := SourceFingerprint(database, datasets)
	if err != nil {
		t.Fatal(err)
	}
	write(datasets, "config-v2")
	third, err := SourceFingerprint(database, datasets)
	if err != nil {
		t.Fatal(err)
	}
	if first == second || second == third {
		t.Fatalf("fingerprint did not change: %q %q %q", first, second, third)
	}
}
