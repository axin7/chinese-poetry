package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/chinese-poetry/chinese-poetry/internal/model"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
)

func fixture(t *testing.T) (*repository.Repository, exportOptions) {
	t.Helper()
	directory := t.TempDir()
	options := exportOptions{database: filepath.Join(directory, "source.db"),
		datasets: filepath.Join(directory, "datas.json"), generation: "fixture-v1",
		output: filepath.Join(directory, "worker.db"), sqlOutput: filepath.Join(directory, "worker.sql"),
		tables: []string{"poems"}, expected: 3}
	createFixture(t, options)
	repo, err := repository.New(options.database, options.datasets)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = repo.Close() })
	return repo, options
}

func createFixture(t *testing.T, options exportOptions) {
	t.Helper()
	db, err := sql.Open("sqlite", options.database)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	statement := `CREATE TABLE poems (
        id INTEGER PRIMARY KEY, title TEXT, author TEXT, content TEXT,
        content_part INTEGER, content_part_count INTEGER,
        translation TEXT, interpretation TEXT, translation_status TEXT);
        INSERT INTO poems VALUES
        (1, 'A ''quoted'' work', NULL, '[" first ","second"]', 1, 2,
            '["one","two"]', 'First explanation', 'done'),
        (2, 'A ''quoted'' work', NULL, '["","third"]', 2, 2,
            '["three"]', 'Second explanation', 'done'),
        (3, 'Punctuation', NULL, '["!!!"]', 1, 1, '["???"]', '', 'done'),
        (4, 'Pending', NULL, '["pending"]', 1, 1, '["pending"]', '', 'pending');`
	if _, err := db.Exec(statement); err != nil {
		t.Fatal(err)
	}
	config := []byte(`{"datasets":{"poems":{"tag":"content"}}}`)
	if err := os.WriteFile(options.datasets, config, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestCanonicalGroupedExportPreservesSplitWorksAndIndexes(t *testing.T) {
	repo, options := fixture(t)
	report, err := exportCorpus(context.Background(), repo, options)
	if err != nil {
		t.Fatal(err)
	}
	if report.Works != 2 || report.Sentences != 3 || report.EmptyLocatorWorks != 1 ||
		report.VerifiedWorks != 2 || report.VerifiedSentences != 3 || report.CanonicalAnchorCount != 2 {
		t.Fatalf("unexpected verification: %#v", report)
	}
	db := openExport(t, options.output)
	var document, locatorJSON string
	err = db.QueryRow("SELECT document, locators FROM works WHERE work_id='poems:1'").
		Scan(&document, &locatorJSON)
	if err != nil {
		t.Fatal(err)
	}
	var work model.WorkDetailResponse
	if err := json.Unmarshal([]byte(document), &work); err != nil {
		t.Fatal(err)
	}
	if work.Title != "A 'quoted' work" || work.Author != nil ||
		!reflect.DeepEqual(work.Original, []string{"first", "second", "third"}) ||
		!reflect.DeepEqual(work.Interpretations,
			[]string{"First explanation", "Second explanation"}) {
		t.Fatalf("split work differs: %#v", work)
	}
	var locators map[string]string
	if err := json.Unmarshal([]byte(locatorJSON), &locators); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"1:0:0": "first", "1:1:1": "second", "2:1:0": "third"}
	if !reflect.DeepEqual(locators, want) {
		t.Fatalf("sentence locators differ: %#v", locators)
	}
}

func TestSQLImportRoundTripPreservesDocumentsAndMetadata(t *testing.T) {
	repo, options := fixture(t)
	report, err := exportCorpus(context.Background(), repo, options)
	if err != nil {
		t.Fatal(err)
	}
	content, err := os.ReadFile(options.sqlOutput)
	if err != nil {
		t.Fatal(err)
	}
	imported := openExport(t, filepath.Join(t.TempDir(), "imported.db"))
	if _, err := imported.Exec(string(content)); err != nil {
		t.Fatalf("import SQL: %v", err)
	}
	verified, err := verifyCorpus(context.Background(), repo, imported, options)
	if err != nil {
		t.Fatal(err)
	}
	if verified.Sentences != 3 || verified.Works != 2 || report.SQL.Statements != 4 ||
		report.SQL.MaxStatementBytes > maxSQLStatementBytes {
		t.Fatalf("unexpected SQL round-trip: %#v / %#v", verified, report.SQL)
	}
	var count string
	if err := imported.QueryRow("SELECT value FROM metadata WHERE key='sentences_count'").
		Scan(&count); err != nil || count != "3" {
		t.Fatalf("metadata mismatch: %q, %v", count, err)
	}
}

func TestExportCountMismatchRollsBackAndDoesNotOverwrite(t *testing.T) {
	repo, options := fixture(t)
	options.expected = 4
	if _, err := exportCorpus(context.Background(), repo, options); err == nil {
		t.Fatal("unexpected sentence count should fail")
	}
	db := openExport(t, options.output)
	var tables int
	if err := db.QueryRow("SELECT count(*) FROM sqlite_master WHERE type='table'").
		Scan(&tables); err != nil || tables != 0 {
		t.Fatalf("partial export committed tables: %d, %v", tables, err)
	}
	if _, err := os.Stat(options.sqlOutput); !os.IsNotExist(err) {
		t.Fatalf("failed export wrote SQL: %v", err)
	}
	if _, err := exportCorpus(context.Background(), repo, options); !os.IsExist(err) {
		t.Fatalf("existing destination should not be overwritten: %v", err)
	}
}

func TestDocumentUsesEmptyArraysAndExactAPIFields(t *testing.T) {
	encoded, err := workDocument(repository.PoetryWork{WorkID: "poems:1"})
	if err != nil {
		t.Fatal(err)
	}
	var document map[string]json.RawMessage
	if err := json.Unmarshal(encoded, &document); err != nil {
		t.Fatal(err)
	}
	if len(document) != 7 || string(document["id"]) != `"poems:1"` {
		t.Fatalf("unexpected document fields: %s", encoded)
	}
	for _, key := range []string{"original", "translation", "interpretations"} {
		if string(document[key]) != "[]" {
			t.Fatalf("%s must be an empty array: %s", key, encoded)
		}
	}
}

func TestOversizedWorkSQLPreservesUTF8AndQuotes(t *testing.T) {
	db := openExport(t, filepath.Join(t.TempDir(), "large.db"))
	if _, err := db.Exec(schema); err != nil {
		t.Fatal(err)
	}
	document := strings.Repeat("'\u6708", 50000)
	locators := strings.Repeat("\u65e5'", 25000)
	if _, err := db.Exec("INSERT INTO works VALUES ('poems:1', 'v1', ?, ?)",
		document, locators); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "large.sql")
	report, err := exportSQL(context.Background(), db, path)
	if err != nil {
		t.Fatal(err)
	}
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	imported := openExport(t, filepath.Join(t.TempDir(), "imported.db"))
	if _, err := imported.Exec(string(content)); err != nil {
		t.Fatal(err)
	}
	var gotDocument, gotLocators string
	if err := imported.QueryRow("SELECT document,locators FROM works").
		Scan(&gotDocument, &gotLocators); err != nil {
		t.Fatal(err)
	}
	if gotDocument != document || gotLocators != locators || report.ExtraWorkUpdates < 2 ||
		report.MaxStatementBytes > maxSQLStatementBytes {
		t.Fatalf("oversized SQL round-trip failed: %#v", report)
	}
}

func openExport(t *testing.T, path string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}
