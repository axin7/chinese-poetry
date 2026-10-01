package config

import (
	"fmt"
	"math"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	DefaultBaseURL        = "https://api.siliconflow.cn/v1"
	DefaultEmbedModel     = "BAAI/bge-m3"
	DefaultRerankModel    = "BAAI/bge-reranker-v2-m3"
	IndexVectorDimension  = 1024
	IndexEmbedProfile     = "sf-bge-m3-1024-v1"
	maxEmbeddingBatchSize = 256
	maxQdrantBatchSize    = 2048
)

type Settings struct {
	DBPath                     string
	DatasetsConfigPath         string
	QdrantURL                  string
	QdrantAPIKey               string
	QdrantTLS                  bool
	CollectionName             string
	Generation                 string
	VectorDim                  int
	EmbeddingProfile           string
	SiliconFlowAPIKey          string
	SiliconFlowBaseURL         string
	EmbeddingModel             string
	RerankModel                string
	EmbeddingTimeout           time.Duration
	RerankTimeout              time.Duration
	EmbeddingConcurrency       int
	RerankConcurrency          int
	RerankEnabled              bool
	HNSWEF                     int
	ResponseCacheBytes         int64
	VectorCacheBytes           int64
	ResponseCacheTTL           time.Duration
	SearchConcurrency          int
	EmbeddingBatchSize         int
	QdrantBatchSize            int
	CheckpointPath             string
	HTTPConcurrency            int
	SearchRate                 int
	SearchBurst                int
	DetailRate                 int
	DetailBurst                int
	DetailConcurrency          int
	DetailCacheBytes           int64
	DetailCacheTTL             time.Duration
	EmbeddingRequestsPerMinute int
}

func Load() (Settings, error) {
	port, err := positiveInt("QDRANT_PORT", 6334)
	if err != nil {
		return Settings{}, err
	}
	settings := defaults(envOr("QDRANT_HOST", "127.0.0.1"), port)
	loadStrings(&settings)
	if err := loadQdrantEndpoint(&settings); err != nil {
		return Settings{}, err
	}
	if err := loadIntegers(&settings); err != nil {
		return Settings{}, err
	}
	if err := validateBatchSizes(settings); err != nil {
		return Settings{}, err
	}
	if err := loadDurations(&settings); err != nil {
		return Settings{}, err
	}
	settings.RerankEnabled, err = boolean("RERANK_ENABLED", false)
	if err != nil {
		return Settings{}, err
	}
	settings.QdrantTLS, err = boolean(
		"QDRANT_TLS", strings.HasPrefix(settings.QdrantURL, "https://"),
	)
	if err != nil {
		return Settings{}, err
	}
	settings.SiliconFlowAPIKey = firstNonempty(
		os.Getenv("SILICONFLOW_KEY"),
		os.Getenv("SILICONFLOW_API_KEY"),
	)
	return settings, nil
}

func defaults(host string, port int) Settings {
	return Settings{
		DBPath:                     "chinese_poetry.db",
		DatasetsConfigPath:         "loader/datas.json",
		QdrantURL:                  fmt.Sprintf("http://%s:%d", host, port),
		CollectionName:             "poetry_sentences",
		Generation:                 "local-v1",
		VectorDim:                  IndexVectorDimension,
		EmbeddingProfile:           IndexEmbedProfile,
		SiliconFlowBaseURL:         DefaultBaseURL,
		EmbeddingModel:             DefaultEmbedModel,
		RerankModel:                DefaultRerankModel,
		EmbeddingTimeout:           5 * time.Second,
		RerankTimeout:              time.Second,
		EmbeddingConcurrency:       16,
		RerankConcurrency:          4,
		HNSWEF:                     64,
		ResponseCacheBytes:         32 * 1024 * 1024,
		VectorCacheBytes:           32 * 1024 * 1024,
		ResponseCacheTTL:           time.Hour,
		SearchConcurrency:          256,
		EmbeddingBatchSize:         16,
		QdrantBatchSize:            256,
		CheckpointPath:             "data/import_progress.json",
		HTTPConcurrency:            64,
		SearchRate:                 2,
		SearchBurst:                4,
		DetailRate:                 10,
		DetailBurst:                20,
		DetailConcurrency:          8,
		DetailCacheBytes:           8 << 20,
		DetailCacheTTL:             time.Hour,
		EmbeddingRequestsPerMinute: 120,
	}
}

func loadStrings(settings *Settings) {
	settings.DBPath = envOr("SQLITE_DB_PATH", settings.DBPath)
	settings.DatasetsConfigPath = envOr("DATASETS_CONFIG_PATH", settings.DatasetsConfigPath)
	settings.QdrantAPIKey = envOr("QDRANT_API_KEY", settings.QdrantAPIKey)
	settings.CollectionName = envOr("COLLECTION_NAME", settings.CollectionName)
	settings.Generation = envOr("CORPUS_GENERATION", settings.Generation)
	settings.EmbeddingProfile = envOr("EMBEDDING_PROFILE", settings.EmbeddingProfile)
	baseURL := envOr("SILICONFLOW_BASE_URL", settings.SiliconFlowBaseURL)
	settings.SiliconFlowBaseURL = strings.TrimRight(baseURL, "/")
	settings.EmbeddingModel = envOr("SILICONFLOW_EMBED_MODEL", settings.EmbeddingModel)
	settings.RerankModel = envOr("SILICONFLOW_RERANK_MODEL", settings.RerankModel)
	settings.CheckpointPath = envOr("CHECKPOINT_PATH", settings.CheckpointPath)
}

func loadQdrantEndpoint(settings *Settings) error {
	if rawURL := os.Getenv("QDRANT_URL"); rawURL != "" {
		settings.QdrantURL = rawURL
		return nil
	}
	endpoint := os.Getenv("QDRANT_CLUSTER_ENDPOINT")
	if endpoint == "" {
		return nil
	}
	parsed, err := url.Parse(endpoint)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" {
		return fmt.Errorf("QDRANT_CLUSTER_ENDPOINT must be an absolute HTTPS URL")
	}
	if parsed.User != nil || (parsed.Path != "" && parsed.Path != "/") ||
		parsed.RawQuery != "" || parsed.ForceQuery || parsed.Fragment != "" {
		return fmt.Errorf("QDRANT_CLUSTER_ENDPOINT must contain only a host and optional port")
	}
	port := parsed.Port()
	if port == "" || port == "443" || port == "6333" {
		port = "6334"
	}
	number, err := strconv.Atoi(port)
	if err != nil || number <= 0 || number > 65535 {
		return fmt.Errorf("QDRANT_CLUSTER_ENDPOINT has an invalid port")
	}
	settings.QdrantURL = "https://" + net.JoinHostPort(parsed.Hostname(), port)
	return nil
}

func loadIntegers(settings *Settings) error {
	values := []struct {
		name    string
		value   *int
		value64 *int64
	}{
		{name: "VECTOR_DIM", value: &settings.VectorDim},
		{name: "EMBEDDING_CONCURRENCY", value: &settings.EmbeddingConcurrency},
		{name: "RERANK_CONCURRENCY", value: &settings.RerankConcurrency},
		{name: "QDRANT_HNSW_EF", value: &settings.HNSWEF},
		{name: "SEARCH_CONCURRENCY", value: &settings.SearchConcurrency},
		{name: "RESPONSE_CACHE_BYTES", value64: &settings.ResponseCacheBytes},
		{name: "VECTOR_CACHE_BYTES", value64: &settings.VectorCacheBytes},
		{name: "EMBEDDING_BATCH_SIZE", value: &settings.EmbeddingBatchSize},
		{name: "QDRANT_BATCH_SIZE", value: &settings.QdrantBatchSize},
		{name: "HTTP_CONCURRENCY", value: &settings.HTTPConcurrency},
		{name: "SEARCH_REQUESTS_PER_SECOND", value: &settings.SearchRate},
		{name: "SEARCH_BURST", value: &settings.SearchBurst},
		{name: "DETAIL_REQUESTS_PER_SECOND", value: &settings.DetailRate},
		{name: "DETAIL_BURST", value: &settings.DetailBurst},
		{name: "DETAIL_CONCURRENCY", value: &settings.DetailConcurrency},
		{name: "DETAIL_CACHE_BYTES", value64: &settings.DetailCacheBytes},
		{name: "EMBEDDING_REQUESTS_PER_MINUTE", value: &settings.EmbeddingRequestsPerMinute},
	}
	for _, item := range values {
		if err := assignPositiveInteger(item.name, item.value, item.value64); err != nil {
			return err
		}
	}
	return nil
}

func assignPositiveInteger(name string, target *int, target64 *int64) error {
	if target != nil {
		value, err := positiveInt(name, *target)
		if err != nil {
			return err
		}
		*target = value
		return nil
	}
	value, err := positiveInt64(name, *target64)
	if err == nil {
		*target64 = value
	}
	return err
}

func validateBatchSizes(settings Settings) error {
	if settings.EmbeddingBatchSize > maxEmbeddingBatchSize {
		return fmt.Errorf("EMBEDDING_BATCH_SIZE must not exceed %d", maxEmbeddingBatchSize)
	}
	if settings.QdrantBatchSize > maxQdrantBatchSize {
		return fmt.Errorf("QDRANT_BATCH_SIZE must not exceed %d", maxQdrantBatchSize)
	}
	return nil
}

func loadDurations(settings *Settings) error {
	values := []struct {
		name  string
		value *time.Duration
	}{
		{name: "EMBEDDING_TIMEOUT", value: &settings.EmbeddingTimeout},
		{name: "RERANK_TIMEOUT", value: &settings.RerankTimeout},
		{name: "RESPONSE_CACHE_TTL", value: &settings.ResponseCacheTTL},
		{name: "DETAIL_CACHE_TTL", value: &settings.DetailCacheTTL},
	}
	for _, item := range values {
		value, err := positiveDuration(item.name, *item.value)
		if err != nil {
			return err
		}
		*item.value = value
	}
	return nil
}

func (settings Settings) ValidateIndexContract() error {
	if settings.VectorDim != IndexVectorDimension {
		return fmt.Errorf("current index requires VECTOR_DIM=%d", IndexVectorDimension)
	}
	if settings.EmbeddingProfile != IndexEmbedProfile {
		return fmt.Errorf("current index requires EMBEDDING_PROFILE=%s", IndexEmbedProfile)
	}
	if settings.EmbeddingModel != DefaultEmbedModel {
		return fmt.Errorf("current index requires %s", DefaultEmbedModel)
	}
	if settings.RerankModel != DefaultRerankModel {
		return fmt.Errorf("current reranker requires %s", DefaultRerankModel)
	}
	return nil
}

func (settings Settings) RequireAPIKey() (string, error) {
	if settings.SiliconFlowAPIKey == "" {
		return "", fmt.Errorf(
			"text search requires SILICONFLOW_KEY or SILICONFLOW_API_KEY",
		)
	}
	return settings.SiliconFlowAPIKey, nil
}

func positiveInt(name string, fallback int) (int, error) {
	raw, ok := os.LookupEnv(name)
	if !ok {
		return fallback, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive integer", name)
	}
	return value, nil
}

func positiveInt64(name string, fallback int64) (int64, error) {
	raw, ok := os.LookupEnv(name)
	if !ok {
		return fallback, nil
	}
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive integer", name)
	}
	return value, nil
}

func positiveDuration(name string, fallback time.Duration) (time.Duration, error) {
	raw, ok := os.LookupEnv(name)
	if !ok {
		return fallback, nil
	}
	if seconds, err := strconv.ParseFloat(raw, 64); err == nil {
		limit := float64(math.MaxInt64) / float64(time.Second)
		if seconds <= 0 || math.IsNaN(seconds) || math.IsInf(seconds, 0) || seconds > limit {
			return 0, fmt.Errorf("%s must be a positive duration", name)
		}
		return time.Duration(seconds * float64(time.Second)), nil
	}
	value, err := time.ParseDuration(raw)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive duration", name)
	}
	return value, nil
}

func boolean(name string, fallback bool) (bool, error) {
	raw, ok := os.LookupEnv(name)
	if !ok {
		return fallback, nil
	}
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "1", "true", "yes", "on":
		return true, nil
	case "0", "false", "no", "off":
		return false, nil
	default:
		return false, fmt.Errorf("%s must be a boolean", name)
	}
}

func envOr(name, fallback string) string {
	if value, ok := os.LookupEnv(name); ok {
		return value
	}
	return fallback
}

func firstNonempty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}
