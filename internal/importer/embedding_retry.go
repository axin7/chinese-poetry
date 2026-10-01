package importer

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"syscall"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/siliconflow"
)

const maxEmbeddingRetries = 3

func (i *Importer) embedWithRetry(ctx context.Context, texts []string) ([][]float32, error) {
	for retry := 0; ; retry++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		vectors, err := i.embedder.EmbedMany(ctx, texts)
		if canceled := ctx.Err(); canceled != nil {
			return nil, canceled
		}
		if err == nil {
			return vectors, nil
		}
		reason, transient := embeddingRetryReason(err)
		if !transient || retry >= maxEmbeddingRetries {
			return nil, err
		}
		delay := embeddingRetryDelay(err, retry)
		log.Printf("embedding batch retry %d/%d in %s (%s)",
			retry+1, maxEmbeddingRetries, delay, reason)
		if err := waitEmbeddingRetry(ctx, delay); err != nil {
			return nil, err
		}
	}
}

func embeddingRetryDelay(err error, retry int) time.Duration {
	var responseError *siliconflow.HTTPError
	if errors.As(err, &responseError) {
		if responseError.RetryAfter != nil && *responseError.RetryAfter >= 0 {
			return *responseError.RetryAfter
		}
		if responseError.StatusCode == 429 {
			return (30 * time.Second) << retry
		}
	}
	return time.Second << retry
}

func embeddingRetryReason(err error) (string, bool) {
	var responseError *siliconflow.HTTPError
	if errors.As(err, &responseError) {
		status := responseError.StatusCode
		transient := status == 408 || status == 429 || status >= 500 && status < 600
		return fmt.Sprintf("HTTP %d", status), transient
	}
	var transportError *siliconflow.TransportError
	if !errors.As(err, &transportError) || errors.Is(err, context.Canceled) {
		return "", false
	}
	if transportError.Timeout() {
		return "transport timeout", true
	}
	var networkError net.Error
	if errors.As(transportError.Err, &networkError) && networkError.Temporary() {
		return "temporary network error", true
	}
	for _, transient := range []error{
		io.EOF, io.ErrUnexpectedEOF, net.ErrClosed, syscall.ECONNRESET,
		syscall.ECONNREFUSED, syscall.ECONNABORTED, syscall.EPIPE,
		syscall.ETIMEDOUT, syscall.ENETUNREACH, syscall.EHOSTUNREACH,
	} {
		if errors.Is(transportError.Err, transient) {
			return "connection interrupted", true
		}
	}
	return "", false
}

func waitEmbeddingRetry(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
