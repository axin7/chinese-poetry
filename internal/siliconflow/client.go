package siliconflow

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/limits"
)

const (
	maxResponseBytes = 32 << 20
	maxErrorBytes    = 500
)

type Client struct {
	config      Config
	httpClient  *http.Client
	ownedClient bool
	embedSlots  chan struct{}
	rerankSlots chan struct{}
	closed      atomic.Bool
	embedBudget *limits.MinuteBudget
}

func New(config Config, httpClient *http.Client) (*Client, error) {
	normalized, err := config.normalized()
	if err != nil {
		return nil, err
	}
	owned := httpClient == nil
	if owned {
		httpClient = newHTTPClient(
			normalized.EmbeddingConcurrency + normalized.RerankConcurrency,
		)
	}
	client := &Client{
		config: normalized, httpClient: httpClient, ownedClient: owned,
		embedSlots:  make(chan struct{}, normalized.EmbeddingConcurrency),
		rerankSlots: make(chan struct{}, normalized.RerankConcurrency),
	}
	if normalized.EmbeddingRequestsPerMinute > 0 {
		client.embedBudget = limits.NewMinuteBudget(normalized.EmbeddingRequestsPerMinute, nil)
	}
	return client, nil
}

func (client *Client) Close() {
	if !client.closed.CompareAndSwap(false, true) {
		return
	}
	if client.ownedClient {
		client.httpClient.CloseIdleConnections()
	}
}

func newHTTPClient(maxConnections int) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = maxConnections
	transport.MaxIdleConnsPerHost = maxConnections
	transport.MaxConnsPerHost = maxConnections
	transport.IdleConnTimeout = 30 * time.Second
	transport.ForceAttemptHTTP2 = true
	return &http.Client{Transport: transport}
}

func (client *Client) call(
	ctx context.Context,
	path string,
	payload any,
	target any,
	timeout time.Duration,
	slots chan struct{},
) error {
	if client.closed.Load() {
		return ErrClosed
	}
	requestCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	if err := acquire(requestCtx, slots); err != nil {
		return &TransportError{Path: path, Err: err}
	}
	defer release(slots)
	if path == "/embeddings" && client.embedBudget != nil {
		if err := client.embedBudget.Admit(); err != nil {
			return err
		}
	}
	response, err := client.execute(requestCtx, path, payload)
	if err != nil {
		return &TransportError{Path: path, Err: err}
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return readHTTPError(response)
	}
	return decodeResponse(response.Body, target)
}

func (client *Client) execute(
	ctx context.Context,
	path string,
	payload any,
) (*http.Response, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("encode request: %w", err)
	}
	request, err := http.NewRequestWithContext(
		ctx, http.MethodPost, client.config.BaseURL+path, bytes.NewReader(body),
	)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+client.config.APIKey)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	return client.httpClient.Do(request)
}

func acquire(ctx context.Context, slots chan struct{}) error {
	select {
	case slots <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func release(slots chan struct{}) {
	<-slots
}

func readHTTPError(response *http.Response) error {
	body, err := io.ReadAll(io.LimitReader(response.Body, maxErrorBytes+1))
	if err != nil {
		return &TransportError{Path: response.Request.URL.Path, Err: err}
	}
	if len(body) > maxErrorBytes {
		body = body[:maxErrorBytes]
	}
	return &HTTPError{
		StatusCode: response.StatusCode, Body: string(body),
		RetryAfter: parseRetryAfter(response.Header.Get("Retry-After"), time.Now()),
	}
}

func decodeResponse(body io.Reader, target any) error {
	data, err := io.ReadAll(io.LimitReader(body, maxResponseBytes+1))
	if err != nil {
		return &TransportError{Path: "response body", Err: err}
	}
	if len(data) > maxResponseBytes {
		return &ResponseError{Reason: "body exceeds size limit"}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(target); err != nil {
		return &ResponseError{Reason: "invalid JSON", Err: err}
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return &ResponseError{Reason: "JSON contains trailing content", Err: err}
	}
	return nil
}
