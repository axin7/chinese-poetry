package limits

import (
	"math"
	"net/http"
	"sync"
	"time"

	"golang.org/x/time/rate"
)

type Error struct {
	Status     int
	Code       string
	Message    string
	RetryAfter int
}

func (err *Error) Error() string { return err.Code }

func Capacity(code string) *Error {
	return &Error{http.StatusServiceUnavailable, code, "Poetry service is busy", 1}
}

type Bucket struct {
	limiter *rate.Limiter
	now     func() time.Time
}

func NewBucket(perSecond, burst int, now func() time.Time) *Bucket {
	if now == nil {
		now = time.Now
	}
	return &Bucket{rate.NewLimiter(rate.Limit(perSecond), burst), now}
}

func (bucket *Bucket) Admit(code string) error {
	now := bucket.now()
	if bucket.limiter.AllowN(now, 1) {
		return nil
	}
	delay := (1 - bucket.limiter.TokensAt(now)) / float64(bucket.limiter.Limit())
	return &Error{http.StatusTooManyRequests, code, "Poetry request rate exceeded",
		max(1, int(math.Ceil(delay)))}
}

type MinuteBudget struct {
	mu       sync.Mutex
	limit    int
	attempts []time.Time
	now      func() time.Time
}

func NewMinuteBudget(limit int, now func() time.Time) *MinuteBudget {
	if now == nil {
		now = time.Now
	}
	return &MinuteBudget{limit: limit, now: now}
}

func (budget *MinuteBudget) Admit() error {
	budget.mu.Lock()
	defer budget.mu.Unlock()
	now := budget.now()
	first := 0
	for first < len(budget.attempts) && !budget.attempts[first].Add(time.Minute).After(now) {
		first++
	}
	budget.attempts = budget.attempts[first:]
	if len(budget.attempts) >= budget.limit {
		delay := budget.attempts[0].Add(time.Minute).Sub(now).Seconds()
		return &Error{http.StatusTooManyRequests, "embedding_budget_exceeded",
			"Embedding request budget exceeded", max(1, int(math.Ceil(delay)))}
	}
	budget.attempts = append(budget.attempts, now)
	return nil
}
