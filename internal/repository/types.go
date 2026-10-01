package repository

import "errors"

var (
	ErrInvalidLocator = errors.New("invalid locator")
	ErrNotFound       = errors.New("record not found")
	ErrDataIntegrity  = errors.New("data integrity error")
)

type Locator struct {
	Dataset         string `json:"dataset"`
	SourceRowID     int64  `json:"source_row_id"`
	RawIndex        int    `json:"raw_index"`
	NormalizedIndex int    `json:"normalized_index"`
	WorkID          string `json:"work_id"`
}

type IndexableSentence struct {
	Dataset         string `json:"dataset"`
	SourceRowID     int64  `json:"source_row_id"`
	RawIndex        int    `json:"raw_index"`
	NormalizedIndex int    `json:"normalized_index"`
	WorkID          string `json:"work_id"`
	Original        string `json:"original"`
	Translation     string `json:"translation"`
}

func (sentence IndexableSentence) Locator() Locator {
	return Locator{
		Dataset:         sentence.Dataset,
		SourceRowID:     sentence.SourceRowID,
		RawIndex:        sentence.RawIndex,
		NormalizedIndex: sentence.NormalizedIndex,
		WorkID:          sentence.WorkID,
	}
}

type SentenceMatch struct {
	Locator           Locator `json:"locator"`
	Original          string  `json:"original"`
	Translation       string  `json:"translation"`
	WorkSentenceIndex int     `json:"work_sentence_index"`
}

type SearchLookup struct {
	Match   SentenceMatch `json:"match"`
	WorkID  string        `json:"work_id"`
	Dataset string        `json:"dataset"`
	Title   string        `json:"title"`
	Author  *string       `json:"author"`
}

type PoetryWork struct {
	WorkID          string   `json:"work_id"`
	Dataset         string   `json:"dataset"`
	Title           string   `json:"title"`
	Author          *string  `json:"author"`
	Original        []string `json:"original"`
	Translation     []string `json:"translation"`
	SourceRowIDs    []int64  `json:"source_row_ids"`
	Interpretations []string `json:"interpretations"`
}

type datasetMetadata struct {
	Table         string
	Name          string
	ContentField  string
	AlignedFields []string
}

type alignedSentence struct {
	RawIndex        int
	NormalizedIndex int
	Original        string
	Translation     string
}

type tableSchema struct {
	Columns   []string
	ColumnSet map[string]struct{}
	HasParts  bool
}

type rowData map[string]any
