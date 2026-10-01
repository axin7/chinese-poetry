package qdrantstore

import (
	"context"
	"strings"
	"testing"

	"github.com/qdrant/go-client/qdrant"
)

func readyCollection(indexed uint64) *qdrant.CollectionInfo {
	info := collectionInfo(1024, qdrant.Distance_Cosine)
	info.Status = qdrant.CollectionStatus_Green
	info.OptimizerStatus = &qdrant.OptimizerStatus{Ok: true}
	info.IndexedVectorsCount = &indexed
	info.PayloadSchema = map[string]*qdrant.PayloadSchemaInfo{
		"dataset":    {DataType: qdrant.PayloadSchemaType_Keyword},
		"generation": {DataType: qdrant.PayloadSchemaType_Keyword},
	}
	indexingKB, scanKB := uint64(1000), uint64(10000)
	info.Config.OptimizerConfig = &qdrant.OptimizersConfigDiff{IndexingThreshold: &indexingKB}
	info.Config.HnswConfig.FullScanThreshold = &scanKB
	return info
}

func TestHealthValidatesWithoutMutatingCollection(t *testing.T) {
	client := &fakeClient{
		exists: true, info: readyCollection(127031), countResults: []uint64{127031, 127031},
	}
	if err := New(client, "poetry", "v1", 64).Health(context.Background()); err != nil {
		t.Fatal(err)
	}
	if client.created != nil || len(client.indexes) != 0 || client.upserted != nil {
		t.Fatal("health check must not mutate collection")
	}
	if len(client.countRequests) != 2 {
		t.Fatalf("expected total and generation counts, got %d", len(client.countRequests))
	}
	for _, request := range client.countRequests {
		if !request.GetExact() {
			t.Fatal("readiness requires exact counts")
		}
	}
	if client.countRequests[0].GetFilter() != nil ||
		len(client.countRequests[1].GetFilter().GetMust()) != 1 {
		t.Fatal("readiness must compare total points with the configured generation")
	}
}

func TestHealthRejectsInvalidCollectionState(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*qdrant.CollectionInfo)
	}{
		{"wrong dimension", func(info *qdrant.CollectionInfo) {
			info.Config.Params.VectorsConfig.GetParams().Size = 768
		}},
		{"optimizing", func(info *qdrant.CollectionInfo) {
			info.Status = qdrant.CollectionStatus_Yellow
		}},
		{"pending", func(info *qdrant.CollectionInfo) { info.Status = qdrant.CollectionStatus_Grey }},
		{"optimizer failed", func(info *qdrant.CollectionInfo) { info.OptimizerStatus.Ok = false }},
		{"optimizer error", func(info *qdrant.CollectionInfo) { info.OptimizerStatus.Error = "failed" }},
		{"optimizer missing", func(info *qdrant.CollectionInfo) { info.OptimizerStatus = nil }},
		{"index missing", func(info *qdrant.CollectionInfo) {
			delete(info.PayloadSchema, "generation")
		}},
		{"wrong index type", func(info *qdrant.CollectionInfo) {
			info.PayloadSchema["dataset"].DataType = qdrant.PayloadSchemaType_Text
		}},
		{"index count missing", func(info *qdrant.CollectionInfo) { info.IndexedVectorsCount = nil }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			info := readyCollection(127031)
			test.mutate(info)
			client := &fakeClient{exists: true, info: info, countResults: []uint64{127031, 127031}}
			if err := New(client, "poetry", "v1", 64).Health(context.Background()); err == nil {
				t.Fatal("expected readiness error")
			}
		})
	}
}

func TestHealthRejectsMixedGeneration(t *testing.T) {
	client := &fakeClient{exists: true, info: readyCollection(10), countResults: []uint64{10, 4}}
	err := New(client, "poetry", "v1", 64).Health(context.Background())
	if err == nil || !strings.Contains(err.Error(), "another corpus generation") {
		t.Fatalf("expected mixed generation error, got %v", err)
	}
}

func TestHealthAllowsSmallPlainSegments(t *testing.T) {
	tests := []struct {
		name           string
		count, indexed uint64
		wantError      bool
	}{
		{"small collection", 2499, 0, false},
		{"scan threshold", 2500, 0, false},
		{"above threshold", 2501, 0, true},
		{"small remainder", 3500, 1000, false},
		{"large remainder", 3501, 1000, true},
		{"index count overestimate", 3500, 3501, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client := &fakeClient{exists: true, info: readyCollection(test.indexed),
				countResults: []uint64{test.count, test.count}}
			err := New(client, "poetry", "v1", 64).Health(context.Background())
			if (err != nil) != test.wantError {
				t.Fatalf("unexpected readiness error: %v", err)
			}
		})
	}
}

func TestCoverageUsesEffectiveThresholds(t *testing.T) {
	info := readyCollection(0)
	info.Config.OptimizerConfig = nil
	if err := validateSearchCoverage(info, 5000); err != nil {
		t.Fatalf("default indexing threshold should allow this collection: %v", err)
	}
	if err := validateSearchCoverage(info, 5001); err == nil {
		t.Fatal("expected coverage error above default threshold")
	}
	scanKB := uint64(4000)
	info = readyCollection(0)
	info.Config.Params.VectorsConfig.GetParams().HnswConfig = &qdrant.HnswConfigDiff{
		FullScanThreshold: &scanKB,
	}
	if err := validateSearchCoverage(info, 1001); err == nil {
		t.Fatal("vector-specific full scan threshold should override collection settings")
	}
}
