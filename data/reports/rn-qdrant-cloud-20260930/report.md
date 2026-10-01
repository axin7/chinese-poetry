# RN Qdrant Cloud Migration

Production API switched to Qdrant Cloud on 2026-09-30 at 11:38 UTC (19:38 Asia/Shanghai).
The API, SQLite and query embedding provider remain on their existing deployment path.
The original local Qdrant, binary, Compose configuration and snapshots are retained for rollback.
The candidate API on port 18081 has been stopped; production continues on loopback 18080.

## Data

| Item | Verified Value |
| --- | --- |
| Collection | `poetry_tang_20260922_v1` |
| Generation | `poetry-20260921-v1` |
| Source / Target | Qdrant 1.15.5 / Cloud 1.19.1 |
| Total / Indexed Vectors | 127,031 / 127,031 |
| `yudingquantangshi` | 125,500 |
| `tangshisanbaishou` | 1,531 |
| Status / Optimizer | green / ok |
| Changed float32 Vectors | 0 |
| Migration Embedding Calls | 0 |

All UUIDs, complete payloads and float32 vectors match in a full ordered comparison.
Both fingerprints match the fresh source baseline and the historical source fingerprint:

```text
d070db80004d2ff2a19459216e3a93d1397147329c40ecc22a005fb9cbb77283
```

Logical copy was used because direct snapshot restoration from 1.15 to 1.19 is unsupported.
Batches of 128 use idempotent acknowledged upserts and a durable checkpoint.
The transfer preserved 1,024-D Cosine, on-disk vectors/payloads, HNSW M=16/EF-construction=100,
INT8 always-RAM quantization and keyword indexes on `dataset` and `generation`.
Cloud indexing/optimization threads were limited to one; all vectors finished indexing.

## Validation

- Go package tests, focused race tests, `go vet` and the Linux build passed.
- Migration tooling: 10 tests passed, including interruption/resume and integrity rejection.
- Direct stored-vector comparisons: 48 queries per endpoint, all top-1 IDs matched.
- Candidate API: 44 checks passed, including Chinese queries, details, filters and error states.
- Post-cutover API and HTTPS origin: 47 checks passed, including Bearer authentication.
- Public Cloudflare path: 3 checks passed using `poetry-cloud-migration-check/1.0`.
- Production remains healthy; rollback Compose configuration still validates.

One additional stored-vector sample exposed an existing source ANN recall miss in both filter modes.
For UUID `002b254b-c8c7-5ef1-b778-e32883e7ee70`, both exact indexes return itself at score 1.0.
Cloud ANN also returns itself (`yudingquantangshi:17179`); source ANN returns another point at 0.6519089.
Raw and Go-normalized queries, including a fresh API cache key, reproduce the difference.
Acceptance verifies Cloud's result against the exact self-hit ID and its original sentence;
the initial strict-comparison failure and diagnostic evidence are preserved.

Rebuilt INT8 quantization changes approximate scores even when the winning point is identical.
Maximum delta in the 48 direct samples was 0.0200768; the six Chinese API queries had identical
winning IDs/details and a maximum score delta of 0.01313019. The largest direct-score discrepancy
has exact score 1.0 on both endpoints. Search retains EF=64, indexed-only and disabled rescoring.

The initial public test with Python's default User-Agent was blocked by Cloudflare error 1010.
The declared application User-Agent and Go's default client identity reach the origin.
No Cloudflare security policy was changed. Automated Python clients should declare their application.

| Vector API Burst Concurrency | Requests / Passed | p95 Seconds |
| --- | --- | --- |
| 4 | 32 / 32 | 0.1388 |
| 8 | 32 / 32 | 0.0897 |
| 16 | 32 / 32 | 0.1538 |

These brief bursts used distinct vectors and fresh response cache keys, with no model calls.
They do not measure sustained capacity or online embedding throughput.
Telemetry allocator resident memory is not the full node memory usage.

## Operations

Use `compose.cloud.yaml` for maintenance, with both private environment files:

```sh
docker compose -f compose.cloud.yaml --env-file .env --env-file .env.cloud \
  up -d --no-deps vector-api
```

Run through ssh-skill in `/opt/poetry-tang-20260922`. Cloud credentials are in `.env.cloud` (0600);
the existing SiliconFlow credentials remain in `.env`. Credential values are absent from this report.
API startup validates the collection contract, health, payload indexes and exact generation isolation
within ten seconds. Runtime readiness also checks searchable indexing coverage.

Rollback uses the original binary and local Qdrant configuration:

```sh
docker compose -f compose.yaml --env-file .env up -d --no-deps vector-api
curl --fail http://127.0.0.1:18080/health
```

Do not use `--remove-orphans` while retaining local Qdrant. Public routes remain
`https://rn-proxy-test.anyveo.com/poetry/search` and `/poetry/poems/<work_id>`.
Search JSON remains `{id, original}`, with generation/score response headers.

| Preserved File | SHA256 |
| --- | --- |
| Original API | `ddfdd6dc5f661b8925b04f024566e20d9c941e7a9eb4545cebc13d54e9bae550` |
| Original Compose | `73499e631541e6d3b973d9e0f62ea4588f9f94de6cb387086f03103d8f33a585` |
| SQLite | `c95b12576cf24cf9f67bcfad8c7d4aff098ad137abb11919fcea2b935324c799` |
| New Cloud API | `75d7d27f4ba9a16bca1931954b7ff071a7ce0a9607859a2c417e767f67413d96` |

Evidence is archived in `evidence/` locally and `cloud-migration/` on RN.
Cloud free-tier inactivity and backup limits still apply; RN's retained copy is the current fallback.
