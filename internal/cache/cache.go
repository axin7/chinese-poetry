package cache

import (
	"container/list"
	"fmt"
	"sync"
	"time"
)

type entry[K comparable, V any] struct {
	key       K
	value     V
	size      int64
	expiresAt time.Time
}

type Cache[K comparable, V any] struct {
	mu       sync.Mutex
	maxBytes int64
	bytes    int64
	items    map[K]*list.Element
	order    *list.List
	now      func() time.Time
}

func New[K comparable, V any](maxBytes int64) (*Cache[K, V], error) {
	return NewWithClock[K, V](maxBytes, time.Now)
}

func NewWithClock[K comparable, V any](
	maxBytes int64, now func() time.Time,
) (*Cache[K, V], error) {
	if maxBytes <= 0 {
		return nil, fmt.Errorf("maxBytes must be a positive integer")
	}
	if now == nil {
		now = time.Now
	}
	return &Cache[K, V]{
		maxBytes: maxBytes,
		items:    make(map[K]*list.Element),
		order:    list.New(),
		now:      now,
	}, nil
}

func (cache *Cache[K, V]) Get(key K) (V, bool) {
	cache.mu.Lock()
	defer cache.mu.Unlock()

	element, exists := cache.items[key]
	if !exists {
		var zero V
		return zero, false
	}
	item := element.Value.(*entry[K, V])
	if !item.expiresAt.After(cache.now()) {
		cache.remove(element)
		var zero V
		return zero, false
	}
	cache.order.MoveToBack(element)
	return item.value, true
}

func (cache *Cache[K, V]) Put(key K, value V, size int64, ttl time.Duration) {
	if size <= 0 || ttl <= 0 || size > cache.maxBytes {
		return
	}
	cache.mu.Lock()
	defer cache.mu.Unlock()

	if existing, exists := cache.items[key]; exists {
		cache.remove(existing)
	}
	item := &entry[K, V]{
		key: key, value: value, size: size, expiresAt: cache.now().Add(ttl),
	}
	cache.items[key] = cache.order.PushBack(item)
	cache.bytes += size
	for cache.bytes > cache.maxBytes {
		cache.remove(cache.order.Front())
	}
}

func (cache *Cache[K, V]) Len() int {
	cache.mu.Lock()
	defer cache.mu.Unlock()
	cache.purgeExpired()
	return len(cache.items)
}

func (cache *Cache[K, V]) Bytes() int64 {
	cache.mu.Lock()
	defer cache.mu.Unlock()
	cache.purgeExpired()
	return cache.bytes
}

func (cache *Cache[K, V]) purgeExpired() {
	now := cache.now()
	for element := cache.order.Front(); element != nil; {
		next := element.Next()
		item := element.Value.(*entry[K, V])
		if !item.expiresAt.After(now) {
			cache.remove(element)
		}
		element = next
	}
}

func (cache *Cache[K, V]) remove(element *list.Element) {
	item := element.Value.(*entry[K, V])
	delete(cache.items, item.key)
	cache.order.Remove(element)
	cache.bytes -= item.size
}
