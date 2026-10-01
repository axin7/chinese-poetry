package cache

import (
	"sync"
	"testing"
	"time"
)

func newTestCache(t *testing.T, maxBytes int64) *Cache[string, string] {
	t.Helper()
	cache, err := New[string, string](maxBytes)
	if err != nil {
		t.Fatal(err)
	}
	return cache
}

func TestCacheEvictsLeastRecentlyUsedEntry(t *testing.T) {
	cache := newTestCache(t, 6)
	cache.Put("first", "a", 3, time.Minute)
	cache.Put("second", "b", 3, time.Minute)
	if _, ok := cache.Get("first"); !ok {
		t.Fatal("first entry was not cached")
	}

	cache.Put("third", "c", 3, time.Minute)

	if _, ok := cache.Get("second"); ok {
		t.Fatal("least recently used entry was not evicted")
	}
	if value, ok := cache.Get("first"); !ok || value != "a" {
		t.Fatalf("unexpected first entry: %q, %v", value, ok)
	}
}

func TestCacheExpiresEntriesUsingMonotonicClock(t *testing.T) {
	cache := newTestCache(t, 10)
	now := time.Now()
	cache.now = func() time.Time { return now }
	cache.Put("key", "value", 5, time.Second)

	now = now.Add(time.Second)
	if _, ok := cache.Get("key"); ok {
		t.Fatal("expired entry was returned")
	}
	if cache.Bytes() != 0 || cache.Len() != 0 {
		t.Fatal("expired entry still consumes capacity")
	}
}

func TestInvalidPutPreservesExistingEntry(t *testing.T) {
	cache := newTestCache(t, 10)
	cache.Put("key", "old", 3, time.Minute)
	cache.Put("key", "new", 11, time.Minute)

	value, ok := cache.Get("key")
	if !ok || value != "old" {
		t.Fatalf("invalid put replaced existing value: %q, %v", value, ok)
	}
}

func TestCacheReplacementUpdatesByteCount(t *testing.T) {
	cache := newTestCache(t, 10)
	cache.Put("key", "old", 6, time.Minute)
	cache.Put("key", "new", 4, time.Minute)

	if cache.Bytes() != 4 || cache.Len() != 1 {
		t.Fatalf("unexpected cache size: %d bytes, %d entries", cache.Bytes(), cache.Len())
	}
}

func TestCacheIsConcurrencySafe(t *testing.T) {
	cache := newTestCache(t, 64)
	var group sync.WaitGroup
	for index := range 100 {
		group.Add(1)
		go func() {
			defer group.Done()
			key := string(rune('a' + index%20))
			cache.Put(key, key, 4, time.Minute)
			cache.Get(key)
		}()
	}
	group.Wait()

	if cache.Bytes() > 64 {
		t.Fatalf("cache exceeded byte limit: %d", cache.Bytes())
	}
}

func TestNewRejectsInvalidCapacity(t *testing.T) {
	if _, err := New[string, string](0); err == nil {
		t.Fatal("expected invalid capacity error")
	}
}
