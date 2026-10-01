package qdrantstore

import (
	"context"
	"fmt"

	"github.com/qdrant/go-client/qdrant"

	"github.com/chinese-poetry/chinese-poetry/internal/model"
)

func (s *Store) Health(ctx context.Context) error {
	exists, err := s.client.CollectionExists(ctx, s.collection)
	if err != nil {
		return fmt.Errorf("check Qdrant collection: %w", err)
	}
	if !exists {
		return fmt.Errorf("Qdrant collection %q does not exist", s.collection)
	}
	info, err := s.validateCollection(ctx, model.VectorDimension)
	if err != nil {
		return err
	}
	if err := validateReadyCollection(info); err != nil {
		return err
	}
	count, err := s.isolatedGenerationCount(ctx)
	if err != nil {
		return err
	}
	if count == 0 {
		return fmt.Errorf("Qdrant generation %q is empty", s.generation)
	}
	return validateSearchCoverage(info, count)
}

func validateReadyCollection(info *qdrant.CollectionInfo) error {
	if info.GetStatus() != qdrant.CollectionStatus_Green {
		return fmt.Errorf("Qdrant collection is not ready: %s", info.GetStatus())
	}
	optimizer := info.GetOptimizerStatus()
	if !optimizer.GetOk() || optimizer.GetError() != "" {
		return fmt.Errorf("Qdrant collection optimizer is not healthy")
	}
	for _, field := range []string{"dataset", "generation"} {
		index := info.GetPayloadSchema()[field]
		if index.GetDataType() != qdrant.PayloadSchemaType_Keyword {
			return fmt.Errorf("Qdrant payload index %q must exist and be keyword", field)
		}
	}
	return nil
}

func validateSearchCoverage(info *qdrant.CollectionInfo, count uint64) error {
	if info.IndexedVectorsCount == nil {
		return fmt.Errorf("Qdrant indexed vector count is unavailable")
	}
	indexed := info.GetIndexedVectorsCount()
	if indexed >= count {
		return nil
	}
	params := info.GetConfig().GetParams().GetVectorsConfig().GetParams()
	threshold := unindexedSearchThreshold(info.GetConfig(), params)
	// Qdrant also searches plain segments small enough for a full scan with indexed_only.
	// Bounding all remaining vectors guarantees each such segment is below that threshold.
	maxUnindexed := threshold / (params.GetSize() * 4)
	if count-indexed > maxUnindexed {
		return fmt.Errorf("Qdrant indexing is incomplete: %d of %d vectors indexed", indexed, count)
	}
	return nil
}

func unindexedSearchThreshold(
	config *qdrant.CollectionConfig,
	params *qdrant.VectorParams,
) uint64 {
	indexingKB, scanKB := uint64(20000), uint64(10000)
	optimizer := config.GetOptimizerConfig()
	if optimizer != nil && optimizer.IndexingThreshold != nil {
		indexingKB = optimizer.GetIndexingThreshold()
	}
	hnsw := config.GetHnswConfig()
	if params.GetHnswConfig() != nil {
		hnsw = params.GetHnswConfig()
	}
	if hnsw != nil && hnsw.FullScanThreshold != nil {
		scanKB = hnsw.GetFullScanThreshold()
	}
	return max(indexingKB, scanKB) * 1024
}
