package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/limits"
)

type unreadBody struct{}

func (unreadBody) Read([]byte) (int, error) { panic("rate-rejected body must not be read") }
func (unreadBody) Close() error             { return nil }

func configuredAPI(t *testing.T, repo Repository, policy Policy) http.Handler {
	t.Helper()
	handler, err := NewConfigured("test-v1", &fakeSearch{}, repo, fakeHealth{}, policy)
	if err != nil {
		t.Fatal(err)
	}
	return handler
}

func TestSearchAdmissionPrecedesParsingAndIgnoresClientHeaders(t *testing.T) {
	now := time.Unix(100, 0)
	policy := DefaultPolicy()
	policy.Now = func() time.Time { return now }
	handler := configuredAPI(t, fakeRepo{}, policy)
	for range policy.SearchBurst {
		writer := httptest.NewRecorder()
		handler.ServeHTTP(writer, httptest.NewRequest(http.MethodPost, "/search",
			strings.NewReader("invalid JSON")))
		if writer.Code != http.StatusUnprocessableEntity {
			t.Fatalf("unexpected admitted status: %d", writer.Code)
		}
	}
	request := httptest.NewRequest(http.MethodPost, "/search", nil)
	request.Header.Set("X-Poetry-Client", "another-client")
	request.Header.Set("Authorization", "Bearer rotated-token")
	request.Body = unreadBody{}
	writer := httptest.NewRecorder()
	handler.ServeHTTP(writer, request)
	assertHTTPRejection(t, writer, 429, "search_rate_exceeded")
	now = now.Add(500 * time.Millisecond)
	writer = httptest.NewRecorder()
	handler.ServeHTTP(writer, httptest.NewRequest(http.MethodPost, "/search",
		strings.NewReader(`{"query":"test"}`)))
	if writer.Code != http.StatusOK {
		t.Fatalf("refilled request returned %d", writer.Code)
	}
}

func TestDetailCacheHitsStillConsumeAdmissionBudget(t *testing.T) {
	policy := DefaultPolicy()
	policy.Now = func() time.Time { return time.Unix(100, 0) }
	handler := configuredAPI(t, fakeRepo{}, policy)
	for range policy.DetailBurst {
		writer := httptest.NewRecorder()
		handler.ServeHTTP(writer, httptest.NewRequest(http.MethodGet, "/poems/tangsong:1", nil))
		if writer.Code != http.StatusOK {
			t.Fatalf("detail returned %d", writer.Code)
		}
	}
	writer := httptest.NewRecorder()
	handler.ServeHTTP(writer, httptest.NewRequest(http.MethodGet, "/poems/tangsong:1", nil))
	assertHTTPRejection(t, writer, 429, "detail_rate_exceeded")
	writer = httptest.NewRecorder()
	handler.ServeHTTP(writer, httptest.NewRequest(http.MethodPost, "/search",
		strings.NewReader(`{"query":"test"}`)))
	if writer.Code != http.StatusOK {
		t.Fatalf("detail bucket affected search: %d", writer.Code)
	}
}

func TestHTTPConcurrencyCapacityReturnsStructured503(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	handler := WithConcurrencyLimit(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(started)
		<-release
		w.WriteHeader(http.StatusOK)
	}), 1)
	done := make(chan struct{})
	go func() {
		handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/poems/1", nil))
		close(done)
	}()
	<-started
	writer := httptest.NewRecorder()
	handler.ServeHTTP(writer, httptest.NewRequest(http.MethodGet, "/poems/2", nil))
	close(release)
	<-done
	assertHTTPRejection(t, writer, 503, "http_capacity_exceeded")
}

func TestSearchPropagatesEmbeddingBudgetAndCapacityErrors(t *testing.T) {
	api := &API{}
	for _, rejection := range []*limits.Error{
		{Status: 429, Code: "embedding_budget_exceeded", Message: "rate limited", RetryAfter: 17},
		limits.Capacity("search_capacity_exceeded"),
	} {
		writer := httptest.NewRecorder()
		api.writeSearchError(writer, rejection)
		assertHTTPRejection(t, writer, rejection.Status, rejection.Code)
	}
}

func TestSingleflightWaitersStillConsumeDetailArrivalBudget(t *testing.T) {
	policy := DefaultPolicy()
	policy.DetailBurst = 2
	policy.Now = func() time.Time { return time.Unix(100, 0) }
	repo := &countedRepo{started: make(chan struct{}), release: make(chan struct{})}
	handler := configuredAPI(t, repo, policy)
	done := make(chan *httptest.ResponseRecorder, 2)
	serve := func(ctx context.Context) {
		writer := httptest.NewRecorder()
		request := httptest.NewRequest(http.MethodGet, "/poems/tangsong:1", nil).WithContext(ctx)
		handler.ServeHTTP(writer, request)
		done <- writer
	}
	go serve(context.Background())
	<-repo.started
	ctx := &enteredContext{Context: context.Background(), entered: make(chan struct{})}
	go serve(ctx)
	<-ctx.entered
	writer := httptest.NewRecorder()
	handler.ServeHTTP(writer, httptest.NewRequest(http.MethodGet, "/poems/tangsong:1", nil))
	assertHTTPRejection(t, writer, 429, "detail_rate_exceeded")
	close(repo.release)
	for range 2 {
		if result := <-done; result.Code != http.StatusOK {
			t.Fatalf("admitted waiter returned %d", result.Code)
		}
	}
	if repo.calls.Load() != 1 {
		t.Fatalf("singleflight started %d reads", repo.calls.Load())
	}
}

func assertHTTPRejection(t *testing.T, writer *httptest.ResponseRecorder, status int, code string) {
	t.Helper()
	var payload struct {
		Code       string `json:"code"`
		Message    string `json:"message"`
		RetryAfter int    `json:"retry_after"`
		Detail     string `json:"detail"`
	}
	if err := json.Unmarshal(writer.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if writer.Code != status || payload.Code != code || payload.Message == "" ||
		payload.RetryAfter < 1 || payload.Detail != payload.Message ||
		writer.Header().Get("Retry-After") == "" {
		t.Fatalf("invalid rejection: %d %s", writer.Code, writer.Body.String())
	}
}
