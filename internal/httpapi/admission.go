package httpapi

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/chinese-poetry/chinese-poetry/internal/limits"
)

type Policy struct {
	SearchRate        int
	SearchBurst       int
	DetailRate        int
	DetailBurst       int
	HTTPConcurrency   int
	DetailConcurrency int
	DetailCacheBytes  int64
	DetailCacheTTL    time.Duration
	Now               func() time.Time
}

func DefaultPolicy() Policy {
	return Policy{SearchRate: 2, SearchBurst: 4, DetailRate: 10, DetailBurst: 20,
		HTTPConcurrency: 64, DetailConcurrency: 8, DetailCacheBytes: 8 << 20,
		DetailCacheTTL: time.Hour, Now: time.Now}
}

func (policy Policy) validate() error {
	if policy.SearchRate <= 0 || policy.SearchBurst <= 0 || policy.DetailRate <= 0 ||
		policy.DetailBurst <= 0 || policy.HTTPConcurrency <= 0 || policy.DetailConcurrency <= 0 ||
		policy.DetailCacheBytes <= 0 || policy.DetailCacheTTL <= 0 {
		return fmt.Errorf("HTTP admission policy values must be positive")
	}
	return nil
}

func withRate(next http.Handler, bucket *limits.Bucket, code string) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if err := bucket.Admit(code); err != nil {
			writeAdmissionError(writer, err.(*limits.Error))
			return
		}
		next.ServeHTTP(writer, request)
	})
}

func WithConcurrencyLimit(next http.Handler, limit int) http.Handler {
	slots := make(chan struct{}, limit)
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/health" {
			next.ServeHTTP(writer, request)
			return
		}
		select {
		case slots <- struct{}{}:
			defer func() { <-slots }()
			next.ServeHTTP(writer, request)
		default:
			writeAdmissionError(writer, limits.Capacity("http_capacity_exceeded"))
		}
	})
}

func writeAdmissionError(writer http.ResponseWriter, err *limits.Error) {
	writer.Header().Set("Content-Type", "application/json; charset=utf-8")
	writer.Header().Set("Retry-After", strconv.Itoa(err.RetryAfter))
	writer.WriteHeader(err.Status)
	_ = json.NewEncoder(writer).Encode(map[string]any{
		"code": err.Code, "message": err.Message, "retry_after": err.RetryAfter,
		"detail": err.Message,
	})
}
