package repository

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite"
)

const (
	maxOpenConnections = 8
	maxIdleConnections = 4
)

type Repository struct {
	db       *sql.DB
	datasets map[string]datasetMetadata
	order    []string
	schemas  map[string]tableSchema
}

func New(dbPath, datasetsConfigPath string) (*Repository, error) {
	absolute, err := filepath.Abs(dbPath)
	if err != nil {
		return nil, fmt.Errorf("resolve database path: %w", err)
	}
	if info, err := os.Stat(absolute); err != nil || info.IsDir() {
		if err == nil {
			err = fmt.Errorf("path is a directory")
		}
		return nil, fmt.Errorf("invalid database path: %w", err)
	}
	datasets, order, err := loadDatasets(datasetsConfigPath)
	if err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", readOnlyDSN(absolute))
	if err != nil {
		return nil, fmt.Errorf("open database: %w", err)
	}
	db.SetMaxOpenConns(maxOpenConnections)
	db.SetMaxIdleConns(maxIdleConnections)
	db.SetConnMaxIdleTime(5 * time.Minute)
	repository := &Repository{
		db: db, datasets: datasets, order: order,
		schemas: make(map[string]tableSchema, len(datasets)),
	}
	if err := repository.Health(context.Background()); err != nil {
		_ = db.Close()
		return nil, err
	}
	if err := repository.loadSchemas(context.Background()); err != nil {
		_ = db.Close()
		return nil, err
	}
	return repository, nil
}

func (repository *Repository) loadSchemas(ctx context.Context) error {
	for _, name := range repository.order {
		metadata := repository.datasets[name]
		schema, err := repository.inspectTableSchema(ctx, metadata)
		if err != nil {
			return err
		}
		repository.schemas[name] = schema
	}
	return nil
}

func (repository *Repository) tableSchema(
	_ context.Context,
	metadata datasetMetadata,
) (tableSchema, error) {
	schema, ok := repository.schemas[metadata.Table]
	if !ok {
		return tableSchema{}, fmt.Errorf("%w: table schema is unavailable", ErrDataIntegrity)
	}
	return schema, nil
}

func readOnlyDSN(path string) string {
	query := url.Values{}
	query.Set("mode", "ro")
	query.Add("_pragma", "query_only(1)")
	query.Add("_pragma", "busy_timeout(5000)")
	return (&url.URL{Scheme: "file", Path: path, RawQuery: query.Encode()}).String()
}

func (repository *Repository) Close() error {
	return repository.db.Close()
}

func (repository *Repository) Health(ctx context.Context) error {
	if err := repository.db.PingContext(ctx); err != nil {
		return fmt.Errorf("database health check: %w", err)
	}
	var value int
	if err := repository.db.QueryRowContext(ctx, "SELECT 1").Scan(&value); err != nil {
		return fmt.Errorf("database health query: %w", err)
	}
	return nil
}

func (repository *Repository) selectedDatasets(names []string) ([]datasetMetadata, error) {
	if names == nil {
		names = repository.order
	}
	if len(names) == 0 {
		return nil, fmt.Errorf("datasets must not be empty")
	}
	seen := make(map[string]struct{}, len(names))
	result := make([]datasetMetadata, 0, len(names))
	for _, name := range names {
		metadata, ok := repository.datasets[name]
		if !ok {
			return nil, fmt.Errorf("dataset %q is not configured", name)
		}
		if _, duplicate := seen[name]; duplicate {
			continue
		}
		seen[name] = struct{}{}
		result = append(result, metadata)
	}
	return result, nil
}

func (repository *Repository) validateLocator(locator Locator) (datasetMetadata, error) {
	metadata, ok := repository.datasets[locator.Dataset]
	if !ok {
		return datasetMetadata{}, fmt.Errorf("%w: dataset is not configured", ErrInvalidLocator)
	}
	if locator.SourceRowID <= 0 || locator.RawIndex < 0 || locator.NormalizedIndex < 0 {
		return datasetMetadata{}, fmt.Errorf("%w: indexes are invalid", ErrInvalidLocator)
	}
	if locator.WorkID == "" {
		return datasetMetadata{}, fmt.Errorf("%w: work_id is empty", ErrInvalidLocator)
	}
	return metadata, nil
}
