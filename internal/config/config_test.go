package config

import (
	"os"
	"testing"
	"time"
)

var environmentNames = []string{
	"SQLITE_DB_PATH", "DATASETS_CONFIG_PATH", "QDRANT_HOST", "QDRANT_PORT",
	"QDRANT_URL", "QDRANT_CLUSTER_ENDPOINT", "QDRANT_API_KEY", "QDRANT_TLS", "COLLECTION_NAME",
	"CORPUS_GENERATION", "VECTOR_DIM",
	"EMBEDDING_PROFILE", "SILICONFLOW_KEY", "SILICONFLOW_API_KEY",
	"SILICONFLOW_BASE_URL", "SILICONFLOW_EMBED_MODEL", "SILICONFLOW_RERANK_MODEL",
	"EMBEDDING_TIMEOUT", "RERANK_TIMEOUT", "EMBEDDING_CONCURRENCY",
	"RERANK_CONCURRENCY", "RERANK_ENABLED", "QDRANT_HNSW_EF",
	"RESPONSE_CACHE_BYTES", "VECTOR_CACHE_BYTES", "RESPONSE_CACHE_TTL",
	"SEARCH_CONCURRENCY", "EMBEDDING_BATCH_SIZE", "QDRANT_BATCH_SIZE",
	"CHECKPOINT_PATH",
	"HTTP_CONCURRENCY", "SEARCH_REQUESTS_PER_SECOND", "SEARCH_BURST",
	"DETAIL_REQUESTS_PER_SECOND", "DETAIL_BURST", "DETAIL_CONCURRENCY",
	"DETAIL_CACHE_BYTES", "DETAIL_CACHE_TTL", "EMBEDDING_REQUESTS_PER_MINUTE",
}

func clearEnvironment(t *testing.T) {
	t.Helper()
	for _, name := range environmentNames {
		value, present := os.LookupEnv(name)
		if err := os.Unsetenv(name); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			if present {
				_ = os.Setenv(name, value)
			} else {
				_ = os.Unsetenv(name)
			}
		})
	}
}

func TestLoadDefaults(t *testing.T) {
	clearEnvironment(t)

	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if settings.QdrantURL != "http://127.0.0.1:6334" {
		t.Fatalf("unexpected Qdrant URL: %s", settings.QdrantURL)
	}
	if settings.VectorDim != 1024 || settings.EmbeddingProfile != IndexEmbedProfile {
		t.Fatalf("unexpected index contract: %+v", settings)
	}
	if settings.ResponseCacheTTL != time.Hour || settings.RerankEnabled {
		t.Fatalf("unexpected runtime defaults: %+v", settings)
	}
}

func TestLoadOverridesAndDurationFormats(t *testing.T) {
	clearEnvironment(t)
	t.Setenv("QDRANT_HOST", "qdrant")
	t.Setenv("QDRANT_PORT", "7333")
	t.Setenv("QDRANT_API_KEY", "qdrant-key")
	t.Setenv("QDRANT_TLS", "true")
	t.Setenv("EMBEDDING_TIMEOUT", "2.5")
	t.Setenv("RERANK_TIMEOUT", "750ms")
	t.Setenv("RERANK_ENABLED", "YES")
	t.Setenv("SILICONFLOW_BASE_URL", "https://example.test/v1///")

	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}

	if settings.QdrantURL != "http://qdrant:7333" {
		t.Fatalf("unexpected Qdrant URL: %s", settings.QdrantURL)
	}
	if settings.QdrantAPIKey != "qdrant-key" || !settings.QdrantTLS {
		t.Fatalf("unexpected Qdrant credentials: %+v", settings)
	}
	if settings.EmbeddingTimeout != 2500*time.Millisecond {
		t.Fatalf("unexpected embedding timeout: %s", settings.EmbeddingTimeout)
	}
	if settings.RerankTimeout != 750*time.Millisecond || !settings.RerankEnabled {
		t.Fatalf("unexpected rerank settings: %+v", settings)
	}
	if settings.SiliconFlowBaseURL != "https://example.test/v1" {
		t.Fatalf("unexpected base URL: %s", settings.SiliconFlowBaseURL)
	}
}

func TestLoadUsesConfiguredHostInDefaultURL(t *testing.T) {
	clearEnvironment(t)
	t.Setenv("QDRANT_HOST", "qdrant")

	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.QdrantURL != "http://qdrant:6334" {
		t.Fatalf("unexpected Qdrant URL: %s", settings.QdrantURL)
	}
}

func TestLoadPrefersExplicitQdrantURL(t *testing.T) {
	clearEnvironment(t)
	t.Setenv("QDRANT_HOST", "ignored")
	t.Setenv("QDRANT_PORT", "7333")
	t.Setenv("QDRANT_URL", "https://qdrant.example.test:6334")
	t.Setenv("QDRANT_CLUSTER_ENDPOINT", "invalid-cloud-endpoint")

	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.QdrantURL != "https://qdrant.example.test:6334" {
		t.Fatalf("unexpected Qdrant URL: %s", settings.QdrantURL)
	}
}

func TestLoadCloudEndpointUsesGRPCAndTLS(t *testing.T) {
	tests := []struct {
		endpoint string
		want     string
	}{
		{"https://cluster.qdrant.io", "https://cluster.qdrant.io:6334"},
		{"https://cluster.qdrant.io/", "https://cluster.qdrant.io:6334"},
		{"https://cluster.qdrant.io:443", "https://cluster.qdrant.io:6334"},
		{"https://cluster.qdrant.io:6333", "https://cluster.qdrant.io:6334"},
		{"https://cluster.qdrant.io:6334", "https://cluster.qdrant.io:6334"},
		{"https://cluster.qdrant.io:7334", "https://cluster.qdrant.io:7334"},
	}
	for _, test := range tests {
		t.Run(test.endpoint, func(t *testing.T) {
			clearEnvironment(t)
			t.Setenv("QDRANT_CLUSTER_ENDPOINT", test.endpoint)
			t.Setenv("QDRANT_API_KEY", "cloud-key")
			settings, err := Load()
			if err != nil {
				t.Fatal(err)
			}
			if settings.QdrantURL != test.want || !settings.QdrantTLS {
				t.Fatalf("unexpected Cloud endpoint: %s, TLS=%t", settings.QdrantURL, settings.QdrantTLS)
			}
			if settings.QdrantAPIKey != "cloud-key" {
				t.Fatal("Cloud API key was not loaded")
			}
		})
	}
}

func TestLoadPreservesExplicitQdrantPort(t *testing.T) {
	clearEnvironment(t)
	t.Setenv("QDRANT_URL", "https://private.example.test:7443")
	t.Setenv("QDRANT_CLUSTER_ENDPOINT", "https://cluster.qdrant.io:443")
	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.QdrantURL != "https://private.example.test:7443" || !settings.QdrantTLS {
		t.Fatalf("explicit Qdrant URL changed: %s, TLS=%t", settings.QdrantURL, settings.QdrantTLS)
	}
}

func TestLoadRejectsInvalidCloudEndpoints(t *testing.T) {
	endpoints := []string{
		"cluster.qdrant.io", "http://cluster.qdrant.io", "https://",
		"https://cluster.qdrant.io:99999", "https://cluster.qdrant.io:0",
		"https://cluster.qdrant.io:invalid", "https://cluster.qdrant.io/collections",
		"https://user:secret@cluster.qdrant.io", "https://cluster.qdrant.io?key=secret",
		"https://cluster.qdrant.io#fragment",
	}
	for _, endpoint := range endpoints {
		t.Run(endpoint, func(t *testing.T) {
			clearEnvironment(t)
			t.Setenv("QDRANT_CLUSTER_ENDPOINT", endpoint)
			if _, err := Load(); err == nil {
				t.Fatal("expected an invalid Cloud endpoint error")
			}
		})
	}
}

func TestLoadRejectsInvalidValues(t *testing.T) {
	tests := []struct {
		name  string
		value string
	}{
		{name: "VECTOR_DIM", value: "0"},
		{name: "RESPONSE_CACHE_BYTES", value: "many"},
		{name: "EMBEDDING_TIMEOUT", value: "never"},
		{name: "RERANK_ENABLED", value: "sometimes"},
		{name: "EMBEDDING_BATCH_SIZE", value: "257"},
		{name: "QDRANT_BATCH_SIZE", value: "2049"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			clearEnvironment(t)
			t.Setenv(test.name, test.value)
			if _, err := Load(); err == nil {
				t.Fatalf("expected %s to fail", test.name)
			}
		})
	}
}

func TestRequireAPIKeyUsesDocumentedPrecedence(t *testing.T) {
	clearEnvironment(t)
	t.Setenv("SILICONFLOW_KEY", "primary")
	t.Setenv("SILICONFLOW_API_KEY", "fallback")

	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	key, err := settings.RequireAPIKey()
	if err != nil || key != "primary" {
		t.Fatalf("unexpected key result: %q, %v", key, err)
	}

	settings.SiliconFlowAPIKey = ""
	if _, err := settings.RequireAPIKey(); err == nil {
		t.Fatal("expected a missing API key error")
	}
}

func TestValidateIndexContract(t *testing.T) {
	clearEnvironment(t)
	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if err := settings.ValidateIndexContract(); err != nil {
		t.Fatal(err)
	}

	settings.EmbeddingModel = "other-model"
	if err := settings.ValidateIndexContract(); err == nil {
		t.Fatal("expected embedding model mismatch")
	}
}
