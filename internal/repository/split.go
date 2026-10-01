package repository

import (
	"context"
	"fmt"
)

var systemFields = map[string]struct{}{
	"id": {}, "translation": {}, "interpretation": {}, "translation_status": {},
	"translation_updated_at": {}, "translation_error": {},
	"translation_retry_count": {}, "content_part": {}, "content_part_count": {},
}

func identityColumns(metadata datasetMetadata, schema tableSchema) []string {
	excluded := make(map[string]struct{}, len(systemFields)+len(metadata.AlignedFields)+1)
	for field := range systemFields {
		excluded[field] = struct{}{}
	}
	excluded[metadata.ContentField] = struct{}{}
	for _, field := range metadata.AlignedFields {
		excluded[field] = struct{}{}
	}
	result := make([]string, 0)
	for _, field := range schema.Columns {
		if _, skip := excluded[field]; !skip {
			result = append(result, field)
		}
	}
	return result
}

func (repository *Repository) splitWorkIDs(
	ctx context.Context,
	metadata datasetMetadata,
	schema tableSchema,
) (map[int64]string, error) {
	if !schema.HasParts {
		return map[int64]string{}, nil
	}
	identity := identityColumns(metadata, schema)
	fields := append([]string{"id", "content_part", "content_part_count",
		"translation_status"}, identity...)
	query := fmt.Sprintf(
		"SELECT %s FROM %s WHERE content_part_count > 1 OR content_part > 1 ORDER BY id",
		joinQuoted(fields), quote(metadata.Table),
	)
	rows, err := queryRowMaps(ctx, repository.db, query)
	if err != nil {
		return nil, fmt.Errorf("read split works: %w", err)
	}
	groups, err := groupSplitRows(rows)
	if err != nil {
		return nil, err
	}
	result := make(map[int64]string, len(rows))
	for firstID, group := range groups {
		if err := validateSplitRows(group, firstID, identity, true); err != nil {
			return nil, err
		}
		for _, row := range group {
			rowID, _ := positiveInteger(row["id"])
			result[rowID] = workID(metadata.Table, firstID)
		}
	}
	return result, nil
}

func groupSplitRows(rows []rowData) (map[int64][]rowData, error) {
	groups := make(map[int64][]rowData)
	for _, row := range rows {
		rowID, ok := positiveInteger(row["id"])
		if !ok {
			return nil, fmt.Errorf("%w: split row id is invalid", ErrDataIntegrity)
		}
		part, count, err := partValues(row)
		if err != nil {
			return nil, err
		}
		if count == 1 {
			return nil, fmt.Errorf("%w: contradictory split count", ErrDataIntegrity)
		}
		firstID := rowID - part + 1
		groups[firstID] = append(groups[firstID], row)
	}
	return groups, nil
}

func validateSplitRows(
	rows []rowData,
	firstID int64,
	identity []string,
	complete bool,
) error {
	if firstID <= 0 || len(rows) == 0 {
		return fmt.Errorf("%w: split work has an invalid first row", ErrDataIntegrity)
	}
	_, expectedCount, err := partValues(rows[0])
	if err != nil {
		return err
	}
	if complete && int64(len(rows)) != expectedCount {
		return fmt.Errorf("%w: split work is incomplete", ErrDataIntegrity)
	}
	for index, row := range rows {
		if err := validateSplitRow(row, rows[0], firstID, int64(index), expectedCount,
			identity); err != nil {
			return err
		}
	}
	return nil
}

func validateSplitRow(
	row rowData,
	baseline rowData,
	firstID, offset, expectedCount int64,
	identity []string,
) error {
	rowID, ok := positiveInteger(row["id"])
	if !ok {
		return fmt.Errorf("%w: split row id is invalid", ErrDataIntegrity)
	}
	part, count, err := partValues(row)
	if err != nil {
		return err
	}
	status, _ := rowString(row, "translation_status")
	if rowID != firstID+offset || part != offset+1 || count != expectedCount {
		return fmt.Errorf("%w: split rows are not contiguous", ErrDataIntegrity)
	}
	if status != "done" || !sameValues(row, baseline, identity) {
		return fmt.Errorf("%w: split row metadata is inconsistent", ErrDataIntegrity)
	}
	return nil
}

func partValues(row rowData) (int64, int64, error) {
	partRaw, partExists := row["content_part"]
	countRaw, countExists := row["content_part_count"]
	if (!partExists || partRaw == nil) && (!countExists || countRaw == nil) {
		return 1, 1, nil
	}
	part, partOK := positiveInteger(partRaw)
	count, countOK := positiveInteger(countRaw)
	if !partOK || !countOK || part > count {
		return 0, 0, fmt.Errorf("%w: split fields are invalid", ErrDataIntegrity)
	}
	return part, count, nil
}

func workID(dataset string, firstRowID int64) string {
	return fmt.Sprintf("%s:%d", dataset, firstRowID)
}
