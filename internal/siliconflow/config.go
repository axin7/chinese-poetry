package siliconflow

import (
	"net/url"
	"strings"
	"time"
)

const (
	DefaultBaseURL           = "https://api.siliconflow.cn/v1"
	DefaultEmbeddingModel    = "BAAI/bge-m3"
	DefaultEmbeddingEncoding = "float"
	DefaultRerankModel       = "BAAI/bge-reranker-v2-m3"
	EmbeddingDimension       = 1024

	DefaultEmbeddingTimeout     = 2 * time.Second
	DefaultRerankTimeout        = time.Second
	DefaultEmbeddingConcurrency = 16
	DefaultRerankConcurrency    = 8
)

type Config struct {
	APIKey                     string
	BaseURL                    string
	EmbeddingModel             string
	EmbeddingEncoding          string
	RerankModel                string
	EmbeddingTimeout           time.Duration
	RerankTimeout              time.Duration
	EmbeddingConcurrency       int
	RerankConcurrency          int
	EmbeddingRequestsPerMinute int
}

func DefaultConfig(apiKey string) Config {
	return Config{
		APIKey:               apiKey,
		BaseURL:              DefaultBaseURL,
		EmbeddingModel:       DefaultEmbeddingModel,
		EmbeddingEncoding:    DefaultEmbeddingEncoding,
		RerankModel:          DefaultRerankModel,
		EmbeddingTimeout:     DefaultEmbeddingTimeout,
		RerankTimeout:        DefaultRerankTimeout,
		EmbeddingConcurrency: DefaultEmbeddingConcurrency,
		RerankConcurrency:    DefaultRerankConcurrency,
	}
}

func (config Config) normalized() (Config, error) {
	if config.BaseURL == "" {
		config.BaseURL = DefaultBaseURL
	}
	if config.EmbeddingModel == "" {
		config.EmbeddingModel = DefaultEmbeddingModel
	}
	if config.EmbeddingEncoding == "" {
		config.EmbeddingEncoding = DefaultEmbeddingEncoding
	}
	if config.RerankModel == "" {
		config.RerankModel = DefaultRerankModel
	}
	if config.EmbeddingTimeout == 0 {
		config.EmbeddingTimeout = DefaultEmbeddingTimeout
	}
	if config.RerankTimeout == 0 {
		config.RerankTimeout = DefaultRerankTimeout
	}
	if config.EmbeddingConcurrency == 0 {
		config.EmbeddingConcurrency = DefaultEmbeddingConcurrency
	}
	if config.RerankConcurrency == 0 {
		config.RerankConcurrency = DefaultRerankConcurrency
	}
	config.BaseURL = strings.TrimRight(config.BaseURL, "/")
	return config, config.validate()
}

func (config Config) validate() error {
	values := map[string]string{
		"api_key": config.APIKey, "base_url": config.BaseURL,
		"embedding_model": config.EmbeddingModel, "rerank_model": config.RerankModel,
	}
	for field, value := range values {
		if strings.TrimSpace(value) == "" {
			return &ValidationError{Field: field, Reason: "must not be empty"}
		}
	}
	parsed, err := url.Parse(config.BaseURL)
	if err != nil || parsed.Scheme == "" || parsed.Host == "" {
		return &ValidationError{Field: "base_url", Reason: "must be an absolute URL"}
	}
	if config.EmbeddingTimeout < 0 || config.RerankTimeout < 0 {
		return &ValidationError{Field: "timeout", Reason: "must be positive"}
	}
	if config.EmbeddingConcurrency < 0 || config.RerankConcurrency < 0 {
		return &ValidationError{Field: "concurrency", Reason: "must be positive"}
	}
	if config.EmbeddingRequestsPerMinute < 0 {
		return &ValidationError{Field: "embedding_rpm", Reason: "must not be negative"}
	}
	if config.EmbeddingEncoding != "float" && config.EmbeddingEncoding != "base64" {
		return &ValidationError{Field: "embedding_encoding", Reason: "must be float or base64"}
	}
	return nil
}
