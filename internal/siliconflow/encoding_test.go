package siliconflow

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strings"
	"testing"
)

func TestEmbedManyAcceptsBase64AndNumericVectorsInIndexOrder(t *testing.T) {
	handler := func(writer http.ResponseWriter, request *http.Request) {
		writeJSON(t, writer, map[string]any{"data": []any{
			map[string]any{"index": 2, "embedding": testVector(0, 1)},
			map[string]any{"index": 1, "embedding": encodedVector(testVector(-1, 0))},
			map[string]any{"index": 0, "embedding": encodedVector(testVector(3, 4))},
		}})
	}
	client := newServerClient(t, handler, nil)
	vectors, err := client.EmbedMany(context.Background(), []string{"first", "second", "third"})
	if err != nil {
		t.Fatalf("base64 embedding failed: %v", err)
	}
	if len(vectors) != 3 {
		t.Fatalf("vector count = %d", len(vectors))
	}
	for index, vector := range vectors {
		if len(vector) != EmbeddingDimension {
			t.Fatalf("vector %d dimension = %d", index, len(vector))
		}
	}
	if math.Abs(float64(vectors[0][0])-0.6) > 1e-6 ||
		math.Abs(float64(vectors[0][1])-0.8) > 1e-6 ||
		vectors[1][0] != -1 || vectors[2][1] != 1 {
		t.Fatal("little-endian decoding, normalization or input order changed")
	}
}

func TestBase64EmbeddingsRejectInvalidValues(t *testing.T) {
	tests := map[string]struct{ value, reason string }{
		"invalid_base64":      {"not:base64", "valid base64"},
		"empty":               {"", "1024"},
		"invalid_byte_length": {base64.StdEncoding.EncodeToString([]byte{1, 2, 3}), "1024"},
		"wrong_dimension":     {encodedVector([]float64{1, 2}), "1024"},
		"zero_vector":         {encodedVector(testVector(0, 0)), "zero vector"},
		"nan":                 {encodedVector(testVector(math.NaN(), 1)), "finite"},
		"positive_infinity":   {encodedVector(testVector(math.Inf(1), 1)), "finite"},
		"negative_infinity":   {encodedVector(testVector(math.Inf(-1), 1)), "finite"},
	}
	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			_, err := parseEncodedTestResponse(t, []any{
				map[string]any{"index": 0, "embedding": test.value},
			}, 1)
			var responseError *ResponseError
			if !errors.As(err, &responseError) || !strings.Contains(responseError.Reason, test.reason) {
				t.Fatalf("error = %v, want ResponseError", err)
			}
		})
	}
}

func TestEmbedBase64EncodingIsOptIn(t *testing.T) {
	handler := func(writer http.ResponseWriter, request *http.Request) {
		var payload embeddingRequest
		if err := json.NewDecoder(request.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload.EncodingFormat != "base64" || payload.Model != DefaultEmbeddingModel {
			t.Errorf("unexpected embedding config: encoding=%q model=%q",
				payload.EncodingFormat, payload.Model)
		}
		writeJSON(t, writer, map[string]any{"data": []any{
			map[string]any{"index": 0, "embedding": encodedVector(testVector(1, 0))},
		}})
	}
	client := newServerClient(t, handler, func(config *Config) {
		config.EmbeddingEncoding = "base64"
	})
	vector, err := client.Embed(context.Background(), "original translated text")
	if err != nil || len(vector) != EmbeddingDimension || vector[0] != 1 {
		t.Fatalf("base64 opt-in failed: dimension=%d error=%v", len(vector), err)
	}
}

func TestEmbeddingEncodingConfiguration(t *testing.T) {
	for _, value := range []string{"", "float", "base64", "unsupported"} {
		t.Run(value, func(t *testing.T) {
			config := DefaultConfig("test-key")
			config.EmbeddingEncoding = value
			normalized, err := config.normalized()
			if value == "unsupported" {
				var validationError *ValidationError
				if !errors.As(err, &validationError) {
					t.Fatalf("invalid encoding accepted: %v", err)
				}
				return
			}
			if value == "" {
				value = "float"
			}
			if err != nil || normalized.EmbeddingEncoding != value {
				t.Fatalf("normalized encoding=%q error=%v", normalized.EmbeddingEncoding, err)
			}
		})
	}
}

func TestDirectNumericEmbeddingRowRemainsSupported(t *testing.T) {
	index := 0
	values := testVector(3, 4)
	vectors, err := parseEmbeddings(embeddingResponse{Data: []embeddingRow{
		{Index: &index, Embedding: &values},
	}}, 1)
	if err != nil || len(vectors) != 1 || math.Abs(float64(vectors[0][0])-0.6) > 1e-6 {
		t.Fatalf("direct numeric embedding row failed: %v", err)
	}
}

func TestBase64EmbeddingsValidateIndexes(t *testing.T) {
	encoded := encodedVector(testVector(1, 0))
	row := func(index int) any { return map[string]any{"index": index, "embedding": encoded} }
	tests := map[string][]any{
		"duplicate":      {row(0), row(0)},
		"missing_index":  {row(0), map[string]any{"embedding": encoded}},
		"negative_index": {row(0), row(-1)},
		"out_of_range":   {row(0), row(2)},
		"missing_result": {row(0)},
	}
	for name, data := range tests {
		t.Run(name, func(t *testing.T) {
			_, err := parseEncodedTestResponse(t, data, 2)
			var responseError *ResponseError
			if !errors.As(err, &responseError) {
				t.Fatalf("error = %v, want ResponseError", err)
			}
		})
	}
}

func encodedVector(values []float64) string {
	data := make([]byte, len(values)*4)
	for index, value := range values {
		binary.LittleEndian.PutUint32(data[index*4:], math.Float32bits(float32(value)))
	}
	return base64.StdEncoding.EncodeToString(data)
}

func parseEncodedTestResponse(t *testing.T, data []any, count int) ([][]float32, error) {
	t.Helper()
	body, err := json.Marshal(map[string]any{"data": data})
	if err != nil {
		t.Fatal(err)
	}
	var response embeddingResponse
	if err := decodeResponse(bytes.NewReader(body), &response); err != nil {
		return nil, err
	}
	return parseEmbeddings(response, count)
}
