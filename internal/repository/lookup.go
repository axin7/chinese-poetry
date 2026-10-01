package repository

import (
	"context"
	"fmt"
	"strconv"
	"strings"
)

var titleFields = []string{"title", "rhythmic", "chapter", "subchapter", "section", "book_title"}
var authorFields = []string{"author", "book_author"}

func (repository *Repository) RetrieveSearch(
	ctx context.Context,
	locator Locator,
) (SearchLookup, error) {
	metadata, err := repository.validateLocator(locator)
	if err != nil {
		return SearchLookup{}, err
	}
	workMetadata, firstID, err := repository.parseWorkID(locator.WorkID)
	if err != nil || workMetadata.Table != metadata.Table {
		return SearchLookup{}, fmt.Errorf("%w: work_id does not match dataset", ErrInvalidLocator)
	}
	schema, err := repository.tableSchema(ctx, metadata)
	if err != nil {
		return SearchLookup{}, err
	}
	rows, err := repository.searchRows(ctx, metadata, schema, firstID, locator.SourceRowID)
	if err != nil {
		return SearchLookup{}, err
	}
	match, err := buildSearchMatch(metadata, rows, locator)
	if err != nil {
		return SearchLookup{}, err
	}
	return SearchLookup{
		Match: match, WorkID: locator.WorkID, Dataset: metadata.Table,
		Title:  displayValue(rows, titleFields, metadata.Name),
		Author: optionalDisplayValue(rows, authorFields),
	}, nil
}

func (repository *Repository) searchRows(
	ctx context.Context,
	metadata datasetMetadata,
	schema tableSchema,
	firstID, rowID int64,
) ([]rowData, error) {
	if firstID > rowID {
		return nil, fmt.Errorf("%w: work starts after source row", ErrInvalidLocator)
	}
	identity := []string(nil)
	if schema.HasParts {
		identity = identityColumns(metadata, schema)
	}
	fields := searchFields(metadata, schema, identity)
	selectList := joinQuoted(fields) +
		`, CASE WHEN id = ? THEN "translation" END AS "translation"`
	query := fmt.Sprintf("SELECT %s FROM %s WHERE id BETWEEN ? AND ? ORDER BY id",
		selectList, quote(metadata.Table))
	rows, err := queryRowMaps(ctx, repository.db, query, rowID, firstID, rowID)
	if err != nil {
		return nil, fmt.Errorf("read search rows: %w", err)
	}
	if len(rows) == 0 || mustRowID(rows[len(rows)-1]) != rowID {
		return nil, fmt.Errorf("%w: %s.%d", ErrNotFound, metadata.Table, rowID)
	}
	if schema.HasParts {
		err = validateSplitRows(rows, firstID, identity, false)
	} else if firstID != rowID || len(rows) != 1 {
		err = fmt.Errorf("%w: work_id does not match source row", ErrInvalidLocator)
	}
	return rows, err
}

func searchFields(
	metadata datasetMetadata,
	schema tableSchema,
	identity []string,
) []string {
	wanted := append([]string{"id", metadata.ContentField, "translation_status"},
		titleFields...)
	wanted = append(wanted, authorFields...)
	if schema.HasParts {
		wanted = append(wanted, "content_part", "content_part_count")
	}
	wanted = append(wanted, identity...)
	return existingUniqueFields(wanted, schema.ColumnSet)
}

func buildSearchMatch(
	metadata datasetMetadata,
	rows []rowData,
	locator Locator,
) (SentenceMatch, error) {
	anchor := rows[len(rows)-1]
	sentences, err := decodeRow(metadata, anchor)
	if err != nil {
		return SentenceMatch{}, err
	}
	sentence, err := locateSentence(sentences, locator)
	if err != nil {
		return SentenceMatch{}, err
	}
	prior := 0
	for _, row := range rows[:len(rows)-1] {
		source, ok := rowString(row, metadata.ContentField)
		if !ok {
			return SentenceMatch{}, fmt.Errorf("%w: source is not text", ErrDataIntegrity)
		}
		values, err := sourceValues(source)
		if err != nil {
			return SentenceMatch{}, err
		}
		normalized, err := normalizedSource(values)
		if err != nil {
			return SentenceMatch{}, err
		}
		prior += len(normalized)
	}
	return SentenceMatch{
		Locator: locator, Original: sentence.Original, Translation: sentence.Translation,
		WorkSentenceIndex: prior + sentence.NormalizedIndex,
	}, nil
}

func (repository *Repository) GetWorkByID(
	ctx context.Context,
	requestedWorkID string,
) (PoetryWork, error) {
	metadata, rowID, err := repository.parseWorkID(requestedWorkID)
	if err != nil {
		return PoetryWork{}, err
	}
	schema, err := repository.tableSchema(ctx, metadata)
	if err != nil {
		return PoetryWork{}, err
	}
	rows, resolvedWorkID, err := repository.fullWorkRows(ctx, metadata, schema, rowID)
	if err != nil {
		return PoetryWork{}, err
	}
	if resolvedWorkID != requestedWorkID {
		return PoetryWork{}, fmt.Errorf("%w: work_id is not the first part", ErrInvalidLocator)
	}
	return buildWork(metadata, rows, resolvedWorkID)
}

func (repository *Repository) fullWorkRows(
	ctx context.Context,
	metadata datasetMetadata,
	schema tableSchema,
	rowID int64,
) ([]rowData, string, error) {
	query := fmt.Sprintf("SELECT * FROM %s WHERE id = ?", quote(metadata.Table))
	anchor, err := queryRowMaps(ctx, repository.db, query, rowID)
	if err != nil {
		return nil, "", err
	}
	if len(anchor) == 0 {
		return nil, "", fmt.Errorf("%w: %s.%d", ErrNotFound, metadata.Table, rowID)
	}
	if !schema.HasParts {
		return anchor, workID(metadata.Table, rowID), nil
	}
	part, count, err := partValues(anchor[0])
	if err != nil {
		return nil, "", err
	}
	firstID := rowID - part + 1
	if count == 1 {
		return anchor, workID(metadata.Table, firstID), nil
	}
	query = fmt.Sprintf("SELECT * FROM %s WHERE id BETWEEN ? AND ? ORDER BY id",
		quote(metadata.Table))
	rows, err := queryRowMaps(ctx, repository.db, query, firstID, firstID+count-1)
	if err != nil {
		return nil, "", err
	}
	err = validateSplitRows(rows, firstID, identityColumns(metadata, schema), true)
	return rows, workID(metadata.Table, firstID), err
}

func buildWork(
	metadata datasetMetadata,
	rows []rowData,
	resolvedWorkID string,
) (PoetryWork, error) {
	work := PoetryWork{
		WorkID: resolvedWorkID, Dataset: metadata.Table,
		Title:  displayValue(rows, titleFields, metadata.Name),
		Author: optionalDisplayValue(rows, authorFields),
	}
	for _, row := range rows {
		rowID := mustRowID(row)
		sentences, err := decodeRow(metadata, row)
		if err != nil {
			return PoetryWork{}, err
		}
		work.SourceRowIDs = append(work.SourceRowIDs, rowID)
		for _, sentence := range sentences {
			work.Original = append(work.Original, sentence.Original)
			work.Translation = append(work.Translation, sentence.Translation)
		}
		if interpretation, ok := rowString(row, "interpretation"); ok {
			if text := strings.TrimSpace(interpretation); text != "" {
				work.Interpretations = append(work.Interpretations, text)
			}
		}
	}
	return work, nil
}

func (repository *Repository) parseWorkID(value string) (datasetMetadata, int64, error) {
	parts := strings.Split(value, ":")
	if len(parts) != 2 {
		return datasetMetadata{}, 0, fmt.Errorf("%w: work_id is invalid", ErrInvalidLocator)
	}
	metadata, ok := repository.datasets[parts[0]]
	rowID, err := strconv.ParseInt(parts[1], 10, 64)
	if !ok || err != nil || rowID <= 0 || parts[1] != strconv.FormatInt(rowID, 10) {
		return datasetMetadata{}, 0, fmt.Errorf("%w: work_id is invalid", ErrInvalidLocator)
	}
	return metadata, rowID, nil
}

func decodeRow(metadata datasetMetadata, row rowData) ([]alignedSentence, error) {
	status, _ := rowString(row, "translation_status")
	if status != "done" {
		return nil, fmt.Errorf("%w: translation is not done", ErrDataIntegrity)
	}
	source, sourceOK := rowString(row, metadata.ContentField)
	translation, translationOK := rowString(row, "translation")
	if !sourceOK || !translationOK {
		return nil, fmt.Errorf("%w: source or translation is not text", ErrDataIntegrity)
	}
	return decodeAligned(metadata, mustRowID(row), source, translation)
}

func locateSentence(sentences []alignedSentence, locator Locator) (alignedSentence, error) {
	if locator.NormalizedIndex >= len(sentences) {
		return alignedSentence{}, fmt.Errorf("%w: normalized index is out of range",
			ErrInvalidLocator)
	}
	sentence := sentences[locator.NormalizedIndex]
	if sentence.RawIndex != locator.RawIndex {
		return alignedSentence{}, fmt.Errorf("%w: raw and normalized indexes differ",
			ErrInvalidLocator)
	}
	return sentence, nil
}

func displayValue(rows []rowData, fields []string, fallback string) string {
	if value := optionalDisplayValue(rows, fields); value != nil {
		return *value
	}
	return fallback
}

func optionalDisplayValue(rows []rowData, fields []string) *string {
	for _, field := range fields {
		for _, row := range rows {
			if value, ok := rowString(row, field); ok && strings.TrimSpace(value) != "" {
				trimmed := strings.TrimSpace(value)
				return &trimmed
			}
		}
	}
	return nil
}

func existingUniqueFields(fields []string, available map[string]struct{}) []string {
	seen := make(map[string]struct{}, len(fields))
	result := make([]string, 0, len(fields))
	for _, field := range fields {
		if _, ok := available[field]; !ok {
			continue
		}
		if _, duplicate := seen[field]; duplicate {
			continue
		}
		seen[field] = struct{}{}
		result = append(result, field)
	}
	return result
}

func mustRowID(row rowData) int64 {
	value, _ := positiveInteger(row["id"])
	return value
}
