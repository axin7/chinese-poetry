# Rust poetry API

Cloudflare Workers runs the authenticated search, work detail and health endpoints under
`/poetry`. Qdrant Cloud stores the existing vectors, original sentences and canonical work
details. Text searches obtain a query embedding from SiliconFlow; vector requests skip this step.
The runtime has no SQL database or D1 binding.

Required secrets: `CLIENT_TOKEN`, `QDRANT_REST_URL`, `QDRANT_API_KEY`, `SILICONFLOW_API_KEY`.
Nonsecret generation, collection and model settings are in `wrangler.toml`.
The lab Worker uses its own workers.dev address; deployment does not alter production DNS.
The public API uses default edge placement. The private Rust Qdrant gateway uses AWS us-west-1
placement and is called only through the `QDRANT_GATEWAY` Service Binding. Both use the same
WASM build; the gateway disables workers.dev and preview URLs and allows only read operations.

```sh
cargo test --locked --manifest-path workers/poetry-rust/Cargo.toml
cargo clippy --locked --manifest-path workers/poetry-rust/Cargo.toml --all-targets -- -D warnings
```

Build from this directory with `worker-build --release --no-panic-recovery`, then use
`pnpm exec wrangler deploy`. Rust 1.90, the wasm32-unknown-unknown target and worker-build 0.8.6
are supported by the checked-in lockfile. `--no-panic-recovery` is required by the WASM glue.
Deploy `wrangler.qdrant.toml` and its Qdrant secrets before deploying the public `wrangler.toml`.

Publish the payload-only API collection using `scripts/qdrant_api/documents.py` before
deploying a matching generation. Search reads a locator from the vector collection, then resolves
its original sentence in the API collection. Detail reads the canonical JSON string there.
Each work has one API point with no vector. Future imports must publish both matching collections
before activating a new generation.

Successful responses use a one-hour edge cache, checked after authentication and validation.
The cache is local to each Cloudflare location. Model requests have eight-second timeouts and
Qdrant gateway forwarding has five seconds. Internal Qdrant calls have seven seconds including
forwarding. The complete request has a
ten-second deadline. Request bodies are limited to 64 KiB.
Cold requests execute their own upstream I/O. The experimental shared executor path did not pass
runtime validation and was removed; edge cache hits still avoid upstream calls.

Workers Free has an account-wide 100,000 requests/day limit and a nominal 10 ms CPU/request
budget. Network waits do not count as CPU. Qdrant and SiliconFlow quotas remain independent
limits; replacing the HTTP server does not increase those upstream quotas.
