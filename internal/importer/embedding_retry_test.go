package importer

import (
	"bytes"
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"log"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"testing/synctest"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/siliconflow"
)

type embeddingFunc func(context.Context, []string) ([][]float32, error)

func (embed embeddingFunc) EmbedMany(ctx context.Context, texts []string) ([][]float32, error) {
	return embed(ctx, texts)
}

func TestImporterRetriesTransientEmbeddingFailures(t *testing.T) {
	tests := map[string]error{
		"timeout":   &siliconflow.TransportError{Err: context.DeadlineExceeded},
		"reset":     &siliconflow.TransportError{Err: syscall.ECONNRESET},
		"refused":   &siliconflow.TransportError{Err: syscall.ECONNREFUSED},
		"eof":       &siliconflow.TransportError{Err: io.EOF},
		"truncated": &siliconflow.TransportError{Err: io.ErrUnexpectedEOF},
	}
	for _, status := range []int{408, 429, 500, 503, 599} {
		tests[fmt.Sprintf("http_%d", status)] = &siliconflow.HTTPError{StatusCode: status}
	}
	for name, failure := range tests {
		t.Run(name, func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				expectedDelay := time.Second
				if name == "http_429" {
					expectedDelay = 30 * time.Second
				}
				calls := 0
				started := time.Now()
				err := runEmbeddingTest(context.Background(), func(context.Context, []string) (
					[][]float32, error,
				) {
					calls++
					if calls == 1 {
						return nil, fmt.Errorf("wrapped: %w", failure)
					}
					return [][]float32{{1}}, nil
				})
				if err != nil || calls != 2 || time.Since(started) != expectedDelay {
					t.Fatalf("calls=%d elapsed=%s error=%v", calls, time.Since(started), err)
				}
			})
		})
	}
}

func TestImporterBoundsEmbeddingRetries(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		calls := 0
		started := time.Now()
		failure := &siliconflow.TransportError{Err: context.DeadlineExceeded}
		err := runEmbeddingTest(context.Background(), func(context.Context, []string) (
			[][]float32, error,
		) {
			calls++
			return nil, failure
		})
		if !errors.Is(err, failure) || calls != 4 || time.Since(started) != 7*time.Second {
			t.Fatalf("calls=%d elapsed=%s error=%v", calls, time.Since(started), err)
		}
	})
}

func TestImporterDoesNotRetryPermanentEmbeddingFailures(t *testing.T) {
	tests := map[string]error{
		"invalid_response":    &siliconflow.ResponseError{Reason: "invalid embedding"},
		"invalid_request":     &siliconflow.ValidationError{Field: "texts", Reason: "empty"},
		"closed_client":       siliconflow.ErrClosed,
		"canceled":            &siliconflow.TransportError{Err: context.Canceled},
		"certificate":         &siliconflow.TransportError{Err: x509.UnknownAuthorityError{}},
		"permanent_transport": &siliconflow.TransportError{Err: errors.New("invalid URL")},
	}
	for _, status := range []int{400, 401, 402, 403, 404, 422} {
		tests[fmt.Sprintf("http_%d", status)] = &siliconflow.HTTPError{StatusCode: status}
	}
	for name, failure := range tests {
		t.Run(name, func(t *testing.T) {
			calls := 0
			err := runEmbeddingTest(context.Background(), func(context.Context, []string) (
				[][]float32, error,
			) {
				calls++
				return nil, failure
			})
			if !errors.Is(err, failure) || calls != 1 {
				t.Fatalf("calls=%d error=%v", calls, err)
			}
		})
	}
}

func TestImporterCancelsEmbeddingRetryWait(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		calls := 0
		started := time.Now()
		cancelled := make(chan struct{})
		err := runEmbeddingTest(ctx, func(context.Context, []string) ([][]float32, error) {
			calls++
			go func() {
				time.Sleep(10 * time.Millisecond)
				cancel()
				close(cancelled)
			}()
			return nil, &siliconflow.TransportError{Err: context.DeadlineExceeded}
		})
		elapsed := time.Since(started)
		<-cancelled
		if !errors.Is(err, context.Canceled) || calls != 1 ||
			elapsed != 10*time.Millisecond {
			t.Fatalf("calls=%d elapsed=%s error=%v", calls, elapsed, err)
		}
	})
}

func TestImporterSkipsEmbeddingWithCanceledParent(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	calls := 0
	err := runEmbeddingTest(ctx, func(context.Context, []string) ([][]float32, error) {
		calls++
		return nil, &siliconflow.TransportError{Err: context.Canceled}
	})
	if !errors.Is(err, context.Canceled) || calls != 0 {
		t.Fatalf("calls=%d error=%v", calls, err)
	}
}

func TestImporterRetryLogOmitsProviderBody(t *testing.T) {
	var output bytes.Buffer
	original := log.Writer()
	log.SetOutput(&output)
	t.Cleanup(func() { log.SetOutput(original) })
	synctest.Test(t, func(t *testing.T) {
		calls := 0
		err := runEmbeddingTest(context.Background(), func(context.Context, []string) (
			[][]float32, error,
		) {
			calls++
			if calls == 1 {
				return nil, &siliconflow.HTTPError{StatusCode: 429, Body: "secret-provider-body"}
			}
			return [][]float32{{1}}, nil
		})
		if err != nil {
			t.Fatal(err)
		}
	})
	if strings.Contains(output.String(), "secret-provider-body") ||
		!strings.Contains(output.String(), "retry 1/3") ||
		!strings.Contains(output.String(), "HTTP 429") {
		t.Fatalf("unsafe or missing retry log: %q", output.String())
	}
}

func runEmbeddingTest(ctx context.Context, embed embeddingFunc) error {
	worker := New(Config{EmbeddingBatchSize: 1, EmbeddingConcurrency: 1}, nil, embed, nil)
	_, err := worker.embedBatches(ctx, testSentences()[:1])
	return err
}

func TestImporterRetriesTimedOutEmbeddingBatch(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var successfulCalls, retriedCalls atomic.Int32
		succeeded := make(chan struct{})
		embed := embeddingFunc(func(_ context.Context, texts []string) ([][]float32, error) {
			if texts[0] == testSentences()[0].Translation {
				successfulCalls.Add(1)
				close(succeeded)
				return [][]float32{{1, 0}}, nil
			}
			<-succeeded
			if retriedCalls.Add(1) == 1 {
				return nil, &siliconflow.TransportError{
					Path: "/embeddings", Err: context.DeadlineExceeded,
				}
			}
			return [][]float32{{2, 0}}, nil
		})
		worker := New(Config{EmbeddingBatchSize: 1, EmbeddingConcurrency: 2}, nil, embed, nil)
		vectors, err := worker.embedBatches(context.Background(), testSentences()[:2])
		if err != nil {
			t.Fatalf("temporary embedding timeout aborted import: %v", err)
		}
		if successfulCalls.Load() != 1 || retriedCalls.Load() != 2 {
			t.Fatalf("unexpected embedding calls: successful=%d retried=%d",
				successfulCalls.Load(), retriedCalls.Load())
		}
		if len(vectors) != 2 || vectors[0][0] != 1 || vectors[1][0] != 2 {
			t.Fatalf("embedding order changed after retry: %v", vectors)
		}
	})
}
