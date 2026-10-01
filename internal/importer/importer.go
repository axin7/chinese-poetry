package importer

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/qdrant/go-client/qdrant"
	"golang.org/x/sync/errgroup"

	"github.com/chinese-poetry/chinese-poetry/internal/repository"
)

var pointNamespace = uuid.MustParse("cc0fab8e-ff7c-49f2-809d-d96a73b5e4fc")

type Config struct {
	Generation           string
	SourceFingerprint    string
	VectorDimension      int
	EmbeddingBatchSize   int
	EmbeddingConcurrency int
	QdrantBatchSize      int
	CheckpointPath       string
}

type Repository interface {
	IterIndexable(
		context.Context,
		[]string,
		int,
		func(repository.IndexableSentence) error,
	) error
}

type Embedder interface {
	EmbedMany(context.Context, []string) ([][]float32, error)
}

type VectorStore interface {
	EnsureCollection(context.Context, uint64) error
	ValidateGenerationIsolation(context.Context) error
	Count(context.Context, []string) (uint64, error)
	Upsert(context.Context, []*qdrant.PointStruct) error
}

type Importer struct {
	config      Config
	repository  Repository
	embedder    Embedder
	store       VectorStore
	checkpoints *CheckpointStore
}

func New(config Config, repo Repository, embedder Embedder, store VectorStore) *Importer {
	return &Importer{
		config:     config,
		repository: repo,
		embedder:   embedder,
		store:      store,
		checkpoints: NewCheckpointStore(
			config.CheckpointPath, config.Generation, config.SourceFingerprint,
		),
	}
}

func (i *Importer) Run(
	ctx context.Context,
	datasets []string,
	maxSentences int64,
	reset bool,
) (int64, error) {
	if err := i.validate(maxSentences); err != nil {
		return 0, err
	}
	unlock, err := i.checkpoints.Lock()
	if err != nil {
		return 0, err
	}
	defer unlock()
	if err := i.store.EnsureCollection(ctx, uint64(i.config.VectorDimension)); err != nil {
		return 0, err
	}
	if err := i.store.ValidateGenerationIsolation(ctx); err != nil {
		return 0, err
	}
	scope := append([]string(nil), datasets...)
	if scope == nil {
		scope = []string{"__all__"}
	}
	progress, err := i.checkpoints.Load(reset, scope)
	if err != nil {
		return 0, err
	}
	count, err := i.store.Count(ctx, datasets)
	if err != nil {
		return 0, err
	}
	if uint64(progress.Indexed) > count {
		return 0, fmt.Errorf(
			"checkpoint is ahead of Qdrant; rerun with --reset-checkpoint",
		)
	}
	if reset {
		if err := i.checkpoints.Save(progress); err != nil {
			return 0, err
		}
	}
	if progress.Completed {
		if uint64(progress.Indexed) != count {
			return 0, fmt.Errorf("completed checkpoint does not match Qdrant point count")
		}
		return progress.Indexed, nil
	}
	if maxSentences > 0 && progress.Indexed >= maxSentences {
		return progress.Indexed, nil
	}
	completed, err := i.iterate(ctx, datasets, maxSentences, &progress)
	if err != nil || !completed {
		return progress.Indexed, err
	}
	if err := i.verifyCompletedCount(ctx, datasets, progress.Indexed); err != nil {
		return progress.Indexed, err
	}
	progress.Completed = true
	if err := i.checkpoints.Save(progress); err != nil {
		return progress.Indexed, err
	}
	return progress.Indexed, nil
}

func (i *Importer) verifyCompletedCount(
	ctx context.Context,
	datasets []string,
	indexed int64,
) error {
	count, err := i.store.Count(ctx, datasets)
	if err != nil {
		return err
	}
	if uint64(indexed) != count {
		return fmt.Errorf("completed import does not match Qdrant point count")
	}
	return nil
}

func (i *Importer) iterate(
	ctx context.Context,
	datasets []string,
	maxSentences int64,
	progress *Progress,
) (bool, error) {
	pending := make([]repository.IndexableSentence, 0, i.config.QdrantBatchSize)
	resumeFound := progress.LastKey == nil
	err := i.repository.IterIndexable(ctx, datasets, i.config.QdrantBatchSize,
		func(sentence repository.IndexableSentence) error {
			if !resumeFound {
				resumeFound = equalKey(keyOf(sentence), *progress.LastKey)
				return nil
			}
			pending = append(pending, sentence)
			limitReached := maxSentences > 0 &&
				int64(len(pending))+progress.Indexed >= maxSentences
			if len(pending) >= i.config.QdrantBatchSize || limitReached {
				if err := i.flush(ctx, pending, progress); err != nil {
					return err
				}
				pending = pending[:0]
			}
			if limitReached {
				return errStop
			}
			return nil
		})
	if err != nil && err != errStop {
		return false, err
	}
	if err == nil && len(pending) > 0 {
		if flushErr := i.flush(ctx, pending, progress); flushErr != nil {
			return false, flushErr
		}
	}
	if !resumeFound {
		return false, fmt.Errorf("checkpoint key does not exist in selected datasets")
	}
	return err == nil, nil
}

var errStop = fmt.Errorf("import limit reached")

func (i *Importer) flush(
	ctx context.Context,
	sentences []repository.IndexableSentence,
	progress *Progress,
) error {
	vectors, err := i.embedBatches(ctx, sentences)
	if err != nil {
		return err
	}
	points, err := buildPoints(i.config.Generation, sentences, vectors)
	if err != nil {
		return err
	}
	if err := i.store.Upsert(ctx, points); err != nil {
		return err
	}
	lastKey := keyOf(sentences[len(sentences)-1])
	updated := Progress{
		Generation: progress.Generation, SourceFingerprint: progress.SourceFingerprint,
		Scope: progress.Scope, LastKey: &lastKey,
		Indexed: progress.Indexed + int64(len(sentences)),
	}
	if err := i.checkpoints.Save(updated); err != nil {
		return err
	}
	*progress = updated
	log.Printf("import checkpoint saved: indexed=%d dataset=%s row_id=%d raw_index=%d",
		updated.Indexed, lastKey.Dataset, lastKey.RowID, lastKey.RawIndex)
	return nil
}

func (i *Importer) embedBatches(
	ctx context.Context,
	sentences []repository.IndexableSentence,
) ([][]float32, error) {
	batchSize := i.config.EmbeddingBatchSize
	count := (len(sentences) + batchSize - 1) / batchSize
	results := make([][][]float32, count)
	group, groupCtx := errgroup.WithContext(ctx)
	group.SetLimit(i.config.EmbeddingConcurrency)
	for batchIndex := 0; batchIndex < count; batchIndex++ {
		batchIndex := batchIndex
		start := batchIndex * batchSize
		end := min(start+batchSize, len(sentences))
		group.Go(func() error {
			texts := make([]string, end-start)
			for index := start; index < end; index++ {
				texts[index-start] = sentences[index].Translation
			}
			vectors, err := i.embedWithRetry(groupCtx, texts)
			if err == nil && len(vectors) != len(texts) {
				return fmt.Errorf("embedding result count mismatch")
			}
			results[batchIndex] = vectors
			return err
		})
	}
	if err := group.Wait(); err != nil {
		return nil, fmt.Errorf("embed import batch: %w", err)
	}
	return flatten(results), nil
}

func flatten(batches [][][]float32) [][]float32 {
	var total int
	for _, batch := range batches {
		total += len(batch)
	}
	result := make([][]float32, 0, total)
	for _, batch := range batches {
		result = append(result, batch...)
	}
	return result
}

func buildPoints(
	generation string,
	sentences []repository.IndexableSentence,
	vectors [][]float32,
) ([]*qdrant.PointStruct, error) {
	if len(sentences) != len(vectors) {
		return nil, fmt.Errorf("sentence and vector counts do not match")
	}
	points := make([]*qdrant.PointStruct, len(sentences))
	for index, sentence := range sentences {
		payload := qdrant.NewValueMap(map[string]any{
			"dataset":          sentence.Dataset,
			"source_row_id":    sentence.SourceRowID,
			"raw_index":        int64(sentence.RawIndex),
			"normalized_index": int64(sentence.NormalizedIndex),
			"work_id":          sentence.WorkID,
			"generation":       generation,
			"translation_hash": translationHash(sentence.Translation),
		})
		points[index] = &qdrant.PointStruct{
			Id:      qdrant.NewID(pointID(generation, sentence).String()),
			Vectors: qdrant.NewVectors(vectors[index]...),
			Payload: payload,
		}
	}
	return points, nil
}

func pointID(generation string, sentence repository.IndexableSentence) uuid.UUID {
	parts := []string{
		generation,
		sentence.Dataset,
		strconv.FormatInt(sentence.SourceRowID, 10),
		strconv.Itoa(sentence.RawIndex),
		strconv.Itoa(sentence.NormalizedIndex),
	}
	return uuid.NewSHA1(pointNamespace, []byte(strings.Join(parts, ":")))
}

func translationHash(value string) string {
	digest := sha256.Sum256([]byte(value))
	return fmt.Sprintf("%x", digest)
}

func keyOf(sentence repository.IndexableSentence) Key {
	return Key{
		Dataset: sentence.Dataset, RowID: sentence.SourceRowID, RawIndex: sentence.RawIndex,
	}
}

func equalKey(left, right Key) bool {
	return left == right
}

func (i *Importer) validate(maxSentences int64) error {
	if i.config.Generation == "" || i.config.SourceFingerprint == "" ||
		i.config.CheckpointPath == "" ||
		i.config.VectorDimension <= 0 || i.config.EmbeddingBatchSize <= 0 ||
		i.config.EmbeddingConcurrency <= 0 || i.config.QdrantBatchSize <= 0 {
		return fmt.Errorf("importer configuration is invalid")
	}
	if maxSentences < 0 {
		return fmt.Errorf("max sentences must not be negative")
	}
	if i.repository == nil || i.embedder == nil || i.store == nil {
		return fmt.Errorf("importer dependencies are required")
	}
	return nil
}
