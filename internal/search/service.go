package search

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/url"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/singleflight"

	"github.com/chinese-poetry/chinese-poetry/internal/cache"
	"github.com/chinese-poetry/chinese-poetry/internal/limits"
	"github.com/chinese-poetry/chinese-poetry/internal/model"
	"github.com/chinese-poetry/chinese-poetry/internal/qdrantstore"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
	"github.com/chinese-poetry/chinese-poetry/internal/siliconflow"
)

var (
	ErrUnavailable = errors.New("search unavailable")
	ErrNoResult    = errors.New("no search result")
)

type Settings struct {
	Generation         string
	EmbeddingProfile   string
	RerankModel        string
	RerankEnabled      bool
	ResponseCacheBytes int64
	VectorCacheBytes   int64
	ResponseCacheTTL   time.Duration
	RequestTimeout     time.Duration
	SearchConcurrency  int
}

type Models interface {
	Embed(context.Context, string) ([]float32, error)
	Rerank(context.Context, string, []string) (siliconflow.RerankResult, error)
}

type VectorStore interface {
	Search(context.Context, []float32, []string, uint64) ([]qdrantstore.Hit, error)
}

type Repository interface {
	RetrieveSearch(context.Context, repository.Locator) (repository.SearchLookup, error)
}

type searchCandidate struct {
	repository.SearchLookup
	score float32
}

type Service struct {
	settings  Settings
	models    Models
	store     VectorStore
	repo      Repository
	resolver  *vectorResolver
	responses *cache.Cache[string, model.SearchResponse]
	flight    singleflight.Group
	timeout   time.Duration
	slots     chan struct{}
}

func New(
	settings Settings,
	models Models,
	store VectorStore,
	repo Repository,
) (*Service, error) {
	if settings.Generation == "" || settings.EmbeddingProfile == "" ||
		settings.ResponseCacheTTL <= 0 {
		return nil, fmt.Errorf("search settings are invalid")
	}
	responses, err := cache.New[string, model.SearchResponse](settings.ResponseCacheBytes)
	if err != nil {
		return nil, err
	}
	resolver, err := newVectorResolver(settings, models)
	if err != nil {
		return nil, err
	}
	if settings.RequestTimeout <= 0 {
		settings.RequestTimeout = 10 * time.Second
	}
	if settings.SearchConcurrency <= 0 {
		settings.SearchConcurrency = 256
	}
	settings.RerankEnabled = settings.RerankEnabled && models != nil
	return &Service{
		settings: settings, models: models, store: store, repo: repo,
		resolver: resolver, responses: responses, timeout: settings.RequestTimeout,
		slots: make(chan struct{}, settings.SearchConcurrency),
	}, nil
}

func (s *Service) Search(
	ctx context.Context,
	request model.SearchRequest,
) (model.SearchResponse, error) {
	if err := request.Validate(); err != nil {
		return model.SearchResponse{}, err
	}
	key, err := resultKey(request, s.settings)
	if err != nil {
		return model.SearchResponse{}, err
	}
	if response, ok := s.responses.Get(key); ok {
		return response, nil
	}
	resultChannel := s.flight.DoChan(key, func() (any, error) {
		if response, ok := s.responses.Get(key); ok {
			return response, nil
		}
		if !s.acquire() {
			return model.SearchResponse{}, fmt.Errorf("%w: %w", ErrUnavailable,
				limits.Capacity("search_capacity_exceeded"))
		}
		defer s.release()
		sharedCtx, cancel := context.WithTimeout(context.Background(), s.timeout)
		defer cancel()
		return s.searchUncached(sharedCtx, request, key)
	})
	select {
	case result := <-resultChannel:
		if result.Err != nil {
			return model.SearchResponse{}, result.Err
		}
		return result.Val.(model.SearchResponse), nil
	case <-ctx.Done():
		return model.SearchResponse{}, fmt.Errorf("%w: request canceled", ErrUnavailable)
	}
}

func (s *Service) acquire() bool {
	select {
	case s.slots <- struct{}{}:
		return true
	default:
		return false
	}
}

func (s *Service) release() {
	<-s.slots
}

func (s *Service) searchUncached(
	ctx context.Context,
	request model.SearchRequest,
	cacheKey string,
) (model.SearchResponse, error) {
	vector, err := s.resolver.Resolve(ctx, request)
	if err != nil {
		return model.SearchResponse{}, err
	}
	rerank := s.settings.RerankEnabled && request.Query != nil
	var datasets []string
	if request.Filters != nil {
		datasets = request.Filters.Tables
	}
	limit := uint64(1)
	if rerank {
		limit = 5
	}
	hits, err := s.store.Search(ctx, vector, datasets, limit)
	if err != nil {
		return model.SearchResponse{}, fmt.Errorf("%w: vector search failed", ErrUnavailable)
	}
	if len(hits) == 0 {
		return model.SearchResponse{}, ErrNoResult
	}
	lookup, degraded, err := s.selectHit(ctx, request, hits, rerank)
	if err != nil {
		return model.SearchResponse{}, err
	}
	response := toResponse(lookup, s.settings.Generation)
	ttl := s.settings.ResponseCacheTTL
	if degraded && ttl > 30*time.Second {
		ttl = 30 * time.Second
	}
	data, _ := json.Marshal(response)
	s.responses.Put(cacheKey, response, int64(len(data)), ttl)
	return response, nil
}

func (s *Service) selectHit(
	ctx context.Context,
	request model.SearchRequest,
	hits []qdrantstore.Hit,
	rerank bool,
) (searchCandidate, bool, error) {
	if !rerank || len(hits) == 1 {
		lookup, err := s.retrieve(ctx, hits[0])
		return lookup, false, err
	}
	candidates, degraded, err := s.retrieveAll(ctx, hits)
	if err != nil {
		return searchCandidate{}, false, err
	}
	if len(candidates) == 1 {
		return candidates[0], true, nil
	}
	documents := make([]string, len(candidates))
	for index := range candidates {
		documents[index] = candidates[index].Match.Translation
	}
	if s.models == nil || request.Query == nil {
		return candidates[0], true, nil
	}
	result, err := s.models.Rerank(ctx, *request.Query, documents)
	if err != nil || result.Index < 0 || result.Index >= len(candidates) {
		return candidates[0], true, nil
	}
	return candidates[result.Index], degraded, nil
}

func (s *Service) retrieveAll(
	ctx context.Context,
	hits []qdrantstore.Hit,
) ([]searchCandidate, bool, error) {
	results := make([]searchCandidate, len(hits))
	errorsByIndex := make([]error, len(hits))
	var group sync.WaitGroup
	for index := range hits {
		group.Go(func() {
			result, err := s.retrieve(ctx, hits[index])
			results[index] = result
			errorsByIndex[index] = err
		})
	}
	group.Wait()
	if errorsByIndex[0] != nil {
		return nil, false, errorsByIndex[0]
	}
	valid := results[:1]
	degraded := false
	for index, err := range errorsByIndex[1:] {
		if err == nil {
			valid = append(valid, results[index+1])
		} else {
			degraded = true
		}
	}
	return valid, degraded, nil
}

func (s *Service) retrieve(
	ctx context.Context,
	hit qdrantstore.Hit,
) (searchCandidate, error) {
	lookup, err := s.repo.RetrieveSearch(ctx, repository.Locator{
		Dataset: hit.Dataset, SourceRowID: hit.SourceRowID,
		RawIndex: hit.RawIndex, NormalizedIndex: hit.NormalizedIndex,
		WorkID: hit.WorkID,
	})
	if err != nil {
		return searchCandidate{}, fmt.Errorf(
			"%w: corpus lookup failed", ErrUnavailable,
		)
	}
	return searchCandidate{SearchLookup: lookup, score: hit.Score}, nil
}

func toResponse(lookup searchCandidate, generation string) model.SearchResponse {
	workID := strings.ReplaceAll(url.PathEscape(lookup.WorkID), ":", "%3A")
	detailURL := "/poems/" + workID + "?generation=" + url.QueryEscape(generation)
	return model.SearchResponse{
		Match: model.SearchMatch{
			Original: lookup.Match.Original, Translation: lookup.Match.Translation,
			SentenceIndex: lookup.Match.WorkSentenceIndex, Score: lookup.score,
		},
		Poem: model.PoemSummary{
			ID: lookup.WorkID, Title: lookup.Title,
			Author: lookup.Author, DetailURL: detailURL,
		},
	}
}

func resultKey(request model.SearchRequest, settings Settings) (string, error) {
	identity := "text:"
	query := ""
	if request.Vector != nil {
		identity = "vector:" + vectorHash(request.Vector)
		if settings.RerankEnabled && request.Query != nil {
			query = *request.Query
		}
	} else if request.Query != nil {
		identity += *request.Query
	}
	var tables []string
	if request.Filters != nil {
		tables = request.Filters.Tables
	}
	payload := struct {
		Input, Query, Generation, Profile, RerankModel string
		Tables                                         []string
		Rerank                                         bool
	}{
		identity, query, settings.Generation, settings.EmbeddingProfile,
		settings.RerankModel, tables,
		settings.RerankEnabled && request.Query != nil,
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:]), nil
}

func vectorHash(vector []float32) string {
	data := make([]byte, len(vector)*4)
	for index, value := range vector {
		binary.LittleEndian.PutUint32(data[index*4:], math.Float32bits(value))
	}
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:])
}
