package config

import (
	"testing"
	"time"
)

func TestAdmissionDefaultsAndOverrides(t *testing.T) {
	clearEnvironment(t)
	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.HTTPConcurrency != 64 || settings.SearchRate != 2 || settings.SearchBurst != 4 ||
		settings.DetailRate != 10 || settings.DetailBurst != 20 || settings.DetailConcurrency != 8 ||
		settings.DetailCacheTTL != time.Hour || settings.EmbeddingRequestsPerMinute != 120 {
		t.Fatalf("unexpected admission defaults: %+v", settings)
	}
	t.Setenv("HTTP_CONCURRENCY", "32")
	t.Setenv("SEARCH_REQUESTS_PER_SECOND", "1")
	t.Setenv("DETAIL_CACHE_TTL", "30m")
	t.Setenv("EMBEDDING_REQUESTS_PER_MINUTE", "60")
	settings, err = Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.HTTPConcurrency != 32 || settings.SearchRate != 1 ||
		settings.DetailCacheTTL != 30*time.Minute || settings.EmbeddingRequestsPerMinute != 60 {
		t.Fatalf("admission overrides were ignored: %+v", settings)
	}
}

func TestAdmissionRejectsInvalidConfiguration(t *testing.T) {
	for _, name := range []string{"HTTP_CONCURRENCY", "SEARCH_REQUESTS_PER_SECOND", "SEARCH_BURST",
		"DETAIL_REQUESTS_PER_SECOND", "DETAIL_BURST", "DETAIL_CONCURRENCY",
		"DETAIL_CACHE_BYTES", "DETAIL_CACHE_TTL", "EMBEDDING_REQUESTS_PER_MINUTE"} {
		t.Run(name, func(t *testing.T) {
			clearEnvironment(t)
			t.Setenv(name, "0")
			if _, err := Load(); err == nil {
				t.Fatal("invalid admission configuration was accepted")
			}
		})
	}
}
