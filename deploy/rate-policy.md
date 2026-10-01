# Poetry admission policy

The Go API keeps one process-shared bucket per route for the authenticated service
`deepfocus-scenarios`. The mandatory proxy token check stays before the API and its caches.
User login, entitlement and user limits belong to DeepFocus Worker. Tokens rotated for the
same service share the existing buckets; client identity headers and IP addresses are ignored.

| Setting | Default | Scope |
| --- | --- | --- |
| `SEARCH_REQUESTS_PER_SECOND` / `SEARCH_BURST` | 2 / 4 | All search arrivals, including cached requests |
| `DETAIL_REQUESTS_PER_SECOND` / `DETAIL_BURST` | 10 / 20 | All detail arrivals, including cached requests |
| `HTTP_CONCURRENCY` | 64 | In-flight HTTP requests; loopback health is exempt |
| `DETAIL_CONCURRENCY` | 8 | Unique uncached detail lookups |
| `DETAIL_CACHE_BYTES` / `DETAIL_CACHE_TTL` | 8388608 / 3600 seconds | Successful details only |
| `EMBEDDING_REQUESTS_PER_MINUTE` | 120 | Actual embedding attempts in a rolling 60-second window |

Deployment templates preserve uncached search concurrency 16 and embedding concurrency 4.
Detail keys contain generation, dataset and work ID; stale generation is rejected before cache
lookup. Matching uncached lookups share one bounded operation. Errors are never cached.
HTTP contexts and shared lookups have ten-second deadlines; full capacity fails immediately.

The embedding budget counts attempted provider calls, including failures, without refunds.
Response/vector cache hits and invalid input do not consume it. Set a lower RPM value when the
provider contract or other callers require it. The initial 120/minute cap is our admission policy,
not a verified provider quota. No TPM limit is assumed without a known budget and token accounting.
All rates are conservative starting settings requiring measurement of latency and provider errors.

Rate rejection returns HTTP 429 with `code`, `message`, `retry_after`, the legacy `detail` field,
and `Retry-After`. Codes are `search_rate_exceeded`, `detail_rate_exceeded`, and
`embedding_budget_exceeded`. Capacity rejection returns HTTP 503 with the same envelope and
codes `http_capacity_exceeded`, `search_capacity_exceeded`, or `detail_capacity_exceeded`.
Genuine upstream outages retain their existing error classification. Proxies forward rejection
status, body and retry headers. Clients must avoid retries before `Retry-After` expires.

`HTTP_ADDR` is required and must be loopback. A private container can bind `:8000` only with
`HTTP_PRIVATE_CONTAINER=true`; its host publish must remain `127.0.0.1:18080:8000`. Missing or
invalid listen settings fail startup. Do not expose the native API or Qdrant ports publicly.

These limits and singleflight groups cover one Go process. Multiple replicas, additional trusted
clients or the experimental Rust Worker require coordinated service-wide admission before the
same thresholds can be claimed globally. Keep service token authentication on every origin path.
Deploy API binary and matching environment before verifying live 429/503 behavior; local tests do
not update production. Use cache misses to measure cold capacity and cache hits to measure HTTP
capacity; inspect rejection counts, cache hit rate and embedding attempt count separately.
