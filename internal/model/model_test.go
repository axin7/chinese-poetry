package model

import (
	"encoding/json"
	"math"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func stringPointer(value string) *string {
	return &value
}

func unitVector() []float32 {
	vector := make([]float32, VectorDimension)
	vector[0] = 1
	return vector
}

func TestSearchRequestAcceptsTextAndVectorTogether(t *testing.T) {
	request := SearchRequest{
		Query:            stringPointer("  想家  "),
		Vector:           unitVector(),
		EmbeddingProfile: EmbeddingProfile,
	}

	if err := request.Validate(); err != nil {
		t.Fatal(err)
	}
	if request.Query == nil || *request.Query != "想家" {
		t.Fatalf("query was not normalized: %#v", request.Query)
	}
	if request.Vector[0] != 1 {
		t.Fatalf("vector was not normalized: %v", request.Vector[:2])
	}
}

func TestSearchRequestRequiresInput(t *testing.T) {
	request := SearchRequest{}
	if err := request.Validate(); err == nil {
		t.Fatal("expected a missing input error")
	}
}

func TestSearchRequestValidatesQuery(t *testing.T) {
	tests := []string{" \n ", strings.Repeat("思", MaxQueryCharacters+1)}
	for _, query := range tests {
		request := SearchRequest{Query: &query}
		if err := request.Validate(); err == nil {
			t.Fatalf("expected query %q to fail", query)
		}
	}
}

func TestVectorRequiresMatchingProfile(t *testing.T) {
	request := SearchRequest{Vector: unitVector()}
	if err := request.Validate(); err == nil {
		t.Fatal("expected a missing profile error")
	}

	request = SearchRequest{Vector: unitVector(), EmbeddingProfile: "other"}
	if err := request.Validate(); err == nil || err.Error() != "embedding_profile_mismatch" {
		t.Fatalf("unexpected mismatch error: %v", err)
	}
}

func TestNormalizeVectorUsesStableFloat32Math(t *testing.T) {
	vector, err := NormalizeVector([]float32{math.MaxFloat32, math.MaxFloat32}, 2)
	if err != nil {
		t.Fatal(err)
	}
	want := float32(math.Sqrt(0.5))
	if vector[0] != want || vector[1] != want {
		t.Fatalf("unexpected normalized vector: %v", vector)
	}
}

func TestNormalizeVectorRejectsInvalidValues(t *testing.T) {
	tests := [][]float32{
		{0, 0},
		{float32(math.Inf(1)), 1},
		{float32(math.NaN()), 1},
	}
	for _, vector := range tests {
		if _, err := NormalizeVector(vector, 2); err == nil {
			t.Fatalf("expected vector to fail: %v", vector)
		}
	}
	if _, err := NormalizeVector([]float32{1}, 2); err == nil {
		t.Fatal("expected dimension mismatch")
	}
}

func TestNormalizeVectorCanonicalizesNegativeZero(t *testing.T) {
	vector, err := NormalizeVector([]float32{float32(math.Copysign(0, -1)), 2}, 2)
	if err != nil {
		t.Fatal(err)
	}
	if math.Signbit(float64(vector[0])) {
		t.Fatal("negative zero was not canonicalized")
	}
}

func TestSearchFiltersValidateAndCanonicalizeTables(t *testing.T) {
	if len(supportedTables) != 29 {
		t.Fatalf("supported table count changed: %d", len(supportedTables))
	}
	filters := SearchFilters{Tables: []string{"tangsong", "songci", "tangsong"}}
	if err := filters.Validate(); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(filters.Tables, []string{"songci", "tangsong"}) {
		t.Fatalf("unexpected tables: %v", filters.Tables)
	}

	for _, tables := range [][]string{nil, {"unknown"}} {
		filters := SearchFilters{Tables: tables}
		if err := filters.Validate(); err == nil {
			t.Fatalf("expected tables to fail: %v", tables)
		}
	}
}

func TestJSONRejectsNonNumericAndOverflowingVectors(t *testing.T) {
	for _, payload := range []string{
		`{"vector":[true]}`,
		`{"vector":[null]}`,
		`{"vector":["1"]}`,
		`{"vector":[3.5e38]}`,
	} {
		var request SearchRequest
		if err := json.Unmarshal([]byte(payload), &request); err == nil {
			t.Fatalf("expected JSON to fail: %s", payload)
		}
	}
}

func TestJSONRejectsExplicitNullAndUnknownFields(t *testing.T) {
	for _, payload := range []string{
		`{"query":null,"vector":[1]}`,
		`{"query":"想家","vector":null}`,
		`{"query":"想家","limit":1}`,
	} {
		var request SearchRequest
		if err := json.Unmarshal([]byte(payload), &request); err == nil {
			t.Fatalf("expected JSON to fail: %s", payload)
		}
	}
}

func TestResponsesUsePythonFieldNames(t *testing.T) {
	response := SearchResponse{
		Match: SearchMatch{Original: "原文", Translation: "译文", SentenceIndex: 1},
		Poem:  PoemSummary{ID: "id", Title: "标题", DetailURL: "/poems/id"},
	}
	payload, err := json.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"match":{"original":"原文","translation":"译文","sentence_index":1,"score":0},` +
		`"poem":{"id":"id","title":"标题","author":null,"detail_url":"/poems/id"}}`
	if string(payload) != want {
		t.Fatalf("unexpected response JSON: %s", payload)
	}
}

func TestWorkDetailResponseUsesExactFields(t *testing.T) {
	response := WorkDetailResponse{
		ID: "id", Dataset: "shijing", Title: "关雎",
		Original: []string{"原文"}, Translation: []string{"译文"},
		Interpretations: []string{},
	}
	payload, err := json.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(payload, &fields); err != nil {
		t.Fatal(err)
	}
	want := []string{"author", "dataset", "id", "interpretations", "original", "title", "translation"}
	got := make([]string, 0, len(fields))
	for field := range fields {
		got = append(got, field)
	}
	sort.Strings(got)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected fields: %v", got)
	}
}
