package repository

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
)

var identifierPattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

type datasetsFile struct {
	Datasets map[string]datasetConfig `json:"datasets"`
}

type datasetConfig struct {
	Name          string              `json:"name"`
	Tag           string              `json:"tag"`
	ObjectRecords objectRecordsConfig `json:"object_records"`
}

type objectRecordsConfig struct {
	AlignedFields []string `json:"aligned_fields"`
}

func loadDatasets(path string) (map[string]datasetMetadata, []string, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, fmt.Errorf("read datasets config: %w", err)
	}
	var config datasetsFile
	if err := json.Unmarshal(content, &config); err != nil {
		return nil, nil, fmt.Errorf("decode datasets config: %w", err)
	}
	if len(config.Datasets) == 0 {
		return nil, nil, fmt.Errorf("datasets config is empty")
	}
	result := make(map[string]datasetMetadata, len(config.Datasets))
	order := make([]string, 0, len(config.Datasets))
	for table, raw := range config.Datasets {
		metadata, err := parseDataset(table, raw)
		if err != nil {
			return nil, nil, err
		}
		result[table] = metadata
		order = append(order, table)
	}
	sort.Strings(order)
	return result, order, nil
}

func parseDataset(table string, raw datasetConfig) (datasetMetadata, error) {
	if !identifierPattern.MatchString(table) {
		return datasetMetadata{}, fmt.Errorf("invalid dataset table %q", table)
	}
	if !identifierPattern.MatchString(raw.Tag) {
		return datasetMetadata{}, fmt.Errorf("dataset %s has invalid tag", table)
	}
	for _, field := range raw.ObjectRecords.AlignedFields {
		if !identifierPattern.MatchString(field) {
			return datasetMetadata{}, fmt.Errorf("dataset %s has invalid aligned field", table)
		}
	}
	name := raw.Name
	if name == "" {
		name = table
	}
	return datasetMetadata{
		Table: table, Name: name, ContentField: raw.Tag,
		AlignedFields: append([]string(nil), raw.ObjectRecords.AlignedFields...),
	}, nil
}
