package qdrantstore

import (
	"context"
	"errors"
	"testing"

	"github.com/qdrant/go-client/qdrant"
)

type fakeClient struct {
	queryRequest  *qdrant.QueryPoints
	queryPoints   []*qdrant.ScoredPoint
	queryErr      error
	exists        bool
	info          *qdrant.CollectionInfo
	created       *qdrant.CreateCollection
	indexes       []*qdrant.CreateFieldIndexCollection
	upserted      *qdrant.UpsertPoints
	countResults  []uint64
	countRequests []*qdrant.CountPoints
	closed        bool
}

func (f *fakeClient) Query(
	_ context.Context,
	request *qdrant.QueryPoints,
) ([]*qdrant.ScoredPoint, error) {
	f.queryRequest = request
	return f.queryPoints, f.queryErr
}

func (f *fakeClient) CollectionExists(context.Context, string) (bool, error) {
	return f.exists, nil
}

func (f *fakeClient) GetCollectionInfo(
	context.Context,
	string,
) (*qdrant.CollectionInfo, error) {
	return f.info, nil
}

func (f *fakeClient) CreateCollection(
	_ context.Context,
	request *qdrant.CreateCollection,
) error {
	f.created = request
	return nil
}

func (f *fakeClient) CreateFieldIndex(
	_ context.Context,
	request *qdrant.CreateFieldIndexCollection,
) (*qdrant.UpdateResult, error) {
	f.indexes = append(f.indexes, request)
	return &qdrant.UpdateResult{}, nil
}

func (f *fakeClient) Upsert(
	_ context.Context,
	request *qdrant.UpsertPoints,
) (*qdrant.UpdateResult, error) {
	f.upserted = request
	return &qdrant.UpdateResult{}, nil
}

func (f *fakeClient) Count(
	_ context.Context,
	request *qdrant.CountPoints,
) (uint64, error) {
	f.countRequests = append(f.countRequests, request)
	if len(f.countResults) == 0 {
		return 0, nil
	}
	result := f.countResults[0]
	f.countResults = f.countResults[1:]
	return result, nil
}

func (f *fakeClient) Close() error {
	f.closed = true
	return nil
}

func TestSearchBuildsFastQuantizedQuery(t *testing.T) {
	payload := qdrant.NewValueMap(map[string]any{
		"dataset":          "tangsong",
		"source_row_id":    int64(7),
		"raw_index":        int64(2),
		"normalized_index": int64(1),
		"work_id":          "tangsong:7",
		"generation":       "v1",
	})
	client := &fakeClient{queryPoints: []*qdrant.ScoredPoint{{
		Score:   0.9,
		Payload: payload,
	}}}
	store := New(client, "poetry", "v1", 64)

	hits, err := store.Search(context.Background(), []float32{0.1, 0.2}, nil, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(hits) != 1 || hits[0].WorkID != "tangsong:7" {
		t.Fatalf("unexpected hits: %#v", hits)
	}
	request := client.queryRequest
	if request.GetLimit() != 1 || request.GetParams().GetHnswEf() != 64 {
		t.Fatalf("unexpected query parameters: %v", request)
	}
	if request.GetParams().GetExact() || request.GetParams().GetQuantization().GetRescore() {
		t.Fatal("query must use approximate quantized search without rescore")
	}
	if !request.GetParams().GetIndexedOnly() {
		t.Fatal("query must avoid unindexed segments")
	}
	if len(request.GetFilter().GetMust()) != 1 {
		t.Fatalf("unexpected filter: %v", request.GetFilter())
	}
}

func TestSearchAddsDatasetFilter(t *testing.T) {
	client := &fakeClient{}
	store := New(client, "poetry", "v1", 64)

	_, err := store.Search(
		context.Background(),
		[]float32{1},
		[]string{"songci", "tangsong"},
		5,
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(client.queryRequest.GetFilter().GetMust()) != 2 {
		t.Fatalf("unexpected filter: %v", client.queryRequest.GetFilter())
	}
}

func TestSearchRejectsInvalidPayload(t *testing.T) {
	client := &fakeClient{queryPoints: []*qdrant.ScoredPoint{{
		Payload: qdrant.NewValueMap(map[string]any{"generation": "v1"}),
	}}}
	store := New(client, "poetry", "v1", 64)

	_, err := store.Search(context.Background(), []float32{1}, nil, 1)
	if err == nil {
		t.Fatal("expected invalid payload error")
	}
}

func TestSearchReturnsClientError(t *testing.T) {
	client := &fakeClient{queryErr: errors.New("unavailable")}
	store := New(client, "poetry", "v1", 64)

	_, err := store.Search(context.Background(), []float32{1}, nil, 1)
	if err == nil {
		t.Fatal("expected query error")
	}
}

func TestCloseClosesOfficialClientWrapper(t *testing.T) {
	client := &fakeClient{}
	store := New(client, "poetry", "v1", 64)

	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if !client.closed {
		t.Fatal("client was not closed")
	}
}
