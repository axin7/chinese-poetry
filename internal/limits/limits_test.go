package limits

import (
	"errors"
	"net/http"
	"sync"
	"testing"
	"time"
)

func TestBucketRefillsWithoutRefundingRejectedRequests(t *testing.T) {
	now := time.Unix(100, 0)
	bucket := NewBucket(2, 4, func() time.Time { return now })
	for range 4 {
		if err := bucket.Admit("search_rate_exceeded"); err != nil {
			t.Fatal(err)
		}
	}
	assertRejection(t, bucket.Admit("search_rate_exceeded"), "search_rate_exceeded", 1)
	now = now.Add(499 * time.Millisecond)
	assertRejection(t, bucket.Admit("search_rate_exceeded"), "search_rate_exceeded", 1)
	now = now.Add(time.Millisecond)
	if err := bucket.Admit("search_rate_exceeded"); err != nil {
		t.Fatal(err)
	}
}

func TestMinuteBudgetUsesRollingWindow(t *testing.T) {
	now := time.Unix(100, 0)
	budget := NewMinuteBudget(2, func() time.Time { return now })
	if err := budget.Admit(); err != nil {
		t.Fatal(err)
	}
	now = now.Add(30 * time.Second)
	if err := budget.Admit(); err != nil {
		t.Fatal(err)
	}
	assertRejection(t, budget.Admit(), "embedding_budget_exceeded", 30)
	now = now.Add(30 * time.Second)
	if err := budget.Admit(); err != nil {
		t.Fatal(err)
	}
	assertRejection(t, budget.Admit(), "embedding_budget_exceeded", 30)
}

func TestConcurrentAttemptsRespectMinuteBudget(t *testing.T) {
	budget := NewMinuteBudget(7, func() time.Time { return time.Unix(100, 0) })
	results := make(chan error, 50)
	var group sync.WaitGroup
	for range cap(results) {
		group.Go(func() { results <- budget.Admit() })
	}
	group.Wait()
	close(results)
	accepted := 0
	for err := range results {
		if err == nil {
			accepted++
		} else {
			assertRejection(t, err, "embedding_budget_exceeded", 60)
		}
	}
	if accepted != 7 {
		t.Fatalf("accepted %d attempts", accepted)
	}
}

func assertRejection(t *testing.T, err error, code string, retry int) {
	t.Helper()
	var rejection *Error
	if !errors.As(err, &rejection) || rejection.Code != code ||
		rejection.Status != http.StatusTooManyRequests || rejection.RetryAfter != retry {
		t.Fatalf("unexpected rejection: %#v", err)
	}
}
