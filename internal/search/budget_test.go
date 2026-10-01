package search

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/chinese-poetry/chinese-poetry/internal/limits"
	"github.com/chinese-poetry/chinese-poetry/internal/model"
	"github.com/chinese-poetry/chinese-poetry/internal/qdrantstore"
	"github.com/chinese-poetry/chinese-poetry/internal/siliconflow"
)

func budgetModels(t *testing.T, attempts *atomic.Int32) *siliconflow.Client {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		attempts.Add(1)
		_ = json.NewEncoder(w).Encode(map[string]any{"data": []any{
			map[string]any{"index": 0, "embedding": unitVector()},
		}})
	}))
	t.Cleanup(server.Close)
	config := siliconflow.DefaultConfig("secret")
	config.BaseURL = server.URL
	config.EmbeddingRequestsPerMinute = 1
	client, err := siliconflow.New(config, server.Client())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(client.Close)
	return client
}

func TestResponseAndVectorCacheHitsDoNotConsumeEmbeddingBudget(t *testing.T) {
	var attempts atomic.Int32
	models := budgetModels(t, &attempts)
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1)}}
	service := newTestService(t, false, models, store)
	query := "first query"
	for _, request := range []model.SearchRequest{
		{Query: &query}, {Query: &query},
		{Query: &query, Filters: &model.SearchFilters{Tables: []string{"tangsong"}}},
	} {
		if _, err := service.Search(context.Background(), request); err != nil {
			t.Fatal(err)
		}
	}
	query = "new query"
	_, err := service.Search(context.Background(), model.SearchRequest{Query: &query})
	var rejection *limits.Error
	if !errors.As(err, &rejection) || rejection.Code != "embedding_budget_exceeded" ||
		attempts.Load() != 1 || store.calls != 2 {
		t.Fatalf("cache/budget boundary failed: %v attempts=%d searches=%d",
			err, attempts.Load(), store.calls)
	}
}

func TestSearchCapacityCoversEmbeddingOperation(t *testing.T) {
	models := &blockingModels{started: make(chan struct{}), release: make(chan struct{})}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1)}}
	service := newTestService(t, false, models, store)
	service.slots = make(chan struct{}, 1)
	first, second := "first query", "second query"
	done := make(chan error, 1)
	go func() {
		_, err := service.Search(context.Background(), model.SearchRequest{Query: &first})
		done <- err
	}()
	<-models.started
	_, err := service.Search(context.Background(), model.SearchRequest{Query: &second})
	var rejection *limits.Error
	if !errors.As(err, &rejection) || rejection.Code != "search_capacity_exceeded" ||
		!errors.Is(err, ErrUnavailable) || models.calls.Load() != 1 {
		t.Fatalf("search exceeded cold-operation capacity: %v", err)
	}
	close(models.release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}
