package qdrantstore

import (
	"context"
	"fmt"

	"github.com/qdrant/go-client/qdrant"
)

func (s *Store) EnsureCollection(ctx context.Context, dimension uint64) error {
	exists, err := s.client.CollectionExists(ctx, s.collection)
	if err != nil {
		return fmt.Errorf("check collection: %w", err)
	}
	if exists {
		info, err := s.validateCollection(ctx, dimension)
		if err != nil {
			return err
		}
		return s.createPayloadIndexes(ctx, info.GetPayloadSchema())
	}
	if err := s.createCollection(ctx, dimension); err != nil {
		return err
	}
	return s.createPayloadIndexes(ctx, nil)
}

func (s *Store) validateCollection(
	ctx context.Context,
	dimension uint64,
) (*qdrant.CollectionInfo, error) {
	info, err := s.client.GetCollectionInfo(ctx, s.collection)
	if err != nil {
		return nil, fmt.Errorf("get collection: %w", err)
	}
	if err := validateCollectionConfig(info.GetConfig(), dimension); err != nil {
		return nil, err
	}
	return info, nil
}

func validateCollectionConfig(config *qdrant.CollectionConfig, dimension uint64) error {
	collectionParams := config.GetParams()
	params := collectionParams.GetVectorsConfig().GetParams()
	if params == nil || params.GetSize() != dimension {
		return fmt.Errorf("existing Qdrant collection vector dimension mismatch")
	}
	if params.GetDistance() != qdrant.Distance_Cosine {
		return fmt.Errorf("existing Qdrant collection distance must be cosine")
	}
	if !params.GetOnDisk() || !collectionParams.GetOnDiskPayload() {
		return fmt.Errorf("existing Qdrant collection disk settings mismatch")
	}
	hnsw := config.GetHnswConfig()
	if params.GetHnswConfig() != nil {
		hnsw = params.GetHnswConfig()
	}
	if hnsw == nil || hnsw.GetM() != 16 || hnsw.GetOnDisk() {
		return fmt.Errorf("existing Qdrant collection HNSW settings mismatch")
	}
	quantization := config.GetQuantizationConfig()
	if params.GetQuantizationConfig() != nil {
		quantization = params.GetQuantizationConfig()
	}
	scalar := quantization.GetScalar()
	if scalar == nil || scalar.GetType() != qdrant.QuantizationType_Int8 ||
		!scalar.GetAlwaysRam() {
		return fmt.Errorf("existing Qdrant collection quantization settings mismatch")
	}
	return nil
}

func (s *Store) createCollection(ctx context.Context, dimension uint64) error {
	onDisk := true
	hnswOnDisk := false
	onDiskPayload := true
	alwaysRAM := true
	m := uint64(16)
	request := &qdrant.CreateCollection{
		CollectionName: s.collection,
		VectorsConfig: qdrant.NewVectorsConfig(&qdrant.VectorParams{
			Size:     dimension,
			Distance: qdrant.Distance_Cosine,
			OnDisk:   &onDisk,
		}),
		HnswConfig: &qdrant.HnswConfigDiff{
			M:      &m,
			OnDisk: &hnswOnDisk,
		},
		QuantizationConfig: qdrant.NewQuantizationScalar(
			&qdrant.ScalarQuantization{
				Type:      qdrant.QuantizationType_Int8,
				AlwaysRam: &alwaysRAM,
			},
		),
		OnDiskPayload: &onDiskPayload,
	}
	if err := s.client.CreateCollection(ctx, request); err != nil {
		return fmt.Errorf("create collection: %w", err)
	}
	return nil
}

func (s *Store) createPayloadIndexes(
	ctx context.Context,
	existing map[string]*qdrant.PayloadSchemaInfo,
) error {
	wait := true
	for _, field := range []string{"dataset", "generation"} {
		if schema, ok := existing[field]; ok {
			if schema.GetDataType() != qdrant.PayloadSchemaType_Keyword {
				return fmt.Errorf("payload index %q must be keyword", field)
			}
			continue
		}
		_, err := s.client.CreateFieldIndex(
			ctx,
			&qdrant.CreateFieldIndexCollection{
				CollectionName: s.collection,
				FieldName:      field,
				FieldType:      qdrant.FieldType_FieldTypeKeyword.Enum(),
				Wait:           &wait,
			},
		)
		if err != nil {
			return fmt.Errorf("create payload index %q: %w", field, err)
		}
	}
	return nil
}

func (s *Store) Upsert(ctx context.Context, points []*qdrant.PointStruct) error {
	wait := true
	_, err := s.client.Upsert(ctx, &qdrant.UpsertPoints{
		CollectionName: s.collection,
		Wait:           &wait,
		Points:         points,
	})
	if err != nil {
		return fmt.Errorf("upsert Qdrant points: %w", err)
	}
	return nil
}
