package siliconflow

import (
	"context"
	"errors"
	"math"
	"net/http"
	"testing"
)

func TestEmbedRejectsInvalidVectors(t *testing.T) {
	tests := []struct {
		name   string
		vector []float64
	}{
		{name: "wrong dimensions", vector: []float64{1, 2}},
		{name: "zero vector", vector: testVector(0, 0)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			handler := func(writer http.ResponseWriter, request *http.Request) {
				writeJSON(t, writer, map[string]any{"data": []any{
					map[string]any{"index": 0, "embedding": test.vector},
				}})
			}
			client := newServerClient(t, handler, nil)
			_, err := client.Embed(context.Background(), "查询")
			var responseErr *ResponseError
			if !errors.As(err, &responseErr) {
				t.Fatalf("error type = %T, want *ResponseError", err)
			}
		})
	}
}

func TestNormalizeRejectsNonFiniteValues(t *testing.T) {
	values := testVector(1, 0)
	values[4] = math.Inf(1)
	_, err := normalize(values)
	var responseErr *ResponseError
	if !errors.As(err, &responseErr) {
		t.Fatalf("error type = %T, want *ResponseError", err)
	}
}

func TestEmbedRejectsDuplicateIndex(t *testing.T) {
	handler := func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(t, writer, map[string]any{"data": []any{
			map[string]any{"index": 0, "embedding": testVector(1, 0)},
			map[string]any{"index": 0, "embedding": testVector(0, 1)},
		}})
	}
	client := newServerClient(t, handler, nil)
	_, err := client.EmbedMany(context.Background(), []string{"甲", "乙"})
	var responseErr *ResponseError
	if !errors.As(err, &responseErr) {
		t.Fatalf("error type = %T, want *ResponseError", err)
	}
}

func TestRerankRejectsOutOfRangeIndex(t *testing.T) {
	handler := func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(t, writer, map[string]any{"results": []any{
			map[string]any{"index": 3, "relevance_score": 0.8},
		}})
	}
	client := newServerClient(t, handler, nil)
	_, err := client.Rerank(context.Background(), "查询", []string{"唯一文档"})
	var responseErr *ResponseError
	if !errors.As(err, &responseErr) {
		t.Fatalf("error type = %T, want *ResponseError", err)
	}
}

func TestResponseRejectsTrailingJSON(t *testing.T) {
	handler := func(writer http.ResponseWriter, request *http.Request) {
		_, _ = writer.Write([]byte(`{"data": []} {"extra": true}`))
	}
	client := newServerClient(t, handler, nil)
	_, err := client.Embed(context.Background(), "查询")
	var responseErr *ResponseError
	if !errors.As(err, &responseErr) {
		t.Fatalf("error type = %T, want *ResponseError", err)
	}
}

func TestInputValidationFailsBeforeRequest(t *testing.T) {
	client, err := New(DefaultConfig("secret"), &http.Client{})
	if err != nil {
		t.Fatalf("New() error: %v", err)
	}
	defer client.Close()
	if _, err := client.EmbedMany(context.Background(), nil); err == nil {
		t.Fatal("EmbedMany() accepted empty input")
	}
	if _, err := client.Rerank(context.Background(), "", []string{"文档"}); err == nil {
		t.Fatal("Rerank() accepted empty query")
	}
}
