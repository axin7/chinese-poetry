package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/chinese-poetry/chinese-poetry/internal/config"
	vectorimporter "github.com/chinese-poetry/chinese-poetry/internal/importer"
	"github.com/chinese-poetry/chinese-poetry/internal/qdrantstore"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
	"github.com/chinese-poetry/chinese-poetry/internal/siliconflow"
)

type options struct {
	datasets        string
	maxSentences    int64
	resetCheckpoint bool
}

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	options := parseOptions()
	if options.maxSentences < 0 {
		return fmt.Errorf("--max-sentences must not be negative")
	}
	settings, err := config.Load()
	if err != nil {
		return err
	}
	if err := settings.ValidateIndexContract(); err != nil {
		return err
	}
	apiKey, err := settings.RequireAPIKey()
	if err != nil {
		return err
	}
	return execute(settings, apiKey, options)
}

func execute(settings config.Settings, apiKey string, options options) error {
	fingerprint, err := vectorimporter.SourceFingerprint(
		settings.DBPath, settings.DatasetsConfigPath,
	)
	if err != nil {
		return err
	}
	repo, err := repository.New(settings.DBPath, settings.DatasetsConfigPath)
	if err != nil {
		return err
	}
	defer repo.Close()
	client, err := qdrantstore.NewOfficialClient(
		settings.QdrantURL, settings.QdrantAPIKey, settings.QdrantTLS,
	)
	if err != nil {
		return err
	}
	store := qdrantstore.New(
		client, settings.CollectionName, settings.Generation, uint64(settings.HNSWEF),
	)
	defer store.Close()
	models, err := siliconflow.New(modelConfig(settings, apiKey), nil)
	if err != nil {
		return err
	}
	defer models.Close()
	worker := vectorimporter.New(vectorimporter.Config{
		Generation: settings.Generation, SourceFingerprint: fingerprint,
		VectorDimension:      settings.VectorDim,
		EmbeddingBatchSize:   settings.EmbeddingBatchSize,
		EmbeddingConcurrency: settings.EmbeddingConcurrency,
		QdrantBatchSize:      settings.QdrantBatchSize,
		CheckpointPath:       settings.CheckpointPath,
	}, repo, models, store)
	ctx, stop := signal.NotifyContext(
		context.Background(), syscall.SIGINT, syscall.SIGTERM,
	)
	defer stop()
	indexed, err := worker.Run(
		ctx, parseDatasets(options.datasets), options.maxSentences,
		options.resetCheckpoint,
	)
	if err == nil {
		log.Printf("已确认写入 %d 个句子向量", indexed)
	}
	return err
}

func modelConfig(settings config.Settings, apiKey string) siliconflow.Config {
	return siliconflow.Config{
		APIKey: apiKey, BaseURL: settings.SiliconFlowBaseURL,
		EmbeddingModel: settings.EmbeddingModel, RerankModel: settings.RerankModel,
		EmbeddingEncoding:    os.Getenv("IMPORT_EMBEDDING_ENCODING"),
		EmbeddingTimeout:     settings.EmbeddingTimeout,
		RerankTimeout:        settings.RerankTimeout,
		EmbeddingConcurrency: settings.EmbeddingConcurrency,
		RerankConcurrency:    settings.RerankConcurrency,
	}
}

func parseOptions() options {
	var result options
	flag.StringVar(&result.datasets, "datasets", "", "comma-separated datasets")
	flag.Int64Var(&result.maxSentences, "max-sentences", 0, "maximum total sentences")
	flag.BoolVar(&result.resetCheckpoint, "reset-checkpoint", false, "reset progress")
	flag.Parse()
	if len(flag.Args()) > 0 {
		extra := strings.Join(flag.Args(), ",")
		result.datasets = strings.Trim(result.datasets+","+extra, ",")
	}
	return result
}

func parseDatasets(raw string) []string {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	parts := strings.Split(raw, ",")
	result := make([]string, 0, len(parts))
	for _, part := range parts {
		if value := strings.TrimSpace(part); value != "" {
			result = append(result, value)
		}
	}
	return result
}
