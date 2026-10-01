package siliconflow

import (
	"context"
	"errors"
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/limits"
)

func TestFailedEmbeddingAttemptsConsumeRollingBudget(t *testing.T) {
	var attempts atomic.Int32
	client := newServerClient(t, func(w http.ResponseWriter, _ *http.Request) {
		attempts.Add(1)
		http.Error(w, "upstream failure", http.StatusServiceUnavailable)
	}, func(config *Config) { config.EmbeddingRequestsPerMinute = 2 })
	now := time.Unix(100, 0)
	client.embedBudget = limits.NewMinuteBudget(2, func() time.Time { return now })
	for range 2 {
		if _, err := client.Embed(context.Background(), "query"); err == nil {
			t.Fatal("expected provider failure")
		}
	}
	_, err := client.Embed(context.Background(), "another query")
	var rejection *limits.Error
	if !errors.As(err, &rejection) || rejection.Code != "embedding_budget_exceeded" ||
		attempts.Load() != 2 {
		t.Fatalf("budget did not protect provider: %v attempts=%d", err, attempts.Load())
	}
	now = now.Add(time.Minute)
	_, _ = client.Embed(context.Background(), "query")
	if attempts.Load() != 3 {
		t.Fatalf("expired budget did not admit request: %d", attempts.Load())
	}
}

func TestInvalidEmbeddingDoesNotConsumeProviderBudget(t *testing.T) {
	var attempts atomic.Int32
	client := newServerClient(t, func(w http.ResponseWriter, _ *http.Request) {
		attempts.Add(1)
		http.Error(w, "failure", http.StatusServiceUnavailable)
	}, func(config *Config) { config.EmbeddingRequestsPerMinute = 1 })
	_, _ = client.Embed(context.Background(), "")
	_, err := client.Embed(context.Background(), "query")
	var upstream *HTTPError
	if !errors.As(err, &upstream) || attempts.Load() != 1 {
		t.Fatalf("invalid input consumed provider budget: %v attempts=%d", err, attempts.Load())
	}
}
