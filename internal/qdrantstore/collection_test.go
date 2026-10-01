package qdrantstore

import (
	"context"
	"testing"

	"github.com/qdrant/go-client/qdrant"
)

func TestEnsureCollectionCreatesOfficialConfiguration(t *testing.T) {
	client := &fakeClient{}
	store := New(client, "poetry", "v1", 64)

	if err := store.EnsureCollection(context.Background(), 1024); err != nil {
		t.Fatal(err)
	}
	request := client.created
	params := request.GetVectorsConfig().GetParams()
	if params.GetSize() != 1024 || params.GetDistance() != qdrant.Distance_Cosine {
		t.Fatalf("unexpected vectors config: %v", params)
	}
	if !params.GetOnDisk() || request.GetHnswConfig().GetOnDisk() {
		t.Fatal("unexpected disk settings")
	}
	scalar := request.GetQuantizationConfig().GetScalar()
	if scalar.GetType() != qdrant.QuantizationType_Int8 || !scalar.GetAlwaysRam() {
		t.Fatalf("unexpected quantization config: %v", scalar)
	}
	if len(client.indexes) != 2 {
		t.Fatalf("expected two payload indexes, got %d", len(client.indexes))
	}
}

func TestEnsureCollectionValidatesExistingDimension(t *testing.T) {
	client := &fakeClient{
		exists: true,
		info:   collectionInfo(768, qdrant.Distance_Cosine),
	}
	store := New(client, "poetry", "v1", 64)

	if err := store.EnsureCollection(context.Background(), 1024); err == nil {
		t.Fatal("expected dimension mismatch")
	}
}

func TestEnsureCollectionAcceptsMatchingCollection(t *testing.T) {
	client := &fakeClient{
		exists: true,
		info:   collectionInfo(1024, qdrant.Distance_Cosine),
	}
	store := New(client, "poetry", "v1", 64)

	if err := store.EnsureCollection(context.Background(), 1024); err != nil {
		t.Fatal(err)
	}
	if client.created != nil {
		t.Fatal("existing collection must not be recreated")
	}
	if len(client.indexes) != 2 {
		t.Fatalf("missing indexes were not repaired: %d", len(client.indexes))
	}
}

func TestEnsureCollectionKeepsExistingPayloadIndexes(t *testing.T) {
	info := collectionInfo(1024, qdrant.Distance_Cosine)
	info.PayloadSchema = map[string]*qdrant.PayloadSchemaInfo{
		"dataset":    {DataType: qdrant.PayloadSchemaType_Keyword},
		"generation": {DataType: qdrant.PayloadSchemaType_Keyword},
	}
	client := &fakeClient{exists: true, info: info}
	store := New(client, "poetry", "v1", 64)

	if err := store.EnsureCollection(context.Background(), 1024); err != nil {
		t.Fatal(err)
	}
	if len(client.indexes) != 0 {
		t.Fatalf("existing indexes were recreated: %d", len(client.indexes))
	}
}

func TestEnsureCollectionRejectsWrongPayloadIndexType(t *testing.T) {
	info := collectionInfo(1024, qdrant.Distance_Cosine)
	info.PayloadSchema = map[string]*qdrant.PayloadSchemaInfo{
		"dataset": {DataType: qdrant.PayloadSchemaType_Text},
	}
	store := New(&fakeClient{exists: true, info: info}, "poetry", "v1", 64)
	if err := store.EnsureCollection(context.Background(), 1024); err == nil {
		t.Fatal("expected payload index type mismatch")
	}
}

func TestEnsureCollectionRejectsIncompatibleStorage(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*qdrant.CollectionInfo)
	}{
		{name: "vectors in memory", mutate: func(info *qdrant.CollectionInfo) {
			value := false
			info.Config.Params.VectorsConfig.GetParams().OnDisk = &value
		}},
		{name: "payload in memory", mutate: func(info *qdrant.CollectionInfo) {
			info.Config.Params.OnDiskPayload = false
		}},
		{name: "HNSW on disk", mutate: func(info *qdrant.CollectionInfo) {
			value := true
			info.Config.HnswConfig.OnDisk = &value
		}},
		{name: "quantization not resident", mutate: func(info *qdrant.CollectionInfo) {
			value := false
			info.Config.QuantizationConfig.GetScalar().AlwaysRam = &value
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			info := collectionInfo(1024, qdrant.Distance_Cosine)
			test.mutate(info)
			store := New(&fakeClient{exists: true, info: info}, "poetry", "v1", 64)
			if err := store.EnsureCollection(context.Background(), 1024); err == nil {
				t.Fatal("expected collection configuration mismatch")
			}
		})
	}
}

func TestHealthRequiresCollection(t *testing.T) {
	store := New(&fakeClient{}, "poetry", "v1", 64)
	if err := store.Health(context.Background()); err == nil {
		t.Fatal("expected missing collection error")
	}
}

func TestHealthRequiresCurrentGenerationPoints(t *testing.T) {
	client := &fakeClient{exists: true, info: readyCollection(0), countResults: []uint64{0, 0}}
	store := New(client, "poetry", "v1", 64)
	if err := store.Health(context.Background()); err == nil {
		t.Fatal("expected empty generation error")
	}
}

func TestGenerationIsolationRejectsMixedCollection(t *testing.T) {
	client := &fakeClient{countResults: []uint64{10, 4}}
	store := New(client, "poetry", "v2", 64)
	if err := store.ValidateGenerationIsolation(context.Background()); err == nil {
		t.Fatal("expected mixed generation error")
	}
}

func collectionInfo(size uint64, distance qdrant.Distance) *qdrant.CollectionInfo {
	onDisk := true
	hnswOnDisk := false
	alwaysRAM := true
	m := uint64(16)
	return &qdrant.CollectionInfo{Config: &qdrant.CollectionConfig{
		Params: &qdrant.CollectionParams{
			OnDiskPayload: true,
			VectorsConfig: qdrant.NewVectorsConfig(&qdrant.VectorParams{
				Size: size, Distance: distance, OnDisk: &onDisk,
			}),
		},
		HnswConfig: &qdrant.HnswConfigDiff{M: &m, OnDisk: &hnswOnDisk},
		QuantizationConfig: qdrant.NewQuantizationScalar(&qdrant.ScalarQuantization{
			Type: qdrant.QuantizationType_Int8, AlwaysRam: &alwaysRAM,
		}),
	}}
}
