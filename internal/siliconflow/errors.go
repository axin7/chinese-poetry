package siliconflow

import (
	"context"
	"errors"
	"fmt"
	"net"
	"time"
)

var ErrClosed = errors.New("siliconflow client is closed")

type ValidationError struct {
	Field  string
	Reason string
}

func (err *ValidationError) Error() string {
	return fmt.Sprintf("invalid %s: %s", err.Field, err.Reason)
}

type TransportError struct {
	Path string
	Err  error
}

func (err *TransportError) Error() string {
	return fmt.Sprintf("SiliconFlow request to %s failed: %v", err.Path, err.Err)
}

func (err *TransportError) Unwrap() error {
	return err.Err
}

func (err *TransportError) Timeout() bool {
	if errors.Is(err.Err, context.DeadlineExceeded) {
		return true
	}
	var netErr net.Error
	return errors.As(err.Err, &netErr) && netErr.Timeout()
}

type HTTPError struct {
	StatusCode int
	Body       string
	RetryAfter *time.Duration
}

func (err *HTTPError) Error() string {
	return fmt.Sprintf("SiliconFlow returned HTTP %d: %s", err.StatusCode, err.Body)
}

type ResponseError struct {
	Reason string
	Err    error
}

func (err *ResponseError) Error() string {
	if err.Err == nil {
		return "invalid SiliconFlow response: " + err.Reason
	}
	return fmt.Sprintf("invalid SiliconFlow response: %s: %v", err.Reason, err.Err)
}

func (err *ResponseError) Unwrap() error {
	return err.Err
}
