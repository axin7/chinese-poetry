package main

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"path/filepath"
	"regexp"
)

var tableIdentifier = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// Only anchor IDs are read directly; canonical repository code decodes every work.
func canonicalWorkIDs(ctx context.Context, database string, tables []string) ([]string, error) {
	absolute, err := filepath.Abs(database)
	if err != nil {
		return nil, err
	}
	uri := &url.URL{Scheme: "file", Path: absolute, RawQuery: "mode=ro"}
	db, err := sql.Open("sqlite", uri.String())
	if err != nil {
		return nil, err
	}
	defer db.Close()
	var result []string
	seen := make(map[string]bool)
	for _, table := range tables {
		if !tableIdentifier.MatchString(table) {
			return nil, fmt.Errorf("invalid table identifier %q", table)
		}
		if seen[table] {
			continue
		}
		seen[table] = true
		ids, err := datasetWorkIDs(ctx, db, table)
		if err != nil {
			return nil, err
		}
		result = append(result, ids...)
	}
	return result, nil
}

func datasetWorkIDs(ctx context.Context, db *sql.DB, table string) ([]string, error) {
	var parts int
	err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM pragma_table_info(?) WHERE name = 'content_part'`, table).Scan(&parts)
	if err != nil {
		return nil, err
	}
	query := fmt.Sprintf(`SELECT id FROM "%s" WHERE translation_status = 'done'`, table)
	if parts != 0 {
		query += " AND (content_part IS NULL OR content_part = 1)"
	}
	rows, err := db.QueryContext(ctx, query+" ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []string
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		result = append(result, fmt.Sprintf("%s:%d", table, id))
	}
	return result, rows.Err()
}
