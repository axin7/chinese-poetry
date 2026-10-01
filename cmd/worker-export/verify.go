package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"

	"github.com/chinese-poetry/chinese-poetry/internal/repository"
)

type fileReport struct {
	Path   string `json:"path"`
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}

type sqlReport struct {
	fileReport
	Statements        int64 `json:"statements"`
	MaxStatementBytes int   `json:"max_statement_bytes"`
	ExtraWorkUpdates  int64 `json:"extra_work_updates"`
}

type exportReport struct {
	Generation            string           `json:"generation"`
	Works                 int64            `json:"works"`
	Sentences             int64            `json:"sentences"`
	DatasetSentences      map[string]int64 `json:"dataset_sentences"`
	DatasetWorks          map[string]int64 `json:"dataset_works"`
	EmptyLocatorWorks     int64            `json:"empty_locator_works"`
	MaxRowBytes           int64            `json:"max_row_bytes"`
	VerifiedWorks         int64            `json:"verified_works"`
	VerifiedSentences     int64            `json:"verified_sentences"`
	CanonicalAnchorCount  int64            `json:"canonical_anchor_count"`
	SourceDatabase        fileReport       `json:"source_database"`
	SourceDatasets        fileReport       `json:"source_datasets"`
	SQLite                fileReport       `json:"sqlite"`
	SQL                   sqlReport        `json:"sql"`
	ExpectedD1RowsWritten int64            `json:"expected_d1_rows_written"`
}

func verifyCorpus(
	ctx context.Context, repo canonicalRepository, db *sql.DB, options exportOptions,
) (exportReport, error) {
	report := exportReport{Generation: options.generation,
		DatasetSentences: make(map[string]int64), DatasetWorks: make(map[string]int64)}
	locators, err := verifyWorks(ctx, repo, db, options.generation, &report)
	if err != nil {
		return report, err
	}
	if err := verifySentences(ctx, repo, options.tables, locators, &report); err != nil {
		return report, err
	}
	anchors, err := canonicalWorkIDs(ctx, options.database, options.tables)
	if err != nil {
		return report, err
	}
	report.CanonicalAnchorCount = int64(len(anchors))
	if report.CanonicalAnchorCount != report.Works {
		return report, fmt.Errorf("canonical anchor count differs from exported works")
	}
	report.SourceDatabase, err = describeFile(options.database)
	if err == nil {
		report.SourceDatasets, err = describeFile(options.datasets)
	}
	// Each work writes a table row and its primary-key index entry; metadata has five rows.
	report.ExpectedD1RowsWritten = 2 * (report.Works + 5)
	return report, err
}

func verifyWorks(
	ctx context.Context, repo canonicalRepository, db *sql.DB, generation string,
	report *exportReport,
) (map[string]map[string]string, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT work_id, generation, document, locators FROM works ORDER BY work_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := make(map[string]map[string]string)
	for rows.Next() {
		id, locators, err := verifyWorkRow(ctx, repo, rows, generation, report)
		if err != nil {
			return nil, err
		}
		result[id] = locators
	}
	return result, rows.Err()
}

func verifyWorkRow(
	ctx context.Context, repo canonicalRepository, rows *sql.Rows, generation string,
	report *exportReport,
) (string, map[string]string, error) {
	var id, version, document, locatorJSON string
	if err := rows.Scan(&id, &version, &document, &locatorJSON); err != nil {
		return "", nil, err
	}
	if version != generation {
		return "", nil, fmt.Errorf("unexpected work generation %q", version)
	}
	work, err := repo.GetWorkByID(ctx, id)
	if err != nil {
		return "", nil, err
	}
	expected, err := workDocument(work)
	if err != nil || !bytes.Equal(expected, []byte(document)) {
		return "", nil, fmt.Errorf("canonical document differs for %s", id)
	}
	var locators map[string]string
	if err := json.Unmarshal([]byte(locatorJSON), &locators); err != nil || locators == nil {
		return "", nil, fmt.Errorf("invalid locator JSON for %s", id)
	}
	report.Works++
	report.VerifiedWorks++
	report.DatasetWorks[work.Dataset]++
	if len(locators) == 0 {
		report.EmptyLocatorWorks++
	}
	rowBytes := int64(len(id) + len(version) + len(document) + len(locatorJSON))
	if rowBytes > report.MaxRowBytes {
		report.MaxRowBytes = rowBytes
	}
	return id, locators, nil
}

func verifySentences(
	ctx context.Context, repo canonicalRepository, tables []string,
	locators map[string]map[string]string, report *exportReport,
) error {
	err := repo.IterIndexable(ctx, tables, 256, func(line repository.IndexableSentence) error {
		work, exists := locators[line.WorkID]
		if !exists || work[locatorKey(line)] != line.Original {
			return fmt.Errorf("canonical sentence differs: %s/%s", line.WorkID, locatorKey(line))
		}
		delete(work, locatorKey(line))
		report.Sentences++
		report.VerifiedSentences++
		report.DatasetSentences[line.Dataset]++
		return nil
	})
	if err != nil {
		return err
	}
	for id, remaining := range locators {
		if len(remaining) != 0 {
			return fmt.Errorf("extra exported sentence locators for %s", id)
		}
	}
	return nil
}

func hashFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}
