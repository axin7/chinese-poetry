//go:build ignore

// Bounded, closed-loop load probe. Build this standalone file with go build.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"math/rand/v2"
	"net"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const generation = "poetry-20260921-v1"
const profile = "sf-bge-m3-1024-v1"

var queries = []string{
	"\u6625\u5929\u82b1\u5f00\u7684\u5510\u8bd7",
	"\u671b\u6708\u601d\u5ff5\u5bb6\u4eba",
	"\u670b\u53cb\u9001\u522b\u4f9d\u4f9d\u4e0d\u820d",
	"\u8fb9\u585e\u5c06\u58eb\u601d\u4e61",
	"\u5c71\u4e2d\u9690\u5c45\u7684\u751f\u6d3b",
	"\u519c\u6c11\u52b3\u4f5c\u751f\u6d3b\u4e0d\u6613",
}

type sample struct {
	Vector  []float32      `json:"vector"`
	Payload map[string]any `json:"payload"`
}

type outcome struct {
	Seconds float64 `json:"seconds"`
	Status  int     `json:"status"`
	Valid   bool    `json:"valid"`
	Detail  string  `json:"detail,omitempty"`
	Proto   string  `json:"protocol"`
	Cache   string  `json:"cache,omitempty"`
}

type stageResult struct {
	UTC         string         `json:"utc"`
	Concurrency int            `json:"concurrency"`
	Requests    int            `json:"requests"`
	Passed      int            `json:"passed"`
	Seconds     float64        `json:"wall_seconds"`
	RPS         float64        `json:"success_rps"`
	ErrorRate   float64        `json:"error_rate"`
	P50         float64        `json:"p50_seconds"`
	P95         float64        `json:"p95_seconds"`
	P99         float64        `json:"p99_seconds"`
	AllP95      float64        `json:"all_p95_seconds"`
	Max         float64        `json:"max_seconds"`
	MaxInFlight int64          `json:"max_client_inflight"`
	Statuses    map[int]int    `json:"statuses"`
	Errors      map[string]int `json:"errors"`
	Protocols   map[string]int `json:"protocols"`
	CacheStates map[string]int `json:"cache_states"`
	EarlyStop   bool           `json:"early_stop"`
	Rows        []outcome      `json:"rows"`
}

type probe struct {
	client      *http.Client
	base        string
	token       string
	origin      string
	workload    string
	samples     []sample
	runID       string
	vectorNonce [2]float32
	serial      atomic.Uint64
}

func newClient(originIP, host string) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.MaxIdleConns = 2048
	transport.MaxIdleConnsPerHost = 1024
	transport.MaxConnsPerHost = 1024
	transport.ForceAttemptHTTP2 = true
	if originIP != "" {
		dialer := &net.Dialer{Timeout: 5 * time.Second}
		transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
			if strings.HasPrefix(address, host+":") {
				address = net.JoinHostPort(originIP, "443")
			}
			return dialer.DialContext(ctx, network, address)
		}
	}
	return &http.Client{Transport: transport, Timeout: 12 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
}

func fetchSamples(client *http.Client, source string) ([]sample, error) {
	var samples []sample
	for _, dataset := range []string{"yudingquantangshi", "tangshisanbaishou"} {
		body, _ := json.Marshal(map[string]any{
			"limit": 8, "with_payload": true, "with_vector": true,
			"filter": map[string]any{"must": []any{
				map[string]any{"key": "dataset", "match": map[string]any{"value": dataset}},
				map[string]any{"key": "generation", "match": map[string]any{"value": generation}},
			}},
		})
		url := source + "/collections/poetry_tang_20260922_v1/points/scroll"
		response, err := client.Post(url, "application/json", bytes.NewReader(body))
		if err != nil {
			return nil, fmt.Errorf("source unavailable")
		}
		var decoded struct {
			Result struct {
				Points []sample `json:"points"`
			} `json:"result"`
		}
		err = json.NewDecoder(io.LimitReader(response.Body, 2<<20)).Decode(&decoded)
		response.Body.Close()
		if err != nil || response.StatusCode != 200 || len(decoded.Result.Points) != 8 {
			return nil, fmt.Errorf("invalid source response")
		}
		for _, point := range decoded.Result.Points {
			if len(point.Vector) != 1024 || point.Payload["generation"] != generation {
				return nil, fmt.Errorf("invalid source point")
			}
		}
		samples = append(samples, decoded.Result.Points...)
	}
	return samples, nil
}

func (p *probe) input() ([]byte, string, string) {
	n := p.serial.Add(1)
	if p.workload == "detail" {
		point := p.samples[int(n)%len(p.samples)]
		return nil, "/poems/" + point.Payload["work_id"].(string), "GET"
	}
	var payload map[string]any
	if p.workload == "vector" {
		point := p.samples[int(n)%len(p.samples)]
		vector := append([]float32(nil), point.Vector...)
		vector[0] += float32(n) * 0.00001
		vector[1] -= float32(n) * 0.000013
		vector[2] += p.vectorNonce[0]
		vector[3] += p.vectorNonce[1]
		payload = map[string]any{"vector": vector, "embedding_profile": profile}
	} else {
		query := queries[int(n)%len(queries)]
		if p.workload == "text" {
			query += fmt.Sprintf("\uff0c\u68c0\u7d22\u7f16\u53f7%s-%d", p.runID, n)
		}
		payload = map[string]any{"query": query}
	}
	body, _ := json.Marshal(payload)
	return body, "/search", "POST"
}

func (p *probe) request() outcome {
	body, path, method := p.input()
	request, err := http.NewRequest(method, p.base+path, bytes.NewReader(body))
	if err != nil {
		return outcome{Detail: "invalid_request"}
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("User-Agent", "poetry-api-deployment-validator/1.0")
	if p.token != "" {
		request.Header.Set("Authorization", "Bearer "+p.token)
	}
	started := time.Now()
	response, err := p.client.Do(request)
	if err != nil {
		return outcome{Seconds: time.Since(started).Seconds(), Detail: "transport_error"}
	}
	content, readErr := io.ReadAll(io.LimitReader(response.Body, 2<<20))
	response.Body.Close()
	row := outcome{Seconds: time.Since(started).Seconds(), Status: response.StatusCode,
		Proto: response.Proto, Cache: response.Header.Get("X-Poetry-Cache")}
	row.Valid, row.Detail = p.validate(response, content, readErr, path)
	return row
}

func (p *probe) validate(response *http.Response, content []byte, readErr error,
	path string) (bool, string) {
	var body map[string]any
	if readErr != nil || json.Unmarshal(content, &body) != nil {
		return false, "invalid_body"
	}
	if response.StatusCode != 200 {
		detail, _ := body["detail"].(string)
		if len(detail) > 150 {
			detail = "upstream_error"
		}
		return false, detail
	}
	if p.origin != "" && response.Header.Get("X-Origin-ID") != p.origin {
		return false, "origin_mismatch"
	}
	id, ok := body["id"].(string)
	if !ok || !(strings.HasPrefix(id, "yudingquantangshi:") ||
		strings.HasPrefix(id, "tangshisanbaishou:")) {
		return false, "invalid_id"
	}
	if p.workload == "detail" {
		original, ok := body["original"].([]any)
		return ok && len(original) > 0 && strings.HasSuffix(path, id), ""
	}
	score, err := strconv.ParseFloat(response.Header.Get("X-Poetry-Score"), 64)
	original, ok := body["original"].(string)
	valid := ok && original != "" && len(body) == 2 && err == nil &&
		!math.IsNaN(score) && !math.IsInf(score, 0) &&
		response.Header.Get("X-Poetry-Generation") == generation
	if !valid {
		return false, "invalid_contract"
	}
	return true, ""
}

func percentile(values []float64, fraction float64) float64 {
	if len(values) == 0 {
		return 0
	}
	sort.Float64s(values)
	position := float64(len(values)-1) * fraction
	lower := int(position)
	upper := min(lower+1, len(values)-1)
	return values[lower] + (values[upper]-values[lower])*(position-float64(lower))
}

func summarize(rows []outcome, concurrency int, started time.Time,
	peak int64, early bool) stageResult {
	s := stageResult{UTC: started.UTC().Format(time.RFC3339Nano), Concurrency: concurrency,
		Requests: len(rows), Seconds: time.Since(started).Seconds(), MaxInFlight: peak,
		Statuses: map[int]int{}, Errors: map[string]int{}, Protocols: map[string]int{},
		CacheStates: map[string]int{},
		EarlyStop:   early, Rows: rows}
	var good, all []float64
	for _, row := range rows {
		all = append(all, row.Seconds)
		s.Statuses[row.Status]++
		s.Protocols[row.Proto]++
		s.CacheStates[row.Cache]++
		if row.Valid {
			s.Passed++
			good = append(good, row.Seconds)
		} else {
			s.Errors[row.Detail]++
		}
	}
	s.RPS = float64(s.Passed) / s.Seconds
	s.ErrorRate = float64(s.Requests-s.Passed) / float64(max(s.Requests, 1))
	s.P50, s.P95, s.P99 = percentile(good, .5), percentile(good, .95), percentile(good, .99)
	s.AllP95, s.Max = percentile(all, .95), percentile(all, 1)
	return s
}

func (p *probe) stage(concurrency int, duration time.Duration, maxRequests uint64) stageResult {
	started := time.Now()
	end := started.Add(duration)
	var count, failures, inFlight, peak atomic.Int64
	var stop atomic.Bool
	var mu sync.Mutex
	var rows []outcome
	var group sync.WaitGroup
	for range concurrency {
		group.Add(1)
		go func() {
			defer group.Done()
			for time.Now().Before(end) && !stop.Load() {
				if uint64(count.Add(1)) > maxRequests {
					break
				}
				active := inFlight.Add(1)
				for active > peak.Load() {
					old := peak.Load()
					if active <= old || peak.CompareAndSwap(old, active) {
						break
					}
				}
				row := p.request()
				inFlight.Add(-1)
				mu.Lock()
				rows = append(rows, row)
				if !row.Valid {
					failures.Add(1)
				}
				completed := len(rows)
				shouldStop := completed >= 30 &&
					float64(failures.Load())/float64(completed) > .05
				mu.Unlock()
				if shouldStop {
					stop.Store(true)
				}
			}
		}()
	}
	group.Wait()
	return summarize(rows, concurrency, started, peak.Load(), stop.Load())
}

func save(report string, data any) error {
	encoded, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(report, append(encoded, '\n'), 0600)
}

func main() {
	base := flag.String("base-url", "https://rn-proxy-test.anyveo.com/poetry", "API base")
	tokenFile := flag.String("token-file", "", "Existing credential file")
	workload := flag.String("workload", "hot", "hot, vector, text, detail")
	source := flag.String("source-url", "http://127.0.0.1:17333", "Read-only Qdrant sample source")
	originIP := flag.String("origin-ip", "", "Connect to HTTPS origin retaining SNI")
	origin := flag.String("expect-origin", "bwg-poetry-network-20260930", "Expected origin header")
	concurrencies := flag.String("concurrency", "1,2,4,8,16", "Sequential ramp")
	duration := flag.Duration("duration", 20*time.Second, "Each stage issuance window")
	rest := flag.Duration("rest", 3*time.Second, "Between-stage rest")
	maxRequests := flag.Uint64("max-requests", 30000, "Per-stage hard request budget")
	report := flag.String("report", "", "New output file")
	flag.Parse()
	if err := run(*base, *tokenFile, *workload, *source, *originIP, *origin,
		*concurrencies, *duration, *rest, *maxRequests, *report); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(base, tokenFile, workload, source, originIP, origin, levels string,
	duration, rest time.Duration, budget uint64, report string) error {
	if report == "" || duration <= 0 || duration > time.Minute || budget == 0 {
		return fmt.Errorf("require report, duration (0,1m], and positive request budget")
	}
	if _, err := os.Stat(report); err == nil {
		return fmt.Errorf("report exists")
	}
	if workload != "hot" && workload != "vector" && workload != "text" && workload != "detail" {
		return fmt.Errorf("unsupported workload")
	}
	p := &probe{base: strings.TrimRight(base, "/"), workload: workload, origin: origin,
		runID:       strconv.FormatInt(time.Now().UnixNano(), 36),
		vectorNonce: [2]float32{float32(rand.Float64() * .01), float32(rand.Float64() * .01)}}
	p.client = newClient(originIP, "rn-proxy-test.anyveo.com")
	defer p.client.CloseIdleConnections()
	if tokenFile != "" {
		if !strings.HasPrefix(base, "https://") {
			return fmt.Errorf("credentials require HTTPS")
		}
		data, err := os.ReadFile(tokenFile)
		if err != nil {
			return fmt.Errorf("cannot read credential file")
		}
		p.token = strings.TrimSpace(string(data))
		if len(p.token) != 64 {
			return fmt.Errorf("unexpected credential format")
		}
	}
	if workload == "vector" || workload == "detail" {
		var err error
		p.samples, err = fetchSamples(p.client, source)
		if err != nil {
			return err
		}
	}
	if workload == "hot" {
		for range len(queries) * 2 {
			if row := p.request(); !row.Valid {
				return fmt.Errorf("warmup failed: %d", row.Status)
			}
		}
	}
	return p.ramp(levels, duration, rest, budget, report, originIP)
}

func (p *probe) ramp(levels string, duration, rest time.Duration, budget uint64,
	report, originIP string) error {
	results := map[string]any{"base_url": p.base, "workload": p.workload,
		"run_id": p.runID, "origin_ip": originIP, "window_seconds": duration.Seconds(),
		"method": "closed-loop; persistent connections; no retries; all completions included"}
	var stages []stageResult
	for _, level := range strings.Split(levels, ",") {
		concurrency, err := strconv.Atoi(level)
		if err != nil || concurrency < 1 || concurrency > 1024 {
			return fmt.Errorf("concurrency must be 1..1024")
		}
		stage := p.stage(concurrency, duration, budget)
		stages = append(stages, stage)
		results["stages"] = stages
		if err := save(report, results); err != nil {
			return err
		}
		stage.Rows = nil
		encoded, _ := json.Marshal(stage)
		fmt.Println(string(encoded))
		if stage.EarlyStop || stage.ErrorRate > .05 {
			break
		}
		time.Sleep(rest)
	}
	return nil
}
