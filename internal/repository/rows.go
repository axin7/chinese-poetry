package repository

import (
	"context"
	"database/sql"
	"fmt"
	"reflect"
	"strconv"
	"strings"
)

func (repository *Repository) inspectTableSchema(
	ctx context.Context,
	metadata datasetMetadata,
) (tableSchema, error) {
	rows, err := repository.db.QueryContext(ctx, "PRAGMA table_info("+quote(metadata.Table)+")")
	if err != nil {
		return tableSchema{}, fmt.Errorf("inspect table %s: %w", metadata.Table, err)
	}
	defer rows.Close()
	schema := tableSchema{ColumnSet: make(map[string]struct{})}
	idPrimary := false
	for rows.Next() {
		var sequence, notNull, primary int
		var name, fieldType string
		var defaultValue any
		if err := rows.Scan(&sequence, &name, &fieldType, &notNull, &defaultValue, &primary); err != nil {
			return tableSchema{}, fmt.Errorf("scan table schema: %w", err)
		}
		schema.Columns = append(schema.Columns, name)
		schema.ColumnSet[name] = struct{}{}
		idPrimary = idPrimary || name == "id" && primary == 1
	}
	if err := rows.Err(); err != nil {
		return tableSchema{}, fmt.Errorf("read table schema: %w", err)
	}
	return validateSchema(metadata, schema, idPrimary)
}

func validateSchema(
	metadata datasetMetadata,
	schema tableSchema,
	idPrimary bool,
) (tableSchema, error) {
	if len(schema.Columns) == 0 {
		return tableSchema{}, fmt.Errorf("%w: table %s is missing", ErrDataIntegrity, metadata.Table)
	}
	requiredFields := []string{
		"id", metadata.ContentField, "translation", "translation_status",
	}
	for _, required := range requiredFields {
		if _, ok := schema.ColumnSet[required]; !ok {
			return tableSchema{}, fmt.Errorf("%w: table %s lacks %s", ErrDataIntegrity,
				metadata.Table, required)
		}
	}
	if !idPrimary {
		return tableSchema{}, fmt.Errorf("%w: table %s id is not primary", ErrDataIntegrity,
			metadata.Table)
	}
	_, hasPart := schema.ColumnSet["content_part"]
	_, hasCount := schema.ColumnSet["content_part_count"]
	if hasPart != hasCount {
		return tableSchema{}, fmt.Errorf("%w: incomplete split columns", ErrDataIntegrity)
	}
	schema.HasParts = hasPart
	return schema, nil
}

func queryRowMaps(ctx context.Context, db *sql.DB, query string, args ...any) ([]rowData, error) {
	rows, err := db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	result := make([]rowData, 0)
	for rows.Next() {
		values := make([]any, len(columns))
		targets := make([]any, len(columns))
		for index := range values {
			targets[index] = &values[index]
		}
		if err := rows.Scan(targets...); err != nil {
			return nil, err
		}
		row := make(rowData, len(columns))
		for index, column := range columns {
			row[column] = normalizeDriverValue(values[index])
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func quote(identifier string) string {
	if !identifierPattern.MatchString(identifier) {
		panic("unsafe SQLite identifier")
	}
	return `"` + identifier + `"`
}

func normalizeDriverValue(value any) any {
	if bytes, ok := value.([]byte); ok {
		return string(bytes)
	}
	return value
}

func rowString(row rowData, field string) (string, bool) {
	value, ok := row[field]
	if !ok || value == nil {
		return "", false
	}
	text, ok := value.(string)
	return text, ok
}

func positiveInteger(value any) (int64, bool) {
	switch number := value.(type) {
	case int64:
		return number, number > 0
	case float64:
		integer := int64(number)
		return integer, number == float64(integer) && integer > 0
	case string:
		integer, err := strconv.ParseInt(number, 10, 64)
		return integer, err == nil && integer > 0
	default:
		return 0, false
	}
}

func sameValues(left, right rowData, fields []string) bool {
	for _, field := range fields {
		if !reflect.DeepEqual(left[field], right[field]) {
			return false
		}
	}
	return true
}

func joinQuoted(fields []string) string {
	quoted := make([]string, len(fields))
	for index, field := range fields {
		quoted[index] = quote(field)
	}
	return strings.Join(quoted, ", ")
}
