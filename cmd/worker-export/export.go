package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"strconv"

	"github.com/chinese-poetry/chinese-poetry/internal/model"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
)

const schema = `CREATE TABLE metadata (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE works (
  work_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
  document TEXT NOT NULL, locators TEXT NOT NULL
);`

type canonicalRepository interface {
	IterIndexable(context.Context, []string, int, func(repository.IndexableSentence) error) error
	GetWorkByID(context.Context, string) (repository.PoetryWork, error)
}

type exportState struct {
	repo       canonicalRepository
	tx         *sql.Tx
	generation string
	works      map[string]map[string]string
	counts     map[string]int64
	sentences  int64
	insertWork *sql.Stmt
}

func exportCorpus(
	ctx context.Context, repo canonicalRepository, options exportOptions,
) (exportReport, error) {
	if err := writeNewFile(options.output, nil); err != nil {
		return exportReport{}, err
	}
	db, err := sql.Open("sqlite", options.output)
	if err != nil {
		return exportReport{}, err
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	if _, err := db.ExecContext(ctx, "PRAGMA journal_mode=DELETE"); err != nil {
		return exportReport{}, err
	}
	if err := populate(ctx, repo, db, options); err != nil {
		return exportReport{}, err
	}
	report, err := verifyCorpus(ctx, repo, db, options)
	if err != nil {
		return exportReport{}, err
	}
	if options.sqlOutput != "" {
		report.SQL, err = exportSQL(ctx, db, options.sqlOutput)
		if err != nil {
			return exportReport{}, err
		}
		report.ExpectedD1RowsWritten += 2 * report.SQL.ExtraWorkUpdates
	}
	if err := db.Close(); err != nil {
		return exportReport{}, err
	}
	report.SQLite, err = describeFile(options.output)
	if err != nil {
		return exportReport{}, err
	}
	return report, nil
}

func populate(
	ctx context.Context, repo canonicalRepository, db *sql.DB, options exportOptions,
) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err := tx.ExecContext(ctx, schema); err != nil {
		return err
	}
	state, err := newExportState(ctx, repo, tx, options.generation)
	if err != nil {
		return err
	}
	defer state.insertWork.Close()
	if err := repo.IterIndexable(ctx, options.tables, 256, state.addSentence); err != nil {
		return err
	}
	if options.expected != 0 && state.sentences != options.expected {
		return fmt.Errorf("sentence count %d does not equal %d", state.sentences, options.expected)
	}
	anchors, err := canonicalWorkIDs(ctx, options.database, options.tables)
	if err != nil {
		return err
	}
	if err := state.addWorks(ctx, anchors); err != nil {
		return err
	}
	if err := state.addMetadata(ctx); err != nil {
		return err
	}
	return tx.Commit()
}

func newExportState(
	ctx context.Context, repo canonicalRepository, tx *sql.Tx, generation string,
) (*exportState, error) {
	work, err := tx.PrepareContext(ctx, "INSERT INTO works VALUES (?, ?, ?, ?)")
	if err != nil {
		return nil, err
	}
	return &exportState{repo: repo, tx: tx, generation: generation,
		works: make(map[string]map[string]string), counts: make(map[string]int64),
		insertWork: work}, nil
}

func (state *exportState) addSentence(line repository.IndexableSentence) error {
	if _, exists := state.works[line.WorkID]; !exists {
		state.works[line.WorkID] = make(map[string]string)
	}
	key := locatorKey(line)
	if _, duplicate := state.works[line.WorkID][key]; duplicate {
		return fmt.Errorf("duplicate sentence locator %s/%s", line.WorkID, key)
	}
	state.works[line.WorkID][key] = line.Original
	state.sentences++
	state.counts[line.Dataset]++
	return nil
}

func (state *exportState) addWorks(ctx context.Context, anchors []string) error {
	for _, workID := range anchors {
		work, err := state.repo.GetWorkByID(ctx, workID)
		if err != nil {
			return fmt.Errorf("load canonical work %s: %w", workID, err)
		}
		document, err := workDocument(work)
		if err != nil {
			return err
		}
		if _, exists := state.works[workID]; !exists {
			state.works[workID] = make(map[string]string)
		}
		locators, err := json.Marshal(state.works[workID])
		if err != nil {
			return err
		}
		if len(workID)+len(state.generation)+len(document)+len(locators) >= 2*1024*1024 {
			return fmt.Errorf("work %s exceeds D1's 2 MiB row limit", workID)
		}
		if _, err := state.insertWork.ExecContext(ctx, workID,
			state.generation, string(document), string(locators)); err != nil {
			return err
		}
	}
	if len(anchors) != len(state.works) {
		return fmt.Errorf("indexed works do not match canonical anchors")
	}
	return nil
}

func locatorKey(line repository.IndexableSentence) string {
	return fmt.Sprintf("%d:%d:%d", line.SourceRowID, line.RawIndex, line.NormalizedIndex)
}

func (state *exportState) addMetadata(ctx context.Context) error {
	counts, err := json.Marshal(state.counts)
	if err != nil {
		return err
	}
	values := map[string]string{
		"generation": state.generation, "schema_version": "1",
		"sentences_count": strconv.FormatInt(state.sentences, 10),
		"works_count":     strconv.Itoa(len(state.works)), "dataset_counts": string(counts),
	}
	for key, value := range values {
		if _, err := state.tx.ExecContext(ctx,
			"INSERT INTO metadata VALUES (?, ?)", key, value); err != nil {
			return err
		}
	}
	return nil
}

func workDocument(work repository.PoetryWork) ([]byte, error) {
	return json.Marshal(model.WorkDetailResponse{
		ID: work.WorkID, Dataset: work.Dataset, Title: work.Title, Author: work.Author,
		Original: nonNilStrings(work.Original), Translation: nonNilStrings(work.Translation),
		Interpretations: nonNilStrings(work.Interpretations),
	})
}

func nonNilStrings(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func describeFile(path string) (fileReport, error) {
	info, err := os.Stat(path)
	if err != nil {
		return fileReport{}, err
	}
	hash, err := hashFile(path)
	return fileReport{Path: path, Bytes: info.Size(), SHA256: hash}, err
}
