package qdrantstore

import (
	"context"
	"fmt"
	"log"
	"net/url"
	"strconv"
	"time"

	"github.com/qdrant/go-client/qdrant"
	"google.golang.org/grpc/status"
)

var payloadFields = []string{
	"dataset",
	"source_row_id",
	"raw_index",
	"normalized_index",
	"work_id",
	"generation",
}

type Client interface {
	Query(context.Context, *qdrant.QueryPoints) ([]*qdrant.ScoredPoint, error)
	CollectionExists(context.Context, string) (bool, error)
	GetCollectionInfo(context.Context, string) (*qdrant.CollectionInfo, error)
	CreateCollection(context.Context, *qdrant.CreateCollection) error
	CreateFieldIndex(
		context.Context,
		*qdrant.CreateFieldIndexCollection,
	) (*qdrant.UpdateResult, error)
	Upsert(context.Context, *qdrant.UpsertPoints) (*qdrant.UpdateResult, error)
	Count(context.Context, *qdrant.CountPoints) (uint64, error)
	Close() error
}

type Hit struct {
	Score           float32
	Dataset         string
	SourceRowID     int64
	RawIndex        int
	NormalizedIndex int
	WorkID          string
	Generation      string
}

type Store struct {
	client     Client
	collection string
	generation string
	hnswEF     uint64
}

func New(client Client, collection, generation string, hnswEF uint64) *Store {
	return &Store{
		client:     client,
		collection: collection,
		generation: generation,
		hnswEF:     hnswEF,
	}
}

func NewOfficialClient(rawURL, apiKey string, forceTLS bool) (*qdrant.Client, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Hostname() == "" ||
		(parsed.Scheme != "http" && parsed.Scheme != "https") {
		return nil, fmt.Errorf("invalid Qdrant URL %q", rawURL)
	}
	port := 6334
	if parsed.Port() != "" {
		port, err = strconv.Atoi(parsed.Port())
		if err != nil || port <= 0 || port > 65535 {
			return nil, fmt.Errorf("invalid Qdrant port in %q", rawURL)
		}
	}
	return qdrant.NewClient(&qdrant.Config{
		Host:                   parsed.Hostname(),
		Port:                   port,
		APIKey:                 apiKey,
		UseTLS:                 forceTLS || parsed.Scheme == "https",
		PoolSize:               1,
		SkipCompatibilityCheck: true,
	})
}

func (s *Store) Search(
	ctx context.Context,
	vector []float32,
	datasets []string,
	limit uint64,
) ([]Hit, error) {
	exact := false
	indexedOnly := true
	rescore := false
	request := &qdrant.QueryPoints{
		CollectionName: s.collection,
		Query:          qdrant.NewQuery(vector...),
		Filter:         s.filter(datasets),
		Params: &qdrant.SearchParams{
			HnswEf:      &s.hnswEF,
			Exact:       &exact,
			IndexedOnly: &indexedOnly,
			Quantization: &qdrant.QuantizationSearchParams{
				Rescore: &rescore,
			},
		},
		Limit:       &limit,
		WithPayload: qdrant.NewWithPayloadInclude(payloadFields...),
		WithVectors: qdrant.NewWithVectors(false),
	}
	started := time.Now()
	points, err := s.client.Query(ctx, request)
	if err != nil {
		log.Printf("Qdrant query failed: collection=%s code=%s elapsed=%s",
			s.collection, status.Code(err), time.Since(started))
		return nil, fmt.Errorf("query Qdrant: %w", err)
	}
	return s.parseHits(points)
}

func (s *Store) Count(ctx context.Context, datasets []string) (uint64, error) {
	return s.count(ctx, datasets, true)
}

func (s *Store) count(
	ctx context.Context,
	datasets []string,
	exact bool,
) (uint64, error) {
	count, err := s.client.Count(ctx, &qdrant.CountPoints{
		CollectionName: s.collection,
		Filter:         s.filter(datasets),
		Exact:          &exact,
	})
	if err != nil {
		return 0, fmt.Errorf("count Qdrant points: %w", err)
	}
	return count, nil
}

func (s *Store) ValidateGenerationIsolation(ctx context.Context) error {
	_, err := s.isolatedGenerationCount(ctx)
	return err
}

func (s *Store) isolatedGenerationCount(ctx context.Context) (uint64, error) {
	exact := true
	total, err := s.client.Count(ctx, &qdrant.CountPoints{
		CollectionName: s.collection,
		Exact:          &exact,
	})
	if err != nil {
		return 0, fmt.Errorf("count collection points: %w", err)
	}
	current, err := s.Count(ctx, nil)
	if err != nil {
		return 0, err
	}
	if total != current {
		return 0, fmt.Errorf(
			"collection contains another corpus generation; use a new collection name",
		)
	}
	return current, nil
}

func (s *Store) Close() error {
	return s.client.Close()
}

func (s *Store) filter(datasets []string) *qdrant.Filter {
	must := []*qdrant.Condition{
		qdrant.NewMatchKeyword("generation", s.generation),
	}
	if len(datasets) > 0 {
		must = append(must, qdrant.NewMatchKeywords("dataset", datasets...))
	}
	return &qdrant.Filter{Must: must}
}

func (s *Store) parseHits(points []*qdrant.ScoredPoint) ([]Hit, error) {
	hits := make([]Hit, 0, len(points))
	for _, point := range points {
		hit, err := s.parseHit(point)
		if err != nil {
			return nil, err
		}
		hits = append(hits, hit)
	}
	return hits, nil
}

func (s *Store) parseHit(point *qdrant.ScoredPoint) (Hit, error) {
	if point == nil {
		return Hit{}, fmt.Errorf("Qdrant hit is nil")
	}
	payload := point.GetPayload()
	hit := Hit{
		Score:           point.GetScore(),
		Dataset:         stringValue(payload, "dataset"),
		SourceRowID:     intValue(payload, "source_row_id"),
		RawIndex:        int(intValue(payload, "raw_index")),
		NormalizedIndex: int(intValue(payload, "normalized_index")),
		WorkID:          stringValue(payload, "work_id"),
		Generation:      stringValue(payload, "generation"),
	}
	if err := validateHit(hit, s.generation); err != nil {
		return Hit{}, err
	}
	return hit, nil
}

func stringValue(payload map[string]*qdrant.Value, key string) string {
	value := payload[key]
	if value == nil {
		return ""
	}
	if _, ok := value.Kind.(*qdrant.Value_StringValue); !ok {
		return ""
	}
	return value.GetStringValue()
}

func intValue(payload map[string]*qdrant.Value, key string) int64 {
	value := payload[key]
	if value == nil {
		return -1
	}
	if _, ok := value.Kind.(*qdrant.Value_IntegerValue); !ok {
		return -1
	}
	return value.GetIntegerValue()
}

func validateHit(hit Hit, generation string) error {
	if hit.Dataset == "" || hit.SourceRowID <= 0 || hit.RawIndex < 0 ||
		hit.NormalizedIndex < 0 || hit.WorkID == "" || hit.Generation == "" {
		return fmt.Errorf("Qdrant hit has invalid locator payload")
	}
	if hit.Generation != generation {
		return fmt.Errorf("Qdrant hit generation does not match corpus")
	}
	return nil
}
