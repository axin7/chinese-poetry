package httpapi

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"

	"github.com/chinese-poetry/chinese-poetry/internal/limits"
	"github.com/chinese-poetry/chinese-poetry/internal/model"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
	"github.com/chinese-poetry/chinese-poetry/internal/search"
)

const MaxSearchBodyBytes int64 = 64 * 1024

type searchResponse struct {
	ID       string `json:"id"`
	Original string `json:"original"`
}

type SearchService interface {
	Search(context.Context, model.SearchRequest) (model.SearchResponse, error)
}

type Repository interface {
	GetWorkByID(context.Context, string) (repository.PoetryWork, error)
	Health(context.Context) error
}

type HealthStore interface {
	Health(context.Context) error
}

type API struct {
	generation string
	search     SearchService
	repository Repository
	store      HealthStore
	details    *detailCache
}

func New(
	generation string,
	service SearchService,
	repo Repository,
	store HealthStore,
) http.Handler {
	handler, err := NewConfigured(generation, service, repo, store, DefaultPolicy())
	if err != nil {
		panic(err)
	}
	return handler
}

func NewConfigured(
	generation string, service SearchService, repo Repository, store HealthStore,
	policy Policy,
) (http.Handler, error) {
	if err := policy.validate(); err != nil {
		return nil, err
	}
	details, err := newDetailCache(generation, repo, policy)
	if err != nil {
		return nil, err
	}
	api := &API{
		generation: generation,
		search:     service,
		repository: repo,
		store:      store,
		details:    details,
	}
	mux := http.NewServeMux()
	// Proxy authentication supplies the single trusted service identity for all buckets.
	mux.Handle("POST /search", withRate(http.HandlerFunc(api.handleSearch),
		limits.NewBucket(policy.SearchRate, policy.SearchBurst, policy.Now), "search_rate_exceeded"))
	mux.Handle("GET /poems/{work_id}", withRate(http.HandlerFunc(api.handlePoem),
		limits.NewBucket(policy.DetailRate, policy.DetailBurst, policy.Now), "detail_rate_exceeded"))
	mux.HandleFunc("GET /health", api.handleHealth)
	return WithConcurrencyLimit(mux, policy.HTTPConcurrency), nil
}

func (api *API) handleSearch(writer http.ResponseWriter, request *http.Request) {
	payload, status, err := decodeSearchRequest(writer, request)
	if err != nil {
		writeError(writer, status, err.Error())
		return
	}
	response, err := api.search.Search(request.Context(), payload)
	if err != nil {
		api.writeSearchError(writer, err)
		return
	}
	writer.Header().Set("X-Poetry-Generation", api.generation)
	writer.Header().Set("X-Poetry-Score", strconv.FormatFloat(
		float64(response.Match.Score), 'g', -1, 32,
	))
	writeJSON(writer, http.StatusOK, searchResponse{
		ID: response.Poem.ID, Original: response.Match.Original,
	})
}

func (api *API) writeSearchError(writer http.ResponseWriter, err error) {
	var rejection *limits.Error
	switch {
	case errors.As(err, &rejection):
		writeAdmissionError(writer, rejection)
	case errors.Is(err, search.ErrNoResult):
		writeError(writer, http.StatusNotFound, "没有找到符合条件的诗词")
	case errors.Is(err, search.ErrUnavailable):
		writeError(writer, http.StatusServiceUnavailable, "检索服务暂时不可用")
	default:
		writeError(writer, http.StatusUnprocessableEntity, err.Error())
	}
}

func (api *API) handlePoem(writer http.ResponseWriter, request *http.Request) {
	versions, supplied := request.URL.Query()["generation"]
	if supplied && (len(versions) != 1 || versions[0] != api.generation) {
		writeError(writer, http.StatusNotFound, "语料版本不存在")
		return
	}
	value, err := api.details.get(request.Context(), request.PathValue("work_id"))
	if err != nil {
		var rejection *limits.Error
		switch {
		case errors.As(err, &rejection):
			writeAdmissionError(writer, rejection)
		case errors.Is(err, repository.ErrNotFound),
			errors.Is(err, repository.ErrInvalidLocator):
			writeError(writer, http.StatusNotFound, "作品不存在")
		case errors.Is(err, repository.ErrDataIntegrity):
			writeError(writer, http.StatusInternalServerError, "语料数据异常")
		default:
			writeError(writer, http.StatusServiceUnavailable, "语料库暂时不可用")
		}
		return
	}
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	_, _ = writer.Write(value)
}

func nonNilStrings(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

func (api *API) handleHealth(writer http.ResponseWriter, request *http.Request) {
	if err := api.store.Health(request.Context()); err != nil {
		writeError(writer, http.StatusServiceUnavailable, "向量索引不可用")
		return
	}
	if err := api.repository.Health(request.Context()); err != nil {
		writeError(writer, http.StatusServiceUnavailable, "语料库不可用")
		return
	}
	writeJSON(writer, http.StatusOK, map[string]string{
		"status": "ok", "generation": api.generation,
	})
}

func decodeSearchRequest(
	writer http.ResponseWriter,
	request *http.Request,
) (model.SearchRequest, int, error) {
	request.Body = http.MaxBytesReader(writer, request.Body, MaxSearchBodyBytes)
	data, err := io.ReadAll(request.Body)
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return model.SearchRequest{}, http.StatusRequestEntityTooLarge,
				fmt.Errorf("请求体不能超过 64 KiB")
		}
		return model.SearchRequest{}, http.StatusBadRequest, fmt.Errorf("读取请求失败")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return model.SearchRequest{}, http.StatusUnprocessableEntity,
			fmt.Errorf("请求体不是有效 JSON")
	}
	if err := validateFields(fields); err != nil {
		return model.SearchRequest{}, http.StatusUnprocessableEntity, err
	}
	var payload model.SearchRequest
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&payload); err != nil {
		return model.SearchRequest{}, http.StatusUnprocessableEntity,
			fmt.Errorf("请求字段类型无效")
	}
	if err := payload.Validate(); err != nil {
		return model.SearchRequest{}, http.StatusUnprocessableEntity, err
	}
	return payload, http.StatusOK, nil
}

func validateFields(fields map[string]json.RawMessage) error {
	allowed := map[string]struct{}{
		"query": {}, "vector": {}, "embedding_profile": {}, "filters": {},
	}
	for field := range fields {
		if _, ok := allowed[field]; !ok {
			return fmt.Errorf("unknown field: %s", field)
		}
	}
	for _, field := range []string{"query", "vector"} {
		if raw, ok := fields[field]; ok && bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			return fmt.Errorf("%s must not be null", field)
		}
	}
	return nil
}

func writeError(writer http.ResponseWriter, status int, detail string) {
	writeJSON(writer, status, map[string]string{"detail": detail})
}

func writeJSON(writer http.ResponseWriter, status int, payload any) {
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(payload)
}
