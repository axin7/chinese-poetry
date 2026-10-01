# Qdrant metadata latency from RN

Measured at 2026-09-30 16:39:12 UTC via ssh-skill. Evidence:
`document-latency-rn.json`. Ten sequential read-only REST requests; no writes or model calls.

Exact Rust scroll bodies selected one point by generation and work ID, without vectors,
requesting identity/schema plus either `poetry_locators` or `poetry_work_document`.
Representative works were `tangshisanbaishou:1` and `yudingquantangshi:15662`.
Each work received four scroll reads and one direct point retrieval for comparison.

| Measurement | Observed range |
| --- | --- |
| HTTP outcomes | 10/10 HTTP 200, each returning one point |
| Qdrant envelope processing time, all reads | 0.120-12.374 ms |
| Qdrant envelope processing time, Rust-equivalent scroll | 0.187-12.374 ms |
| RN reused HTTPS complete request | 11.483-46.624 ms, median 11.924 ms |
| RN new HTTPS complete request | 40.031-60.382 ms, median 58.810 ms |
| New connection establishment | 26.930-46.365 ms |
| Returned requested field UTF-8 bytes | 95-659 bytes |

The two first locator scrolls took 58.865 and 60.382 ms including TLS connection setup.
Corresponding Qdrant processing times were 0.191 and 12.374 ms. Both became about
11.5-11.7 ms with a reused connection. Direct point retrieval does not show a material
latency improvement over the filtered scroll at this workload.

These results do not support a seconds-long metadata index or small-payload serialization
bottleneck. A 5-second Worker timeout is more consistent with regional egress/connection
behavior, or another stage such as vector search, than these metadata lookups.

Limits: this measures metadata only from RN, without concurrent load. New connection means
fresh TCP/TLS, not an empty Qdrant disk/page cache. Qdrant processing time excludes network
transit and portions of gateway handling. It does not identify the timed-out Worker call;
stage-specific Worker timing and a vector-query control are needed for that conclusion.

Reproduction uses `uv run python document-latency-run.py`. RN has no `uv` binary, so the
ssh-skill operation invokes its existing Python 3 for a standard-library-only remote script.
No endpoint, API key, or raw poem text is logged.
