package httpapi

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"golang.org/x/sync/singleflight"

	"github.com/chinese-poetry/chinese-poetry/internal/cache"
	"github.com/chinese-poetry/chinese-poetry/internal/limits"
	"github.com/chinese-poetry/chinese-poetry/internal/model"
)

type detailCache struct {
	generation string
	repository Repository
	values     *cache.Cache[string, []byte]
	flight     singleflight.Group
	slots      chan struct{}
	ttl        time.Duration
}

func newDetailCache(generation string, repo Repository, policy Policy) (*detailCache, error) {
	values, err := cache.NewWithClock[string, []byte](policy.DetailCacheBytes, policy.Now)
	if err != nil {
		return nil, err
	}
	return &detailCache{generation: generation, repository: repo, values: values,
		slots: make(chan struct{}, policy.DetailConcurrency), ttl: policy.DetailCacheTTL}, nil
}

func (details *detailCache) get(ctx context.Context, id string) ([]byte, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	dataset := strings.SplitN(id, ":", 2)[0]
	key := details.generation + "\x00" + dataset + "\x00" + id
	if value, ok := details.values.Get(key); ok {
		return value, nil
	}
	result := details.flight.DoChan(key, func() (any, error) {
		if value, ok := details.values.Get(key); ok {
			return value, nil
		}
		return details.load(id, key)
	})
	select {
	case value := <-result:
		if value.Err != nil {
			return nil, value.Err
		}
		return value.Val.([]byte), nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (details *detailCache) load(id, key string) ([]byte, error) {
	select {
	case details.slots <- struct{}{}:
		defer func() { <-details.slots }()
	default:
		return nil, limits.Capacity("detail_capacity_exceeded")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	work, err := details.repository.GetWorkByID(ctx, id)
	if err != nil {
		return nil, err
	}
	value, err := json.Marshal(model.WorkDetailResponse{
		ID: work.WorkID, Dataset: work.Dataset, Title: work.Title, Author: work.Author,
		Original: nonNilStrings(work.Original), Translation: nonNilStrings(work.Translation),
		Interpretations: nonNilStrings(work.Interpretations),
	})
	if err != nil {
		return nil, err
	}
	value = append(value, '\n')
	details.values.Put(key, value, int64(len(value)+len(key)), details.ttl)
	return value, nil
}
