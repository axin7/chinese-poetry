package importer

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"testing"
	"testing/synctest"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/siliconflow"
)

func TestImporterRecoversAfterRateLimitWindow(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		started := time.Now()
		calls := 0
		err := runEmbeddingTest(context.Background(), func(context.Context, []string) (
			[][]float32, error,
		) {
			calls++
			if time.Since(started) < time.Minute {
				return nil, &siliconflow.HTTPError{StatusCode: 429}
			}
			return [][]float32{{1}}, nil
		})
		if err != nil || calls != 3 || time.Since(started) != 90*time.Second {
			t.Fatalf("rate limit recovery: calls=%d elapsed=%s error=%v",
				calls, time.Since(started), err)
		}
	})
}

func TestImporterHonorsRetryAfter(t *testing.T) {
	for _, delay := range []time.Duration{0, 2 * time.Second, 90 * time.Second} {
		t.Run(delay.String(), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				started := time.Now()
				calls := 0
				err := runEmbeddingTest(context.Background(), func(context.Context, []string) (
					[][]float32, error,
				) {
					calls++
					if calls == 1 {
						failure := &siliconflow.HTTPError{StatusCode: 429, RetryAfter: &delay}
						return nil, fmt.Errorf("wrapped: %w", failure)
					}
					return [][]float32{{1}}, nil
				})
				if err != nil || calls != 2 || time.Since(started) != delay {
					t.Fatalf("calls=%d elapsed=%s error=%v", calls, time.Since(started), err)
				}
			})
		})
	}
}

func TestImporterRateLimitWaitRespectsParentDeadline(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		started := time.Now()
		calls := 0
		err := runEmbeddingTest(ctx, func(context.Context, []string) ([][]float32, error) {
			calls++
			return nil, &siliconflow.HTTPError{StatusCode: 429}
		})
		if !errors.Is(err, context.DeadlineExceeded) || calls != 1 ||
			time.Since(started) != 3*time.Second {
			t.Fatalf("calls=%d elapsed=%s error=%v", calls, time.Since(started), err)
		}
	})
}

func TestImporterBoundsRateLimitRetries(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		started := time.Now()
		var attempts []time.Duration
		failure := &siliconflow.HTTPError{StatusCode: 429}
		err := runEmbeddingTest(context.Background(), func(context.Context, []string) (
			[][]float32, error,
		) {
			attempts = append(attempts, time.Since(started))
			return nil, failure
		})
		want := []time.Duration{0, 30 * time.Second, 90 * time.Second, 210 * time.Second}
		if !errors.Is(err, failure) || !slices.Equal(attempts, want) {
			t.Fatalf("attempt times=%v error=%v", attempts, err)
		}
	})
}
