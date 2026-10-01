package repository

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
)

func writeDatasetsConfig(t *testing.T, directory string, datasets string) string {
	t.Helper()
	path := filepath.Join(directory, "datas.json")
	content := fmt.Sprintf(`{"datasets":%s}`, datasets)
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func openWritableDB(t *testing.T, directory string) (string, *sql.DB) {
	t.Helper()
	path := filepath.Join(directory, "poetry.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	return path, db
}

func createSimpleRepository(t *testing.T) *Repository {
	t.Helper()
	directory := t.TempDir()
	dbPath, db := openWritableDB(t, directory)
	statement := `CREATE TABLE poems (
        id INTEGER PRIMARY KEY, title TEXT, author TEXT, content TEXT,
        translation TEXT, interpretation TEXT, translation_status TEXT)`
	if _, err := db.Exec(statement); err != nil {
		t.Fatal(err)
	}
	rows := []struct{ source, translation, status string }{
		{`[" 甲 ", "", "！！！", "□□□", "乙"]`,
			`["译甲", "！！！", "原文残缺", "译乙"]`, "done"},
		{"独坐异乡", `["一个人身处异乡"]`, "done"},
		{`["待处理"]`, `["待处理"]`, "pending"},
	}
	for index, row := range rows {
		_, err := db.Exec(`INSERT INTO poems VALUES (?, ?, ?, ?, ?, ?, ?)`,
			index+1, fmt.Sprintf("标题%d", index+1), "作者", row.source,
			row.translation, "解释", row.status)
		if err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	config := writeDatasetsConfig(t, directory,
		`{"poems":{"name":"诗集","tag":"content"}}`)
	repository, err := New(dbPath, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = repository.Close() })
	return repository
}

func TestIterIndexableFiltersStatusAndPreservesIndexes(t *testing.T) {
	repository := createSimpleRepository(t)
	var records []IndexableSentence
	err := repository.IterIndexable(context.Background(), nil, 1,
		func(sentence IndexableSentence) error {
			records = append(records, sentence)
			return nil
		})
	if err != nil {
		t.Fatal(err)
	}
	got := make([]string, 0, len(records))
	for _, record := range records {
		got = append(got, fmt.Sprintf("%d:%d:%d:%s", record.SourceRowID,
			record.RawIndex, record.NormalizedIndex, record.Translation))
	}
	want := []string{"1:0:0:译甲", "1:3:2:原文残缺", "1:4:3:译乙",
		"2:0:0:一个人身处异乡"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("records differ\ngot:  %#v\nwant: %#v", got, want)
	}
	if records[0].Locator().WorkID != "poems:1" {
		t.Fatalf("unexpected locator: %#v", records[0].Locator())
	}
}

func TestRetrieveSearchUsesStableNormalizedPosition(t *testing.T) {
	repository := createSimpleRepository(t)
	locator := Locator{
		Dataset: "poems", SourceRowID: 1, RawIndex: 4,
		NormalizedIndex: 3, WorkID: "poems:1",
	}
	lookup, err := repository.RetrieveSearch(context.Background(), locator)
	if err != nil {
		t.Fatal(err)
	}
	if lookup.Match.Original != "乙" || lookup.Match.WorkSentenceIndex != 3 {
		t.Fatalf("unexpected lookup: %#v", lookup)
	}
	if lookup.Title != "标题1" || lookup.Author == nil || *lookup.Author != "作者" {
		t.Fatalf("unexpected metadata: %#v", lookup)
	}
}

func TestRepositoryIsReadOnlyAndHealthy(t *testing.T) {
	repository := createSimpleRepository(t)
	if err := repository.Health(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := repository.db.Exec(`UPDATE poems SET title = 'changed' WHERE id = 1`); err == nil {
		t.Fatal("read-only repository allowed an update")
	}
}

func createAlignmentRepository(t *testing.T, source, translation string) *Repository {
	t.Helper()
	directory := t.TempDir()
	dbPath, db := openWritableDB(t, directory)
	_, err := db.Exec(`CREATE TABLE poems (
        id INTEGER PRIMARY KEY, content TEXT, translation TEXT, translation_status TEXT)`)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO poems VALUES (1, ?, ?, 'done')`,
		source, translation); err != nil {
		t.Fatal(err)
	}
	_ = db.Close()
	config := writeDatasetsConfig(t, directory, `{"poems":{"tag":"content"}}`)
	repository, err := New(dbPath, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = repository.Close() })
	return repository
}

func TestEqualRawArraysFilterEmptyPairsTogether(t *testing.T) {
	repository := createAlignmentRepository(t,
		`["甲", "", "乙"]`, `["译甲", "", "译乙"]`)
	var got []IndexableSentence
	err := repository.IterIndexable(context.Background(), nil, 10,
		func(value IndexableSentence) error { got = append(got, value); return nil })
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[1].RawIndex != 2 || got[1].NormalizedIndex != 1 {
		t.Fatalf("unexpected aligned records: %#v", got)
	}
}

func TestAlignmentMismatchFailsInsteadOfTruncating(t *testing.T) {
	repository := createAlignmentRepository(t, `["甲", "乙"]`, `["译甲"]`)
	err := repository.IterIndexable(context.Background(), nil, 10,
		func(IndexableSentence) error { return nil })
	if !errors.Is(err, ErrDataIntegrity) {
		t.Fatalf("expected data-integrity error, got %v", err)
	}
}

func createSplitRepository(t *testing.T, secondChapter, secondStatus string) *Repository {
	t.Helper()
	directory := t.TempDir()
	dbPath, db := openWritableDB(t, directory)
	_, err := db.Exec(`CREATE TABLE chapters (
        id INTEGER PRIMARY KEY, book_title TEXT, book_author TEXT, chapter TEXT,
        paragraphs TEXT, content_part INTEGER, content_part_count INTEGER,
        translation TEXT, interpretation TEXT, translation_status TEXT)`)
	if err != nil {
		t.Fatal(err)
	}
	insert := `INSERT INTO chapters VALUES (?, '全书', '作者', ?, ?, ?, 2, ?, ?, ?)`
	_, err = db.Exec(insert, 1, "第一章", `["甲", "乙"]`, 1,
		`["译甲", "译乙"]`, "解一", "done")
	if err == nil {
		_, err = db.Exec(insert, 2, secondChapter, `["", "丙"]`, 2,
			`["译丙"]`, "解二", secondStatus)
	}
	if err != nil {
		t.Fatal(err)
	}
	_ = db.Close()
	config := writeDatasetsConfig(t, directory,
		`{"chapters":{"tag":"paragraphs","object_records":{"path":[]}}}`)
	repository, err := New(dbPath, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = repository.Close() })
	return repository
}

func TestSplitWorkSearchAndDetails(t *testing.T) {
	repository := createSplitRepository(t, "第一章", "done")
	locator := Locator{
		Dataset: "chapters", SourceRowID: 2, RawIndex: 1,
		NormalizedIndex: 0, WorkID: "chapters:1",
	}
	lookup, err := repository.RetrieveSearch(context.Background(), locator)
	if err != nil {
		t.Fatal(err)
	}
	work, err := repository.GetWorkByID(context.Background(), "chapters:1")
	if err != nil {
		t.Fatal(err)
	}
	if lookup.Match.WorkSentenceIndex != 2 || lookup.Title != "第一章" {
		t.Fatalf("unexpected lookup: %#v", lookup)
	}
	if !reflect.DeepEqual(work.Original, []string{"甲", "乙", "丙"}) {
		t.Fatalf("unexpected work: %#v", work)
	}
	if !reflect.DeepEqual(work.Interpretations, []string{"解一", "解二"}) {
		t.Fatalf("unexpected interpretations: %#v", work.Interpretations)
	}
}

func TestSplitWorkRejectsAmbiguityAndIncompleteTranslation(t *testing.T) {
	for _, test := range []struct{ chapter, status string }{
		{"另一章", "done"}, {"第一章", "pending"},
	} {
		repository := createSplitRepository(t, test.chapter, test.status)
		err := repository.IterIndexable(context.Background(), nil, 10,
			func(IndexableSentence) error { return nil })
		if !errors.Is(err, ErrDataIntegrity) {
			t.Fatalf("expected integrity error for %#v, got %v", test, err)
		}
	}
}

func TestLocatorAndWorkIDFailFast(t *testing.T) {
	repository := createSimpleRepository(t)
	_, err := repository.RetrieveSearch(context.Background(), Locator{
		Dataset: "unknown", SourceRowID: 1, WorkID: "unknown:1",
	})
	if !errors.Is(err, ErrInvalidLocator) {
		t.Fatalf("expected invalid locator, got %v", err)
	}
	for _, value := range []string{"poems", "poems:0", "poems:01", "unknown:1"} {
		_, err := repository.GetWorkByID(context.Background(), value)
		if !errors.Is(err, ErrInvalidLocator) {
			t.Fatalf("expected invalid work ID for %q, got %v", value, err)
		}
	}
}

func TestCallbackErrorAndConcurrentReads(t *testing.T) {
	repository := createSimpleRepository(t)
	stop := errors.New("stop")
	err := repository.IterIndexable(context.Background(), nil, 2,
		func(IndexableSentence) error { return stop })
	if !errors.Is(err, stop) {
		t.Fatalf("expected callback error, got %v", err)
	}
	locator := Locator{Dataset: "poems", SourceRowID: 1, WorkID: "poems:1"}
	var wait sync.WaitGroup
	for range 12 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			if _, err := repository.RetrieveSearch(context.Background(), locator); err != nil {
				t.Errorf("concurrent read failed: %v", err)
			}
		}()
	}
	wait.Wait()
}

func TestInvalidConfiguredIdentifierIsRejected(t *testing.T) {
	directory := t.TempDir()
	dbPath, db := openWritableDB(t, directory)
	_ = db.Close()
	config := writeDatasetsConfig(t, directory,
		`{"poems; DROP TABLE poems":{"tag":"content"}}`)
	if _, err := New(dbPath, config); err == nil {
		t.Fatal("unsafe identifier was accepted")
	}
}

func TestProductionDatabaseSmoke(t *testing.T) {
	dbPath := os.Getenv("POETRY_TEST_DB")
	configPath := os.Getenv("POETRY_TEST_DATASETS")
	if dbPath == "" || configPath == "" {
		t.Skip("production database paths are not configured")
	}
	repository, err := New(dbPath, configPath)
	if err != nil {
		t.Fatal(err)
	}
	defer repository.Close()
	count := 0
	err = repository.IterIndexable(context.Background(), nil, 2000,
		func(IndexableSentence) error { count++; return nil })
	if err != nil {
		t.Fatal(err)
	}
	if count != 1_716_980 {
		t.Fatalf("unexpected indexable sentence count: %d", count)
	}
	locator := Locator{
		Dataset: "guwenguanzhi", SourceRowID: 96,
		RawIndex: 0, NormalizedIndex: 0, WorkID: "guwenguanzhi:94",
	}
	lookup, err := repository.RetrieveSearch(context.Background(), locator)
	if err != nil || lookup.Match.WorkSentenceIndex != 21 {
		t.Fatalf("unexpected split lookup: %#v, %v", lookup, err)
	}
}
