package search

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"time"

	"golang.org/x/sync/singleflight"

	"github.com/chinese-poetry/chinese-poetry/internal/cache"
	"github.com/chinese-poetry/chinese-poetry/internal/model"
)

type vectorResolver struct {
	profile string
	models  Models
	cache   *cache.Cache[string, []float32]
	flight  singleflight.Group
}

func newVectorResolver(settings Settings, models Models) (*vectorResolver, error) {
	vectors, err := cache.New[string, []float32](settings.VectorCacheBytes)
	if err != nil {
		return nil, err
	}
	return &vectorResolver{
		profile: settings.EmbeddingProfile,
		models:  models,
		cache:   vectors,
	}, nil
}

func (r *vectorResolver) Resolve(
	ctx context.Context,
	request model.SearchRequest,
) ([]float32, error) {
	if request.Vector != nil {
		return request.Vector, nil
	}
	if request.Query == nil {
		return nil, fmt.Errorf("query or vector is required")
	}
	key := embeddingKey(r.profile, *request.Query)
	if vector, ok := r.cache.Get(key); ok {
		return vector, nil
	}
	result, err, _ := r.flight.Do(key, func() (any, error) {
		if vector, ok := r.cache.Get(key); ok {
			return vector, nil
		}
		if r.models == nil {
			return nil, fmt.Errorf("%w: SiliconFlow key is not configured", ErrUnavailable)
		}
		vector, err := r.models.Embed(ctx, *request.Query)
		if err != nil {
			return nil, fmt.Errorf("%w: query embedding failed: %w", ErrUnavailable, err)
		}
		vector, err = model.NormalizeVector(vector, model.VectorDimension)
		if err != nil {
			return nil, fmt.Errorf("%w: invalid query embedding", ErrUnavailable)
		}
		r.cache.Put(key, vector, int64(len(vector)*4), 24*time.Hour)
		return vector, nil
	})
	if err != nil {
		return nil, err
	}
	return result.([]float32), nil
}

func embeddingKey(profile, query string) string {
	digest := sha256.Sum256([]byte(profile + "\x00" + query))
	return hex.EncodeToString(digest[:])
}
