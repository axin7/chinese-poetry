package importer

import (
	"context"
	"path/filepath"
	"sync"
	"testing"

	"github.com/qdrant/go-client/qdrant"

	"github.com/chinese-poetry/chinese-poetry/internal/repository"
)

type fakeRepository struct {
	sentences []repository.IndexableSentence
}

func (f *fakeRepository) IterIndexable(
	ctx context.Context,
	_ []string,
	_ int,
	yield func(repository.IndexableSentence) error,
) error {
	for _, sentence := range f.sentences {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := yield(sentence); err != nil {
			return err
		}
	}
	return nil
}

type fakeEmbedder struct {
	mu      sync.Mutex
	batches [][]string
}

func (f *fakeEmbedder) EmbedMany(
	_ context.Context,
	texts []string,
) ([][]float32, error) {
	f.mu.Lock()
	f.batches = append(f.batches, append([]string(nil), texts...))
	f.mu.Unlock()
	result := make([][]float32, len(texts))
	for index := range texts {
		result[index] = []float32{float32(index + 1), 0}
	}
	return result, nil
}

type fakeStore struct {
	dimension uint64
	batches   [][]*qdrant.PointStruct
	count     uint64
}

func (f *fakeStore) EnsureCollection(_ context.Context, dimension uint64) error {
	f.dimension = dimension
	return nil
}

func (f *fakeStore) Upsert(_ context.Context, points []*qdrant.PointStruct) error {
	f.batches = append(f.batches, append([]*qdrant.PointStruct(nil), points...))
	f.count += uint64(len(points))
	return nil
}

func (*fakeStore) ValidateGenerationIsolation(context.Context) error { return nil }

func (f *fakeStore) Count(context.Context, []string) (uint64, error) {
	return f.count, nil
}

func TestImporterWritesPointsAndResumes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	repo := &fakeRepository{sentences: testSentences()}
	embedder := &fakeEmbedder{}
	store := &fakeStore{}
	config := testConfig(path)

	first, err := New(config, repo, embedder, store).Run(
		context.Background(), []string{"tangsong"}, 2, false,
	)
	if err != nil {
		t.Fatal(err)
	}
	if first != 2 || store.dimension != 1024 || len(store.batches) != 1 {
		t.Fatalf("unexpected first import: %d %#v", first, store)
	}

	second, err := New(config, repo, embedder, store).Run(
		context.Background(), []string{"tangsong"}, 3, false,
	)
	if err != nil {
		t.Fatal(err)
	}
	if second != 3 || len(store.batches) != 2 || len(store.batches[1]) != 1 {
		t.Fatalf("unexpected resumed import: %d %#v", second, store.batches)
	}
}

func TestCompletedImporterDoesNotRescan(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	repo := &countingRepository{fakeRepository: fakeRepository{sentences: testSentences()}}
	worker := New(testConfig(path), repo, &fakeEmbedder{}, &fakeStore{})
	if _, err := worker.Run(context.Background(), []string{"tangsong"}, 0, false); err != nil {
		t.Fatal(err)
	}
	if _, err := worker.Run(context.Background(), []string{"tangsong"}, 0, false); err != nil {
		t.Fatal(err)
	}
	if repo.iterations != 1 {
		t.Fatalf("completed import scanned source %d times", repo.iterations)
	}
}

func TestImporterRejectsExtraQdrantPointsOnCompletion(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	store := &fakeStore{count: 1}
	worker := New(testConfig(path), &fakeRepository{}, &fakeEmbedder{}, store)
	if _, err := worker.Run(context.Background(), []string{"tangsong"}, 0, false); err == nil {
		t.Fatal("expected completed point count mismatch")
	}
}

type countingRepository struct {
	fakeRepository
	iterations int
}

func (repository *countingRepository) IterIndexable(
	ctx context.Context,
	datasets []string,
	batchSize int,
	yield func(repository.IndexableSentence) error,
) error {
	repository.iterations++
	return repository.fakeRepository.IterIndexable(ctx, datasets, batchSize, yield)
}

func TestPointIDMatchesPythonUUIDv5(t *testing.T) {
	sentence := repository.IndexableSentence{
		Dataset: "tangsong", SourceRowID: 7, RawIndex: 2, NormalizedIndex: 1,
	}
	got := pointID("v1", sentence).String()
	want := "a118c345-2319-5907-aa39-2e047233f3e1"
	if got != want {
		t.Fatalf("point ID mismatch: got %s want %s", got, want)
	}
}

func TestImporterRejectsCheckpointAheadOfQdrant(t *testing.T) {
	path := filepath.Join(t.TempDir(), "progress.json")
	checkpoint := NewCheckpointStore(path, "v1", "sha256:source")
	if err := checkpoint.Save(Progress{
		Generation: "v1", SourceFingerprint: "sha256:source",
		Scope:   []string{"tangsong"},
		LastKey: &Key{Dataset: "tangsong", RowID: 1, RawIndex: 0}, Indexed: 1,
	}); err != nil {
		t.Fatal(err)
	}
	worker := New(testConfig(path), &fakeRepository{}, &fakeEmbedder{}, &fakeStore{})
	_, err := worker.Run(context.Background(), []string{"tangsong"}, 0, false)
	if err == nil {
		t.Fatal("expected checkpoint consistency error")
	}
}

func TestBuildPointsPreservesLocatorPayload(t *testing.T) {
	sentence := testSentences()[0]
	points, err := buildPoints("v1", []repository.IndexableSentence{sentence},
		[][]float32{{1, 0}})
	if err != nil {
		t.Fatal(err)
	}
	payload := points[0].GetPayload()
	if payload["work_id"].GetStringValue() != sentence.WorkID ||
		payload["source_row_id"].GetIntegerValue() != sentence.SourceRowID {
		t.Fatalf("unexpected point payload: %v", payload)
	}
}

func testConfig(path string) Config {
	return Config{
		Generation:           "v1",
		SourceFingerprint:    "sha256:source",
		VectorDimension:      1024,
		EmbeddingBatchSize:   1,
		EmbeddingConcurrency: 2,
		QdrantBatchSize:      2,
		CheckpointPath:       path,
	}
}

func testSentences() []repository.IndexableSentence {
	return []repository.IndexableSentence{
		{
			Dataset: "tangsong", SourceRowID: 1, RawIndex: 0,
			NormalizedIndex: 0, WorkID: "tangsong:1", Translation: "第一句",
		},
		{
			Dataset: "tangsong", SourceRowID: 1, RawIndex: 1,
			NormalizedIndex: 1, WorkID: "tangsong:1", Translation: "第二句",
		},
		{
			Dataset: "tangsong", SourceRowID: 2, RawIndex: 0,
			NormalizedIndex: 0, WorkID: "tangsong:2", Translation: "第三句",
		},
	}
}
