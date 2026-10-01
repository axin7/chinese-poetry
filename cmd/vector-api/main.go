package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/config"
	"github.com/chinese-poetry/chinese-poetry/internal/httpapi"
	"github.com/chinese-poetry/chinese-poetry/internal/qdrantstore"
	"github.com/chinese-poetry/chinese-poetry/internal/repository"
	"github.com/chinese-poetry/chinese-poetry/internal/search"
	"github.com/chinese-poetry/chinese-poetry/internal/siliconflow"
)

type runtime struct {
	repository *repository.Repository
	store      *qdrantstore.Store
	models     *siliconflow.Client
	handler    http.Handler
}

const (
	startupTimeout = 10 * time.Second
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	address, err := listenAddress()
	if err != nil {
		return err
	}
	settings, err := config.Load()
	if err != nil {
		return err
	}
	if err := settings.ValidateIndexContract(); err != nil {
		return err
	}
	runtime, err := buildRuntime(settings)
	if err != nil {
		return err
	}
	defer runtime.Close()
	return serve(address, runtime.handler)
}

func listenAddress() (string, error) {
	address := os.Getenv("HTTP_ADDR")
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return "", fmt.Errorf("HTTP_ADDR must explicitly specify a private listen address")
	}
	number, err := strconv.Atoi(port)
	if err != nil || number <= 0 || number > 65535 {
		return "", fmt.Errorf("HTTP_ADDR has an invalid port")
	}
	ip := net.ParseIP(host)
	if ip != nil && ip.IsLoopback() {
		return address, nil
	}
	if (host == "" || (ip != nil && ip.IsUnspecified())) &&
		os.Getenv("HTTP_PRIVATE_CONTAINER") == "true" {
		return address, nil
	}
	return "", fmt.Errorf(
		"HTTP_ADDR must be loopback; container binds require HTTP_PRIVATE_CONTAINER=true",
	)
}

func buildRuntime(settings config.Settings) (*runtime, error) {
	repo, store, err := buildStores(settings)
	if err != nil {
		return nil, err
	}
	models, err := buildModels(settings)
	if err != nil {
		_ = store.Close()
		_ = repo.Close()
		return nil, err
	}
	service, err := buildSearch(settings, models, store, repo)
	if err != nil {
		closeModels(models)
		_ = store.Close()
		_ = repo.Close()
		return nil, err
	}
	handler, err := buildHandler(settings, service, repo, store)
	if err != nil {
		closeModels(models)
		_ = store.Close()
		_ = repo.Close()
		return nil, err
	}
	return &runtime{
		repository: repo,
		store:      store,
		models:     models,
		handler:    withTimeout(handler, 10*time.Second),
	}, nil
}

func buildStores(settings config.Settings) (*repository.Repository, *qdrantstore.Store, error) {
	repo, err := repository.New(settings.DBPath, settings.DatasetsConfigPath)
	if err != nil {
		return nil, nil, err
	}
	client, err := qdrantstore.NewOfficialClient(
		settings.QdrantURL, settings.QdrantAPIKey, settings.QdrantTLS,
	)
	if err != nil {
		_ = repo.Close()
		return nil, nil, err
	}
	store := qdrantstore.New(
		client, settings.CollectionName, settings.Generation, uint64(settings.HNSWEF),
	)
	if err := checkStartupHealth(context.Background(), store); err != nil {
		_ = store.Close()
		_ = repo.Close()
		return nil, nil, err
	}
	return repo, store, nil
}

func buildHandler(
	settings config.Settings, service httpapi.SearchService,
	repo httpapi.Repository, store httpapi.HealthStore,
) (http.Handler, error) {
	return httpapi.NewConfigured(settings.Generation, service, repo, store,
		httpapi.Policy{SearchRate: settings.SearchRate, SearchBurst: settings.SearchBurst,
			DetailRate: settings.DetailRate, DetailBurst: settings.DetailBurst,
			HTTPConcurrency: settings.HTTPConcurrency, DetailConcurrency: settings.DetailConcurrency,
			DetailCacheBytes: settings.DetailCacheBytes, DetailCacheTTL: settings.DetailCacheTTL})
}

func checkStartupHealth(ctx context.Context, store httpapi.HealthStore) error {
	ctx, cancel := context.WithTimeout(ctx, startupTimeout)
	defer cancel()
	if err := store.Health(ctx); err != nil {
		return fmt.Errorf("Qdrant startup readiness: %w", err)
	}
	return nil
}

func buildModels(settings config.Settings) (*siliconflow.Client, error) {
	if settings.SiliconFlowAPIKey == "" {
		return nil, nil
	}
	modelConfig := siliconflow.Config{
		APIKey: settings.SiliconFlowAPIKey, BaseURL: settings.SiliconFlowBaseURL,
		EmbeddingModel: settings.EmbeddingModel, RerankModel: settings.RerankModel,
		EmbeddingTimeout:           settings.EmbeddingTimeout,
		RerankTimeout:              settings.RerankTimeout,
		EmbeddingConcurrency:       settings.EmbeddingConcurrency,
		RerankConcurrency:          settings.RerankConcurrency,
		EmbeddingRequestsPerMinute: settings.EmbeddingRequestsPerMinute,
	}
	return siliconflow.New(modelConfig, nil)
}

func buildSearch(
	settings config.Settings,
	models *siliconflow.Client,
	store *qdrantstore.Store,
	repo *repository.Repository,
) (*search.Service, error) {
	var modelClient search.Models
	if models != nil {
		modelClient = models
	}
	return search.New(search.Settings{
		Generation: settings.Generation, EmbeddingProfile: settings.EmbeddingProfile,
		RerankModel: settings.RerankModel, RerankEnabled: settings.RerankEnabled,
		ResponseCacheBytes: settings.ResponseCacheBytes,
		VectorCacheBytes:   settings.VectorCacheBytes,
		ResponseCacheTTL:   settings.ResponseCacheTTL,
		SearchConcurrency:  settings.SearchConcurrency,
	}, modelClient, store, repo)
}

func (runtime *runtime) Close() {
	closeModels(runtime.models)
	if err := runtime.store.Close(); err != nil {
		log.Printf("close Qdrant client: %v", err)
	}
	if err := runtime.repository.Close(); err != nil {
		log.Printf("close SQLite repository: %v", err)
	}
}

func closeModels(models *siliconflow.Client) {
	if models != nil {
		models.Close()
	}
}

func serve(address string, handler http.Handler) error {
	server := &http.Server{
		Addr: address, Handler: handler,
		ReadHeaderTimeout: 2 * time.Second, ReadTimeout: 10 * time.Second,
		WriteTimeout: 15 * time.Second, IdleTimeout: 5 * time.Second,
		MaxHeaderBytes: 1 << 20,
	}
	ctx, stop := signal.NotifyContext(
		context.Background(), syscall.SIGINT, syscall.SIGTERM,
	)
	defer stop()
	errorsChannel := make(chan error, 1)
	go func() { errorsChannel <- server.ListenAndServe() }()
	log.Printf("vector API listening on %s", address)
	select {
	case err := <-errorsChannel:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return server.Shutdown(shutdownCtx)
	}
}

func withTimeout(next http.Handler, timeout time.Duration) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		ctx, cancel := context.WithTimeout(request.Context(), timeout)
		defer cancel()
		next.ServeHTTP(writer, request.WithContext(ctx))
	})
}
