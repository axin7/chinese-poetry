package siliconflow

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func testVector(first, second float64) []float64 {
	vector := make([]float64, EmbeddingDimension)
	vector[0] = first
	vector[1] = second
	return vector
}

func writeJSON(t *testing.T, writer http.ResponseWriter, value any) {
	t.Helper()
	writer.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(writer).Encode(value); err != nil {
		t.Fatalf("encode response: %v", err)
	}
}

func newServerClient(
	t *testing.T,
	handler http.HandlerFunc,
	configure func(*Config),
) *Client {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	config := DefaultConfig("secret")
	config.BaseURL = server.URL + "/v1"
	if configure != nil {
		configure(&config)
	}
	client, err := New(config, server.Client())
	if err != nil {
		t.Fatalf("New() error: %v", err)
	}
	t.Cleanup(client.Close)
	return client
}

func TestEmbedRequestAndNormalization(t *testing.T) {
	handler := func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/v1/embeddings" {
			t.Errorf("path = %q", request.URL.Path)
		}
		if got := request.Header.Get("Authorization"); got != "Bearer secret" {
			t.Errorf("authorization = %q", got)
		}
		var payload embeddingRequest
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if payload.Model != DefaultEmbeddingModel || payload.EncodingFormat != "float" {
			t.Errorf("unexpected payload: %+v", payload)
		}
		if !reflect.DeepEqual(payload.Input, []string{"思念家乡"}) {
			t.Errorf("input = %#v", payload.Input)
		}
		writeJSON(t, writer, map[string]any{"data": []any{
			map[string]any{"index": 0, "embedding": testVector(3, 4)},
		}})
	}
	client := newServerClient(t, handler, nil)
	vector, err := client.Embed(context.Background(), "思念家乡")
	if err != nil {
		t.Fatalf("Embed() error: %v", err)
	}
	if math.Abs(float64(vector[0])-0.6) > 1e-6 || math.Abs(float64(vector[1])-0.8) > 1e-6 {
		t.Fatalf("normalized prefix = %v", vector[:2])
	}
}

func TestEmbedManyRestoresIndexOrder(t *testing.T) {
	handler := func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(t, writer, map[string]any{"data": []any{
			map[string]any{"index": 1, "embedding": testVector(0, 2)},
			map[string]any{"index": 0, "embedding": testVector(2, 0)},
		}})
	}
	client := newServerClient(t, handler, nil)
	vectors, err := client.EmbedMany(context.Background(), []string{"甲", "乙"})
	if err != nil {
		t.Fatalf("EmbedMany() error: %v", err)
	}
	if vectors[0][0] != 1 || vectors[0][1] != 0 {
		t.Errorf("first vector prefix = %v", vectors[0][:2])
	}
	if vectors[1][0] != 0 || vectors[1][1] != 1 {
		t.Errorf("second vector prefix = %v", vectors[1][:2])
	}
}

func TestRerankUsesConfiguredModelAndDocumentOrder(t *testing.T) {
	documents := []string{"第一句译文", "第二句译文", "第三句译文"}
	handler := func(writer http.ResponseWriter, request *http.Request) {
		var payload rerankRequest
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if payload.Model != "custom-reranker" || payload.TopN != 1 {
			t.Errorf("unexpected payload: %+v", payload)
		}
		if payload.ReturnDocuments || !reflect.DeepEqual(payload.Documents, documents) {
			t.Errorf("documents changed: %#v", payload.Documents)
		}
		writeJSON(t, writer, map[string]any{"results": []any{
			map[string]any{"index": 1, "relevance_score": 0.91},
		}})
	}
	client := newServerClient(t, handler, func(config *Config) {
		config.RerankModel = "custom-reranker"
	})
	result, err := client.Rerank(context.Background(), "夜里想家", documents)
	if err != nil {
		t.Fatalf("Rerank() error: %v", err)
	}
	if result.Index != 1 || math.Abs(result.Score-0.91) > 1e-9 {
		t.Fatalf("result = %+v", result)
	}
}

func TestHTTPErrorIsTyped(t *testing.T) {
	handler := func(writer http.ResponseWriter, request *http.Request) {
		http.Error(writer, "rate limited", http.StatusTooManyRequests)
	}
	client := newServerClient(t, handler, nil)
	_, err := client.Embed(context.Background(), "查询")
	var httpErr *HTTPError
	if !errors.As(err, &httpErr) {
		t.Fatalf("error type = %T, want *HTTPError", err)
	}
	if httpErr.StatusCode != http.StatusTooManyRequests {
		t.Errorf("status = %d", httpErr.StatusCode)
	}
}

func TestTimeoutIsTypedAndNotRetried(t *testing.T) {
	var attempts atomic.Int32
	handler := func(writer http.ResponseWriter, request *http.Request) {
		attempts.Add(1)
		select {
		case <-request.Context().Done():
		case <-time.After(100 * time.Millisecond):
		}
	}
	client := newServerClient(t, handler, func(config *Config) {
		config.EmbeddingTimeout = 20 * time.Millisecond
	})
	_, err := client.Embed(context.Background(), "查询")
	var transportErr *TransportError
	if !errors.As(err, &transportErr) || !transportErr.Timeout() {
		t.Fatalf("error = %T %v, want timeout TransportError", err, err)
	}
	if attempts.Load() != 1 {
		t.Fatalf("attempts = %d, want 1", attempts.Load())
	}
}

func TestEmbeddingConcurrencyIsBounded(t *testing.T) {
	var active atomic.Int32
	var maximum atomic.Int32
	handler := func(writer http.ResponseWriter, request *http.Request) {
		current := active.Add(1)
		for current > maximum.Load() && !maximum.CompareAndSwap(maximum.Load(), current) {
		}
		time.Sleep(10 * time.Millisecond)
		active.Add(-1)
		writeJSON(t, writer, map[string]any{"data": []any{
			map[string]any{"index": 0, "embedding": testVector(1, 0)},
		}})
	}
	client := newServerClient(t, handler, func(config *Config) {
		config.EmbeddingConcurrency = 1
	})
	var group sync.WaitGroup
	for index := range 3 {
		group.Add(1)
		go func() {
			defer group.Done()
			if _, err := client.Embed(context.Background(), string(rune('甲'+index))); err != nil {
				t.Errorf("Embed() error: %v", err)
			}
		}()
	}
	group.Wait()
	if maximum.Load() != 1 {
		t.Fatalf("maximum concurrency = %d", maximum.Load())
	}
}

func TestEmbeddingAndRerankUseIndependentConcurrency(t *testing.T) {
	embedStarted := make(chan struct{})
	releaseEmbed := make(chan struct{})
	handler := func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/v1/embeddings" {
			close(embedStarted)
			<-releaseEmbed
			writeJSON(t, writer, map[string]any{"data": []any{
				map[string]any{"index": 0, "embedding": testVector(1, 0)},
			}})
			return
		}
		writeJSON(t, writer, map[string]any{"results": []any{
			map[string]any{"index": 0, "relevance_score": 0.7},
		}})
	}
	client := newServerClient(t, handler, nil)
	embedDone := make(chan error, 1)
	go func() {
		_, err := client.Embed(context.Background(), "查询")
		embedDone <- err
	}()
	<-embedStarted
	result, err := client.Rerank(context.Background(), "查询", []string{"文档"})
	close(releaseEmbed)
	if err != nil || result.Index != 0 {
		t.Fatalf("Rerank() = %+v, %v", result, err)
	}
	if err := <-embedDone; err != nil {
		t.Fatalf("Embed() error: %v", err)
	}
}

type closeTrackingTransport struct {
	base   http.RoundTripper
	closed atomic.Bool
}

func (transport *closeTrackingTransport) RoundTrip(
	request *http.Request,
) (*http.Response, error) {
	return transport.base.RoundTrip(request)
}

func (transport *closeTrackingTransport) CloseIdleConnections() {
	transport.closed.Store(true)
}

func TestCloseDoesNotCloseInjectedHTTPClient(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	defer server.Close()
	tracking := &closeTrackingTransport{base: server.Client().Transport}
	config := DefaultConfig("secret")
	config.BaseURL = server.URL
	client, err := New(config, &http.Client{Transport: tracking})
	if err != nil {
		t.Fatalf("New() error: %v", err)
	}
	client.Close()
	if tracking.closed.Load() {
		t.Fatal("injected HTTP transport was closed")
	}
	_, err = client.Embed(context.Background(), "查询")
	if !errors.Is(err, ErrClosed) {
		t.Fatalf("error = %v, want ErrClosed", err)
	}
}
