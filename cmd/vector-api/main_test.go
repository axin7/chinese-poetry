package main

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/httpapi"
)

func TestConcurrencyLimitKeepsHealthAvailable(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	handler := httpapi.WithConcurrencyLimit(http.HandlerFunc(
		func(writer http.ResponseWriter, request *http.Request) {
			if request.URL.Path != "/health" {
				close(started)
				<-release
			}
			writer.WriteHeader(http.StatusOK)
		},
	), 1)
	done := make(chan struct{})
	go func() {
		handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(
			http.MethodGet, "/poems/one", nil,
		))
		close(done)
	}()
	<-started
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/health", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("health request was limited: %d", recorder.Code)
	}
	close(release)
	<-done
}

type startupHealthFunc func(context.Context) error

func (check startupHealthFunc) Health(ctx context.Context) error {
	return check(ctx)
}

func TestStartupHealthUsesBoundedContext(t *testing.T) {
	check := startupHealthFunc(func(ctx context.Context) error {
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) <= 0 || time.Until(deadline) > startupTimeout {
			t.Fatal("startup health requires a bounded context")
		}
		return nil
	})
	if err := checkStartupHealth(context.Background(), check); err != nil {
		t.Fatal(err)
	}
}

func TestStartupHealthPropagatesFailure(t *testing.T) {
	failure := errors.New("collection is incomplete")
	check := startupHealthFunc(func(context.Context) error { return failure })
	if err := checkStartupHealth(context.Background(), check); !errors.Is(err, failure) {
		t.Fatalf("expected startup to fail, got %v", err)
	}
}

func TestStartupHealthPreservesCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	check := startupHealthFunc(func(ctx context.Context) error { return ctx.Err() })
	if err := checkStartupHealth(ctx, check); !errors.Is(err, context.Canceled) {
		t.Fatalf("expected canceled startup check, got %v", err)
	}
}

func TestListenAddressRequiresExplicitPrivateBinding(t *testing.T) {
	for _, test := range []struct {
		address, container string
		valid              bool
	}{
		{"", "", false}, {":8000", "", false}, {"0.0.0.0:8000", "", false},
		{"127.0.0.1:18080", "", true}, {"[::1]:18080", "", true},
		{":8000", "true", true}, {"0.0.0.0:8000", "true", true},
		{"203.0.113.1:8000", "true", false}, {"127.0.0.1:0", "", false},
	} {
		t.Run(test.address+test.container, func(t *testing.T) {
			t.Setenv("HTTP_ADDR", test.address)
			t.Setenv("HTTP_PRIVATE_CONTAINER", test.container)
			_, err := listenAddress()
			if (err == nil) != test.valid {
				t.Fatalf("unexpected binding result: %v", err)
			}
		})
	}
}
