package siliconflow

import (
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"testing/synctest"
	"time"
)

func TestParseRetryAfter(t *testing.T) {
	now := time.Date(2026, 9, 21, 0, 0, 0, 0, time.UTC)
	tests := []struct {
		value string
		want  time.Duration
		valid bool
	}{
		{value: "45", want: 45 * time.Second, valid: true},
		{value: " 30 ", want: 30 * time.Second, valid: true},
		{value: "0", valid: true},
		{value: now.Add(time.Minute).Format(http.TimeFormat), want: time.Minute, valid: true},
		{value: now.Add(-time.Minute).Format(http.TimeFormat), valid: true},
		{value: ""}, {value: "invalid"}, {value: "-1"}, {value: "1.5"},
		{value: "9223372036854775807"},
	}
	for _, test := range tests {
		t.Run(test.value, func(t *testing.T) {
			got := parseRetryAfter(test.value, now)
			if (got != nil) != test.valid || got != nil && *got != test.want {
				t.Fatalf("Retry-After=%q: got %v, want %s valid=%t",
					test.value, got, test.want, test.valid)
			}
		})
	}
}

func TestHTTPErrorPreservesRetryAfter(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		date := time.Now().UTC().Add(45 * time.Second).Format(http.TimeFormat)
		for _, value := range []string{"45", date} {
			response := &http.Response{
				StatusCode: http.StatusTooManyRequests,
				Header:     http.Header{"Retry-After": []string{value}},
				Body:       io.NopCloser(strings.NewReader("rate limited")),
			}
			err := readHTTPError(response)
			_ = response.Body.Close()
			var failure *HTTPError
			if !errors.As(err, &failure) || failure.RetryAfter == nil ||
				*failure.RetryAfter != 45*time.Second || failure.StatusCode != 429 {
				t.Fatalf("Retry-After metadata lost: %v", err)
			}
		}
	})
}
