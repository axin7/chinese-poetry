package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/chinese-poetry/chinese-poetry/internal/model"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
)

type fakeSearch struct {
	request model.SearchRequest
}

func (f *fakeSearch) Search(
	_ context.Context,
	request model.SearchRequest,
) (model.SearchResponse, error) {
	f.request = request
	return model.SearchResponse{
		Match: model.SearchMatch{
			Original: "古句", Translation: "译文", SentenceIndex: 2, Score: 0.875,
		},
		Poem: model.PoemSummary{
			ID: "tangsong:1", Title: "测试诗", DetailURL: "/poems/tangsong:1?generation=test-v1",
		},
	}, nil
}

type fakeRepo struct{ empty bool }

func (fakeRepo) Health(context.Context) error { return nil }

func (repo fakeRepo) GetWorkByID(
	_ context.Context,
	workID string,
) (repository.PoetryWork, error) {
	if repo.empty {
		return repository.PoetryWork{WorkID: workID}, nil
	}
	author := "作者"
	return repository.PoetryWork{
		WorkID: workID, Dataset: "tangsong", Title: "测试诗", Author: &author,
		Original: []string{"古句", "后句"}, Translation: []string{"译文", "后句译文"},
		Interpretations: []string{"赏析"},
	}, nil
}

type failingRepo struct{ err error }

func (repo failingRepo) Health(context.Context) error { return nil }

func (repo failingRepo) GetWorkByID(
	context.Context,
	string,
) (repository.PoetryWork, error) {
	return repository.PoetryWork{}, repo.err
}

type fakeHealth struct{ err error }

func (f fakeHealth) Health(context.Context) error { return f.err }

func TestSearchReturnsOnlyIDAndVerse(t *testing.T) {
	searcher := &fakeSearch{}
	handler := New("test-v1", searcher, fakeRepo{}, fakeHealth{})
	bodies := []string{
		`{"query":"晚上想家"}`,
		`{"vector":[` + strings.Repeat("0,", 1023) + `1],` +
			`"embedding_profile":"sf-bge-m3-1024-v1"}`,
	}
	for _, body := range bodies {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(
			http.MethodPost, "/search", strings.NewReader(body),
		))
		if recorder.Code != http.StatusOK {
			t.Fatalf("unexpected status %d: %s", recorder.Code, recorder.Body.String())
		}
		var response map[string]any
		if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
			t.Fatal(err)
		}
		want := map[string]any{"id": "tangsong:1", "original": "古句"}
		if !reflect.DeepEqual(response, want) {
			t.Fatalf("unexpected search fields: %v", response)
		}
		if recorder.Header().Get("X-Poetry-Generation") != "test-v1" ||
			recorder.Header().Get("X-Poetry-Score") != "0.875" {
			t.Fatalf("missing search metadata: %v", recorder.Header())
		}
		if searcher.request.Query != nil && *searcher.request.Query != "晚上想家" {
			t.Fatalf("unexpected query: %s", *searcher.request.Query)
		}
	}
}

func TestSearchRejectsBodyLargerThan64KiB(t *testing.T) {
	handler := New("test-v1", &fakeSearch{}, fakeRepo{}, fakeHealth{})
	request := httptest.NewRequest(http.MethodPost, "/search",
		strings.NewReader(strings.Repeat("x", int(MaxSearchBodyBytes)+1)))
	recorder := httptest.NewRecorder()

	handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("unexpected status %d", recorder.Code)
	}
}

func TestSearchRejectsUnknownAndNullFields(t *testing.T) {
	handler := New("test-v1", &fakeSearch{}, fakeRepo{}, fakeHealth{})
	for _, body := range []string{`{"query":"x","limit":2}`, `{"query":null}`} {
		request := httptest.NewRequest(http.MethodPost, "/search", strings.NewReader(body))
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, request)
		if recorder.Code != http.StatusUnprocessableEntity {
			t.Fatalf("body %s returned %d", body, recorder.Code)
		}
	}
}

func TestPoemDetailAcceptsIDWithOptionalCurrentGeneration(t *testing.T) {
	handler := New("test-v1", &fakeSearch{}, fakeRepo{}, fakeHealth{})
	want := `{"id":"tangsong:1","dataset":"tangsong","title":"测试诗",` +
		`"author":"作者","original":["古句","后句"],` +
		`"translation":["译文","后句译文"],"interpretations":["赏析"]}`
	for _, query := range []string{"", "?generation=test-v1"} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(
			http.MethodGet, "/poems/tangsong:1"+query, nil,
		))
		if recorder.Code != http.StatusOK || strings.TrimSpace(recorder.Body.String()) != want {
			t.Fatalf("query %q returned %d: %s", query, recorder.Code, recorder.Body.String())
		}
	}
}

func TestPoemDetailRejectsStaleOrAmbiguousGeneration(t *testing.T) {
	handler := New("test-v1", &fakeSearch{}, failingRepo{repository.ErrDataIntegrity}, fakeHealth{})
	for _, query := range []string{
		"generation=old", "generation=", "generation=test-v1&generation=old",
	} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(
			http.MethodGet, "/poems/tangsong:1?"+query, nil,
		))
		if recorder.Code != http.StatusNotFound ||
			!strings.Contains(recorder.Body.String(), "语料版本不存在") {
			t.Fatalf("query %q returned %d: %s", query, recorder.Code, recorder.Body.String())
		}
	}
}

func TestPoemDetailReturnsEmptyArraysForMissingContent(t *testing.T) {
	handler := New("test-v1", &fakeSearch{}, fakeRepo{empty: true}, fakeHealth{})
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/poems/tangsong:1", nil))
	var response map[string]json.RawMessage
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"original", "translation", "interpretations"} {
		if string(response[field]) != "[]" {
			t.Errorf("%s must be an array, got %s", field, response[field])
		}
	}
}

func TestPoemDetailClassifiesRepositoryErrors(t *testing.T) {
	tests := []struct {
		err    error
		status int
	}{
		{err: repository.ErrNotFound, status: http.StatusNotFound},
		{err: repository.ErrInvalidLocator, status: http.StatusNotFound},
		{err: repository.ErrDataIntegrity, status: http.StatusInternalServerError},
		{err: errors.New("database closed"), status: http.StatusServiceUnavailable},
	}
	for _, test := range tests {
		handler := New("test-v1", &fakeSearch{}, failingRepo{test.err}, fakeHealth{})
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(
			http.MethodGet, "/poems/tangsong:1", nil,
		))
		if recorder.Code != test.status {
			t.Fatalf("error %v returned %d", test.err, recorder.Code)
		}
	}
}

func TestPoemDetailRequiresID(t *testing.T) {
	handler := New("test-v1", &fakeSearch{}, fakeRepo{}, fakeHealth{})
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/poems/", nil))
	if recorder.Code != http.StatusNotFound {
		t.Fatalf("missing poem ID returned %d", recorder.Code)
	}
}

func TestHealthReturnsGeneration(t *testing.T) {
	handler := New("test-v1", &fakeSearch{}, fakeRepo{}, fakeHealth{})
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/health", nil))

	if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), "test-v1") {
		t.Fatalf("unexpected health response: %s", recorder.Body.String())
	}
}
