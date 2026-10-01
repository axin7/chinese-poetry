package model

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"unicode/utf8"
)

const (
	VectorDimension    = 1024
	EmbeddingProfile   = "sf-bge-m3-1024-v1"
	MaxQueryCharacters = 512
)

var supportedTables = map[string]struct{}{
	"wudai_huajianji": {}, "wudai_nantang": {}, "yuanqu": {},
	"tangsong": {}, "mengzi": {}, "songci": {}, "youmengying": {},
	"yudingquantangshi": {}, "caocao": {}, "chuci": {},
	"shuimotangshi": {}, "nalanxingde": {}, "lunyu": {}, "shijing": {},
	"daxue": {}, "zhongyong": {}, "baijiaxing": {}, "dizigui": {},
	"guwenguanzhi": {}, "qianjiashi": {}, "qianziwen": {},
	"sanzijing_new": {}, "sanzijing_traditional": {}, "shenglvqimeng": {},
	"tangshisanbaishou": {}, "wenzimengqiu": {}, "youxueqionglin": {},
	"zengguangxianwen": {}, "zhuzijiaxun": {},
}

type SearchFilters struct {
	Tables []string `json:"tables"`
}

type SearchRequest struct {
	Query            *string        `json:"query,omitempty"`
	Vector           []float32      `json:"vector,omitempty"`
	EmbeddingProfile string         `json:"embedding_profile,omitempty"`
	Filters          *SearchFilters `json:"filters,omitempty"`
}

type searchRequest SearchRequest

func (request *SearchRequest) UnmarshalJSON(data []byte) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for _, name := range []string{"query", "vector"} {
		if raw, exists := fields[name]; exists && bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			return fmt.Errorf("%s must not be null", name)
		}
	}
	if raw, exists := fields["vector"]; exists {
		if err := rejectNullVectorValues(raw); err != nil {
			return err
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var decoded searchRequest
	if err := decoder.Decode(&decoded); err != nil {
		return err
	}
	*request = SearchRequest(decoded)
	return nil
}

func rejectNullVectorValues(data []byte) error {
	var values []json.RawMessage
	if err := json.Unmarshal(data, &values); err != nil {
		return err
	}
	for index, value := range values {
		if bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return fmt.Errorf("vector[%d] must be a number", index)
		}
	}
	return nil
}

func (request *SearchRequest) Validate() error {
	if request.Query != nil {
		query := strings.TrimSpace(*request.Query)
		if query == "" {
			return fmt.Errorf("query must not be empty")
		}
		if utf8.RuneCountInString(query) > MaxQueryCharacters {
			return fmt.Errorf("query must not exceed %d characters", MaxQueryCharacters)
		}
		request.Query = &query
	}
	if request.Vector != nil {
		vector, err := NormalizeVector(request.Vector, VectorDimension)
		if err != nil {
			return err
		}
		request.Vector = vector
		if request.EmbeddingProfile == "" {
			return fmt.Errorf("embedding_profile is required with vector")
		}
	}
	if request.EmbeddingProfile != "" && request.EmbeddingProfile != EmbeddingProfile {
		return fmt.Errorf("embedding_profile_mismatch")
	}
	if request.Query == nil && request.Vector == nil {
		return fmt.Errorf("query or vector is required")
	}
	if request.Filters != nil {
		return request.Filters.Validate()
	}
	return nil
}

func NormalizeVector(values []float32, dimension int) ([]float32, error) {
	if dimension <= 0 {
		return nil, fmt.Errorf("vector dimension must be positive")
	}
	if len(values) != dimension {
		return nil, fmt.Errorf("vector must contain exactly %d values", dimension)
	}
	scale, err := vectorScale(values)
	if err != nil {
		return nil, err
	}
	if scale == 0 {
		return nil, fmt.Errorf("vector must not be zero")
	}
	norm := scaledNorm(values, scale)
	result := make([]float32, len(values))
	for index, value := range values {
		normalized := float32((float64(value) / scale) / norm)
		if normalized == 0 {
			normalized = 0
		}
		result[index] = normalized
	}
	return result, nil
}

func vectorScale(values []float32) (float64, error) {
	var scale float64
	for index, value := range values {
		number := float64(value)
		if math.IsNaN(number) || math.IsInf(number, 0) {
			return 0, fmt.Errorf("vector[%d] must be finite", index)
		}
		absolute := math.Abs(number)
		if absolute > scale {
			scale = absolute
		}
	}
	return scale, nil
}

func scaledNorm(values []float32, scale float64) float64 {
	var sum, compensation float64
	for _, value := range values {
		scaled := float64(value) / scale
		term := scaled*scaled - compensation
		next := sum + term
		compensation = (next - sum) - term
		sum = next
	}
	return math.Sqrt(sum)
}

func (filters *SearchFilters) Validate() error {
	if len(filters.Tables) == 0 {
		return fmt.Errorf("filters.tables must be a non-empty array")
	}
	unique := make(map[string]struct{}, len(filters.Tables))
	for _, table := range filters.Tables {
		if _, exists := supportedTables[table]; !exists {
			return fmt.Errorf("unsupported table: %s", table)
		}
		unique[table] = struct{}{}
	}
	filters.Tables = filters.Tables[:0]
	for table := range unique {
		filters.Tables = append(filters.Tables, table)
	}
	sort.Strings(filters.Tables)
	return nil
}

type SearchMatch struct {
	Original      string  `json:"original"`
	Translation   string  `json:"translation"`
	SentenceIndex int     `json:"sentence_index"`
	Score         float32 `json:"score"`
}

type PoemSummary struct {
	ID        string  `json:"id"`
	Title     string  `json:"title"`
	Author    *string `json:"author"`
	DetailURL string  `json:"detail_url"`
}

type SearchResponse struct {
	Match SearchMatch `json:"match"`
	Poem  PoemSummary `json:"poem"`
}

type WorkDetailResponse struct {
	ID              string   `json:"id"`
	Dataset         string   `json:"dataset"`
	Title           string   `json:"title"`
	Author          *string  `json:"author"`
	Original        []string `json:"original"`
	Translation     []string `json:"translation"`
	Interpretations []string `json:"interpretations"`
}
