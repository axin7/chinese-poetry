# SiliconFlow BGE-M3 comparison

Test generator: RN, reusable HTTPS connections, unique short poetry inputs, no retries,
5-second request deadline, 1024 finite nonzero vector validation. Production was unchanged.

| Model | Concurrent calls | Duration | Success / requests | Successful RPS | P95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| BAAI/bge-m3 | 16 | 60.390 s | 1951 / 1951 | 32.307 | 0.8191 s |
| Pro/BAAI/bge-m3 | 16 | 60.392 s | 2265 / 2265 | 37.505 | 0.5255 s |
| BAAI/bge-m3 | 32 | 18.957 s | 1024 / 1024 | 54.017 | 1.0452 s |
| Pro/BAAI/bge-m3 | 32 | 11.979 s | 789 / 824 | 65.866 | 0.9658 s |
| Pro/BAAI/bge-m3, isolated repeat | 32 | 11.644 s | 785 / 805 | 67.419 | 0.6028 s |

At 16 calls, Pro improved throughput by 16.1% and reduced P95 by 35.8% in the cooled
one-minute comparison. Both models passed that workload without errors. Pro's 32-call
repeats stopped on HTTP 429; the isolated response was classified as an RPM/request-rate
limit. Successful-response percentiles exclude failed requests. The short free32 result
does not establish sustainable capacity. Neither provider's absolute maximum was measured.
The original failed runs saved HTTP status and sanitized RPM classification, not literal
error messages or exact per-error timestamps; later cooled runs include stage UTC times.

Eight identical queries produced element-identical free/Pro vectors (cosine 1.0, maximum
element delta 0), and identical Qdrant Cloud top hits and scores. Official pricing metadata
identifies the same underlying BAAI/bge-m3 model, 1024 dimensions, and 8192-token context.
Existing corpus vectors do not need re-embedding on the evidence from these eight samples.

Current official Pro input price: CNY 0.07 / million tokens. Public level0 free limits:
2000 RPM and 500000 TPM; Pro level0: 2000 RPM and 1000000 TPM. Pro level1/2 RPM rises to
3000/5000. RPM measures request rate, not simultaneous requests. The account-info endpoint
returned HTTP 410, so the actual account tier and billing balance were not obtained.

Across all four test reports: 9504 embedding requests; 89737 successful Pro tokens;
estimated Pro cost CNY 0.00628159, excluding any provider billing not exposed in usage.

Evidence: `pro-model-metadata.json`, `pro-direct-first.json`, `pro-direct-steady.json`,
`pro-direct-isolated32.json`, `pro-direct-sustained16.json`.
