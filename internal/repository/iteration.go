package repository

import (
	"context"
	"database/sql"
	"fmt"
)

func (repository *Repository) IterIndexable(
	ctx context.Context,
	datasets []string,
	batchSize int,
	yield func(IndexableSentence) error,
) error {
	if batchSize <= 0 {
		return fmt.Errorf("batch size must be positive")
	}
	if yield == nil {
		return fmt.Errorf("yield callback is required")
	}
	selected, err := repository.selectedDatasets(datasets)
	if err != nil {
		return err
	}
	for _, metadata := range selected {
		if err := repository.iterDataset(ctx, metadata, batchSize, yield); err != nil {
			return err
		}
	}
	return nil
}

func (repository *Repository) iterDataset(
	ctx context.Context,
	metadata datasetMetadata,
	batchSize int,
	yield func(IndexableSentence) error,
) error {
	schema, err := repository.tableSchema(ctx, metadata)
	if err != nil {
		return err
	}
	splitIDs, err := repository.splitWorkIDs(ctx, metadata, schema)
	if err != nil {
		return err
	}
	lastID := int64(0)
	for {
		processed, nextID, err := repository.iterBatch(
			ctx, metadata, schema, splitIDs, lastID, batchSize, yield,
		)
		if err != nil {
			return err
		}
		if processed == 0 {
			return nil
		}
		lastID = nextID
	}
}

func (repository *Repository) iterBatch(
	ctx context.Context,
	metadata datasetMetadata,
	schema tableSchema,
	splitIDs map[int64]string,
	lastID int64,
	batchSize int,
	yield func(IndexableSentence) error,
) (int, int64, error) {
	rows, err := repository.indexRows(ctx, metadata, schema, lastID, batchSize)
	if err != nil {
		return 0, lastID, err
	}
	defer rows.Close()
	processed := 0
	for rows.Next() {
		row, err := scanIndexRow(rows, schema.HasParts)
		if err != nil {
			return processed, lastID, err
		}
		lastID = row.ID
		processed++
		if err := emitIndexRow(metadata, row, splitIDs, yield); err != nil {
			return processed, lastID, err
		}
	}
	return processed, lastID, rows.Err()
}

type indexRow struct {
	ID               int64
	Source           string
	Translation      string
	Part             any
	PartCount        any
	TranslationState string
}

func (repository *Repository) indexRows(
	ctx context.Context,
	metadata datasetMetadata,
	schema tableSchema,
	lastID int64,
	batchSize int,
) (*sql.Rows, error) {
	fields := []string{"id", metadata.ContentField, "translation", "translation_status"}
	if schema.HasParts {
		fields = append(fields, "content_part", "content_part_count")
	}
	query := fmt.Sprintf(
		"SELECT %s FROM %s WHERE translation_status = ? AND id > ? ORDER BY id LIMIT ?",
		joinQuoted(fields), quote(metadata.Table),
	)
	return repository.db.QueryContext(ctx, query, "done", lastID, batchSize)
}

func scanIndexRow(rows *sql.Rows, hasParts bool) (indexRow, error) {
	var row indexRow
	var err error
	if hasParts {
		err = rows.Scan(&row.ID, &row.Source, &row.Translation, &row.TranslationState,
			&row.Part, &row.PartCount)
	} else {
		err = rows.Scan(&row.ID, &row.Source, &row.Translation, &row.TranslationState)
	}
	if err != nil {
		return indexRow{}, fmt.Errorf("scan index row: %w", err)
	}
	return row, nil
}

func emitIndexRow(
	metadata datasetMetadata,
	row indexRow,
	splitIDs map[int64]string,
	yield func(IndexableSentence) error,
) error {
	resolvedWorkID := workID(metadata.Table, row.ID)
	if row.Part != nil || row.PartCount != nil {
		_, count, err := partValues(rowData{
			"content_part": row.Part, "content_part_count": row.PartCount,
		})
		if err != nil {
			return err
		}
		if count > 1 {
			var ok bool
			resolvedWorkID, ok = splitIDs[row.ID]
			if !ok {
				return fmt.Errorf("%w: split row cannot resolve work", ErrDataIntegrity)
			}
		}
	}
	sentences, err := decodeAligned(metadata, row.ID, row.Source, row.Translation)
	if err != nil {
		return err
	}
	return emitSentences(metadata, row.ID, resolvedWorkID, sentences, yield)
}

func emitSentences(
	metadata datasetMetadata,
	rowID int64,
	resolvedWorkID string,
	sentences []alignedSentence,
	yield func(IndexableSentence) error,
) error {
	for _, sentence := range sentences {
		if !pairIsIndexable(sentence.Original, sentence.Translation) {
			continue
		}
		item := IndexableSentence{
			Dataset: metadata.Table, SourceRowID: rowID,
			RawIndex: sentence.RawIndex, NormalizedIndex: sentence.NormalizedIndex,
			WorkID: resolvedWorkID, Original: sentence.Original,
			Translation: sentence.Translation,
		}
		if err := yield(item); err != nil {
			return err
		}
	}
	return nil
}
