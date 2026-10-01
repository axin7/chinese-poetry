package siliconflow

import (
	"net/http"
	"strconv"
	"strings"
	"time"
)

func parseRetryAfter(value string, now time.Time) *time.Duration {
	value = strings.TrimSpace(value)
	if value == "" {
		return nil
	}
	seconds, err := strconv.ParseInt(value, 10, 64)
	if err == nil {
		if seconds < 0 || seconds > int64((1<<63-1)/time.Second) {
			return nil
		}
		delay := time.Duration(seconds) * time.Second
		return &delay
	}
	date, err := http.ParseTime(value)
	if err != nil {
		return nil
	}
	delay := max(date.Sub(now), 0)
	return &delay
}
