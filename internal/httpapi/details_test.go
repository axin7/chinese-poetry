package httpapi

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/limits"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
)

type countedRepo struct {
	calls   atomic.Int32
	err     error
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func (repo *countedRepo) Health(context.Context) error { return nil }

func (repo *countedRepo) GetWorkByID(
	ctx context.Context, id string,
) (repository.PoetryWork, error) {
	repo.calls.Add(1)
	if repo.started != nil {
		repo.once.Do(func() { close(repo.started) })
	}
	if repo.release != nil {
		select {
		case <-repo.release:
		case <-ctx.Done():
			return repository.PoetryWork{}, ctx.Err()
		}
	}
	if repo.err != nil {
		return repository.PoetryWork{}, repo.err
	}
	return fakeRepo{}.GetWorkByID(ctx, id)
}

func testDetails(t *testing.T, repo Repository, policy Policy) *detailCache {
	t.Helper()
	details, err := newDetailCache("test-v1", repo, policy)
	if err != nil {
		t.Fatal(err)
	}
	return details
}

func TestDetailsCacheSeparatesDatasetIDAndExpires(t *testing.T) {
	now := time.Unix(100, 0)
	policy := DefaultPolicy()
	policy.Now = func() time.Time { return now }
	repo := &countedRepo{}
	details := testDetails(t, repo, policy)
	for _, id := range []string{"tangsong:1", "tangsong:1", "tangshisanbaishou:1", "tangsong:2"} {
		if _, err := details.get(context.Background(), id); err != nil {
			t.Fatal(err)
		}
	}
	if repo.calls.Load() != 3 {
		t.Fatalf("expected three unique cache identities, got %d", repo.calls.Load())
	}
	otherGeneration, err := newDetailCache("other-v1", repo, policy)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := otherGeneration.get(context.Background(), "tangsong:1"); err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Hour)
	if _, err := details.get(context.Background(), "tangsong:1"); err != nil {
		t.Fatal(err)
	}
	if repo.calls.Load() != 5 {
		t.Fatalf("generation change or TTL retained stale detail: %d", repo.calls.Load())
	}
}

func TestDetailsNeverCacheErrorsOrOversizedResponses(t *testing.T) {
	for _, test := range []struct {
		err   error
		bytes int64
	}{
		{err: repository.ErrNotFound, bytes: 8 << 20},
		{err: repository.ErrDataIntegrity, bytes: 8 << 20},
		{bytes: 1},
	} {
		policy := DefaultPolicy()
		policy.DetailCacheBytes = test.bytes
		repo := &countedRepo{err: test.err}
		details := testDetails(t, repo, policy)
		for range 2 {
			_, err := details.get(context.Background(), "tangsong:1")
			if !errors.Is(err, test.err) {
				t.Fatalf("unexpected detail result: %v", err)
			}
		}
		if repo.calls.Load() != 2 {
			t.Fatalf("failed/oversized detail was cached: %d", repo.calls.Load())
		}
	}
}

func TestDetailCapacityAndCancellationDoNotStartExtraWork(t *testing.T) {
	policy := DefaultPolicy()
	policy.DetailConcurrency = 1
	repo := &countedRepo{started: make(chan struct{}), release: make(chan struct{})}
	details := testDetails(t, repo, policy)
	done := make(chan error, 1)
	go func() { _, err := details.get(context.Background(), "tangsong:1"); done <- err }()
	<-repo.started
	_, err := details.get(context.Background(), "tangsong:2")
	var rejection *limits.Error
	if !errors.As(err, &rejection) || rejection.Code != "detail_capacity_exceeded" {
		t.Fatalf("unexpected capacity error: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = details.get(ctx, "tangsong:3")
	if !errors.Is(err, context.Canceled) || repo.calls.Load() != 1 {
		t.Fatalf("canceled request started work: %v calls=%d", err, repo.calls.Load())
	}
	close(repo.release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

type enteredContext struct {
	context.Context
	entered chan struct{}
	once    sync.Once
}

func (ctx *enteredContext) Done() <-chan struct{} {
	ctx.once.Do(func() { close(ctx.entered) })
	return ctx.Context.Done()
}

func TestDetailsSingleflightSharesCapacityAcrossWaiters(t *testing.T) {
	policy := DefaultPolicy()
	policy.DetailConcurrency = 1
	repo := &countedRepo{started: make(chan struct{}), release: make(chan struct{})}
	details := testDetails(t, repo, policy)
	done := make(chan error, 2)
	go func() { _, err := details.get(context.Background(), "tangsong:1"); done <- err }()
	<-repo.started
	ctx := &enteredContext{Context: context.Background(), entered: make(chan struct{})}
	go func() { _, err := details.get(ctx, "tangsong:1"); done <- err }()
	<-ctx.entered
	close(repo.release)
	for range 2 {
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
	if repo.calls.Load() != 1 {
		t.Fatalf("singleflight started %d reads", repo.calls.Load())
	}
}

func TestDetailCacheRemainsWithinMemoryBudget(t *testing.T) {
	policy := DefaultPolicy()
	policy.DetailCacheBytes = 400
	repo := &countedRepo{}
	details := testDetails(t, repo, policy)
	for _, id := range []string{"tangsong:1", "tangsong:2", "tangsong:3", "tangsong:1"} {
		if _, err := details.get(context.Background(), id); err != nil {
			t.Fatal(err)
		}
		if details.values.Bytes() > policy.DetailCacheBytes {
			t.Fatal("detail cache exceeded its memory budget")
		}
	}
	if repo.calls.Load() != 4 {
		t.Fatalf("evicted detail did not reload: %d", repo.calls.Load())
	}
}
