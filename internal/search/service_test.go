package search

import (
	"context"
	"errors"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/model"
	"github.com/chinese-poetry/chinese-poetry/internal/qdrantstore"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
	"github.com/chinese-poetry/chinese-poetry/internal/siliconflow"
)

type fakeModels struct {
	embedCalls  int
	rerankCalls int
	rerankIndex int
	rerankErr   error
}

type blockingModels struct {
	started chan struct{}
	release chan struct{}
	once    sync.Once
	calls   atomic.Int32
}

func (f *blockingModels) Embed(context.Context, string) ([]float32, error) {
	f.calls.Add(1)
	f.once.Do(func() { close(f.started) })
	<-f.release
	return unitVector(), nil
}

func (*blockingModels) Rerank(
	context.Context,
	string,
	[]string,
) (siliconflow.RerankResult, error) {
	return siliconflow.RerankResult{}, nil
}

func (f *fakeModels) Embed(context.Context, string) ([]float32, error) {
	f.embedCalls++
	return unitVector(), nil
}

func (f *fakeModels) Rerank(
	context.Context,
	string,
	[]string,
) (siliconflow.RerankResult, error) {
	f.rerankCalls++
	return siliconflow.RerankResult{Index: f.rerankIndex, Score: 0.9}, f.rerankErr
}

type fakeVectorStore struct {
	hits  []qdrantstore.Hit
	calls int
	limit uint64
}

func (f *fakeVectorStore) Search(
	_ context.Context,
	_ []float32,
	_ []string,
	limit uint64,
) ([]qdrantstore.Hit, error) {
	f.calls++
	f.limit = limit
	return f.hits, nil
}

type fakeRepository struct{}

func (fakeRepository) RetrieveSearch(
	_ context.Context,
	locator repository.Locator,
) (repository.SearchLookup, error) {
	return repository.SearchLookup{
		Match: repository.SentenceMatch{
			Locator: locator, Original: "古句", Translation: "现代译文",
			WorkSentenceIndex: locator.RawIndex,
		},
		WorkID: locator.WorkID, Dataset: locator.Dataset,
		Title: "测试诗",
	}, nil
}

type partialRepository struct {
	failRowID int64
}

func (partial partialRepository) RetrieveSearch(
	ctx context.Context,
	locator repository.Locator,
) (repository.SearchLookup, error) {
	if locator.SourceRowID == partial.failRowID {
		return repository.SearchLookup{}, errors.New("missing candidate")
	}
	return fakeRepository{}.RetrieveSearch(ctx, locator)
}

func TestVectorSearchSkipsEmbeddingAndReturnsOne(t *testing.T) {
	models := &fakeModels{}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1)}}
	service := newTestService(t, false, models, store)
	request := model.SearchRequest{
		Vector: unitVector(), EmbeddingProfile: model.EmbeddingProfile,
	}

	response, err := service.Search(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if models.embedCalls != 0 || store.limit != 1 || response.Poem.ID != "tangsong:1" {
		t.Fatalf("unexpected vector result: %#v", response)
	}
}

func TestTextSearchCachesEmbeddingAndResponse(t *testing.T) {
	models := &fakeModels{}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1)}}
	service := newTestService(t, false, models, store)
	query := "想家"
	request := model.SearchRequest{Query: &query}

	first, err := service.Search(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	second, err := service.Search(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if first != second || models.embedCalls != 1 || store.calls != 1 {
		t.Fatalf("cache miss: models=%d store=%d", models.embedCalls, store.calls)
	}
}

func TestRerankUsesFiveCandidatesAndSelectsOne(t *testing.T) {
	models := &fakeModels{rerankIndex: 1}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1), testHit(2)}}
	service := newTestService(t, true, models, store)
	query := "月夜思乡"

	response, err := service.Search(
		context.Background(), model.SearchRequest{Query: &query},
	)
	if err != nil {
		t.Fatal(err)
	}
	if store.limit != 5 || models.rerankCalls != 1 || response.Poem.ID != "tangsong:2" {
		t.Fatalf("unexpected rerank result: %#v", response)
	}
}

func TestRerankFailureFallsBackToANNFirst(t *testing.T) {
	models := &fakeModels{rerankErr: errors.New("timeout")}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1), testHit(2)}}
	service := newTestService(t, true, models, store)
	query := "月夜思乡"

	response, err := service.Search(
		context.Background(), model.SearchRequest{Query: &query},
	)
	if err != nil {
		t.Fatal(err)
	}
	if response.Poem.ID != "tangsong:1" {
		t.Fatalf("unexpected fallback: %#v", response)
	}
}

func TestRerankCandidateLookupFailureFallsBackToANNFirst(t *testing.T) {
	models := &fakeModels{rerankIndex: 1}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1), testHit(2)}}
	service := newTestServiceWithRepository(
		t, true, models, store, partialRepository{failRowID: 2},
	)
	query := "月夜思乡"

	response, err := service.Search(
		context.Background(), model.SearchRequest{Query: &query},
	)
	if err != nil {
		t.Fatal(err)
	}
	if models.rerankCalls != 0 || response.Poem.ID != "tangsong:1" {
		t.Fatalf("unexpected candidate fallback: %#v", response)
	}
}

func TestRerankSkipsBrokenSecondaryCandidate(t *testing.T) {
	models := &fakeModels{rerankIndex: 1}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{
		testHit(1), testHit(2), testHit(3),
	}}
	service := newTestServiceWithRepository(
		t, true, models, store, partialRepository{failRowID: 2},
	)
	query := "月夜思乡"
	response, err := service.Search(
		context.Background(), model.SearchRequest{Query: &query},
	)
	if err != nil {
		t.Fatal(err)
	}
	if models.rerankCalls != 1 || response.Poem.ID != "tangsong:3" {
		t.Fatalf("unexpected partial rerank result: %#v", response)
	}
}

func TestVectorCacheKeyIgnoresQueryWhenRerankIsDisabled(t *testing.T) {
	models := &fakeModels{}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1)}}
	service := newTestService(t, false, models, store)
	firstQuery, secondQuery := "想家", "思乡"
	for _, query := range []*string{&firstQuery, &secondQuery} {
		_, err := service.Search(context.Background(), model.SearchRequest{
			Query: query, Vector: unitVector(), EmbeddingProfile: model.EmbeddingProfile,
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	if store.calls != 1 {
		t.Fatalf("equivalent vector requests searched %d times", store.calls)
	}
}

func TestTextSearchWithoutModelIsUnavailable(t *testing.T) {
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1)}}
	service := newTestService(t, false, nil, store)
	query := "想家"

	_, err := service.Search(context.Background(), model.SearchRequest{Query: &query})
	if !errors.Is(err, ErrUnavailable) {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestVectorSearchWithoutModelSkipsRerank(t *testing.T) {
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1), testHit(2)}}
	service := newTestService(t, true, nil, store)
	query := "月夜思乡"
	_, err := service.Search(context.Background(), model.SearchRequest{
		Query: &query, Vector: unitVector(), EmbeddingProfile: model.EmbeddingProfile,
	})
	if err != nil {
		t.Fatal(err)
	}
	if store.limit != 1 {
		t.Fatalf("vector search requested %d candidates without a reranker", store.limit)
	}
}

func TestCanceledWaiterDoesNotCancelSharedSearch(t *testing.T) {
	models := &blockingModels{started: make(chan struct{}), release: make(chan struct{})}
	store := &fakeVectorStore{hits: []qdrantstore.Hit{testHit(1)}}
	service := newTestService(t, false, models, store)
	query := "想家"
	request := model.SearchRequest{Query: &query}
	firstCtx, cancel := context.WithCancel(context.Background())
	firstDone := make(chan error, 1)
	go func() {
		_, err := service.Search(firstCtx, request)
		firstDone <- err
	}()
	<-models.started
	cancel()
	if err := <-firstDone; !errors.Is(err, ErrUnavailable) {
		t.Fatalf("unexpected canceled waiter error: %v", err)
	}
	secondDone := make(chan error, 1)
	go func() {
		_, err := service.Search(context.Background(), request)
		secondDone <- err
	}()
	time.Sleep(10 * time.Millisecond)
	close(models.release)
	if err := <-secondDone; err != nil {
		t.Fatalf("shared search was canceled: %v", err)
	}
	if models.calls.Load() != 1 {
		t.Fatalf("embedding was not shared: %d calls", models.calls.Load())
	}
}

func newTestService(
	t *testing.T,
	rerank bool,
	models Models,
	store VectorStore,
) *Service {
	return newTestServiceWithRepository(t, rerank, models, store, fakeRepository{})
}

func newTestServiceWithRepository(
	t *testing.T,
	rerank bool,
	models Models,
	store VectorStore,
	repo Repository,
) *Service {
	t.Helper()
	service, err := New(Settings{
		Generation: "test-v1", EmbeddingProfile: model.EmbeddingProfile,
		RerankModel: "BAAI/bge-reranker-v2-m3", RerankEnabled: rerank,
		ResponseCacheBytes: 1 << 20, VectorCacheBytes: 1 << 20,
		ResponseCacheTTL: time.Hour,
	}, models, store, repo)
	if err != nil {
		t.Fatal(err)
	}
	return service
}

func testHit(rowID int64) qdrantstore.Hit {
	return qdrantstore.Hit{
		Score: 0.9, Dataset: "tangsong", SourceRowID: rowID,
		RawIndex: 0, NormalizedIndex: 0,
		WorkID: "tangsong:" + strconv.FormatInt(rowID, 10), Generation: "test-v1",
	}
}

func unitVector() []float32 {
	result := make([]float32, model.VectorDimension)
	result[0] = 1
	return result
}
