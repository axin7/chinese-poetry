package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strings"

	"github.com/chinese-poetry/chinese-poetry/internal/repository"
)

type exportOptions struct {
	database, datasets, generation, output, sqlOutput, report string
	tables                                                    []string
	expected                                                  int64
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	options := exportOptions{}
	var tables string
	flag.StringVar(&options.database, "db", "", "read-only canonical SQLite corpus")
	flag.StringVar(&options.datasets, "datasets", "", "canonical datasets JSON")
	flag.StringVar(&options.generation, "generation", "", "Qdrant corpus generation")
	flag.StringVar(&options.output, "out", "", "new D1-compatible SQLite file")
	flag.StringVar(&options.sqlOutput, "sql", "", "optional new D1 import SQL file")
	flag.StringVar(&options.report, "report", "", "new JSON verification report")
	flag.StringVar(&tables, "tables", "yudingquantangshi,tangshisanbaishou", "dataset list")
	flag.Int64Var(&options.expected, "expected-sentences", 0, "fail on unexpected count")
	flag.Parse()
	options.tables = strings.Split(tables, ",")
	if err := validateOptions(options); err != nil {
		return err
	}
	repo, err := repository.New(options.database, options.datasets)
	if err != nil {
		return err
	}
	defer repo.Close()
	report, err := exportCorpus(context.Background(), repo, options)
	if err != nil {
		return err
	}
	encoded, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return err
	}
	if err := writeNewFile(options.report, append(encoded, '\n')); err != nil {
		return err
	}
	fmt.Println(string(encoded))
	return nil
}

func validateOptions(options exportOptions) error {
	paths := []string{options.database, options.datasets, options.output, options.report}
	for _, value := range append(paths, options.generation) {
		if strings.TrimSpace(value) == "" {
			return fmt.Errorf("db, datasets, generation, out, and report are required")
		}
	}
	if options.expected < 0 {
		return fmt.Errorf("expected-sentences cannot be negative")
	}
	for _, value := range options.tables {
		if strings.TrimSpace(value) != value || value == "" {
			return fmt.Errorf("tables must contain nonempty dataset names without whitespace")
		}
	}
	return nil
}

func writeNewFile(path string, data []byte) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(data)
	closeErr := file.Close()
	if writeErr != nil {
		return writeErr
	}
	return closeErr
}
