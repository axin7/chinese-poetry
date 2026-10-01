package search

import (
	"errors"
	"testing"

	"github.com/chinese-poetry/chinese-poetry/internal/model"
	"github.com/chinese-poetry/chinese-poetry/internal/qdrantstore"
)

func scoredHits() []qdrantstore.Hit {
	hits := []qdrantstore.Hit{testHit(1), testHit(2), testHit(3)}
	for index := range hits {
		hits[index].Score = []float32{0.81, 0.72, 0.63}[index]
	}
	return hits
}

func TestSearchScoreFollowsSelectedCandidate(t *testing.T) {
	tests := []struct {
		name      string
		rerank    bool
		models    fakeModels
		brokenRow int64
		wantID    string
		wantScore float32
	}{
		{name: "ann", wantID: "tangsong:1", wantScore: 0.81},
		{name: "reranked", rerank: true, models: fakeModels{rerankIndex: 1},
			wantID: "tangsong:2", wantScore: 0.72},
		{name: "skipped candidate", rerank: true, brokenRow: 2,
			models: fakeModels{rerankIndex: 1}, wantID: "tangsong:3", wantScore: 0.63},
		{name: "rerank unavailable", rerank: true,
			models: fakeModels{rerankErr: errors.New("unavailable")},
			wantID: "tangsong:1", wantScore: 0.81},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			store := &fakeVectorStore{hits: scoredHits()}
			service := newTestServiceWithRepository(t, test.rerank, &test.models, store,
				partialRepository{failRowID: test.brokenRow})
			query := "月夜思乡"
			request := model.SearchRequest{Query: &query}
			for range 2 {
				response, err := service.Search(t.Context(), request)
				if err != nil {
					t.Fatal(err)
				}
				if response.Poem.ID != test.wantID || response.Match.Score != test.wantScore {
					t.Fatalf("score does not match selected candidate: %#v", response)
				}
			}
			if store.calls != 1 {
				t.Fatalf("cached score triggered %d searches", store.calls)
			}
		})
	}
}

func TestVectorRequestReturnsNegativeAndZeroScores(t *testing.T) {
	for _, score := range []float32{-0.25, 0} {
		hit := testHit(1)
		hit.Score = score
		service := newTestService(t, false, nil, &fakeVectorStore{hits: []qdrantstore.Hit{hit}})
		response, err := service.Search(t.Context(), model.SearchRequest{
			Vector: unitVector(), EmbeddingProfile: model.EmbeddingProfile,
		})
		if err != nil {
			t.Fatal(err)
		}
		if response.Match.Score != score {
			t.Fatalf("got score %g, want %g", response.Match.Score, score)
		}
	}
}
