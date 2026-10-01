# Worker corpus export

Use the canonical Go repository to preserve split works and raw/normalized sentence indexes:

```sh
go run ./cmd/worker-export \
  -db data/reports/rn-tang-deploy-20260922/corpus/chinese_poetry.db \
  -datasets data/reports/rn-tang-deploy-20260922/corpus/datas.json \
  -generation poetry-20260921-v1 \
  -expected-sentences 127031 \
  -out /path/to/new/poetry-api.sqlite \
  -report /path/to/new/verification.json
```

Destination files must not exist. Source files are opened read-only. The exporter checks every
sentence locator and every canonical work document before producing the verification report.
SQLite is an offline interchange artifact for publishing the Qdrant payload-only API collection
with `scripts/qdrant_api/documents.py`. The Rust Worker uses Qdrant and has no D1 binding.

`document` contains the exact API detail fields. `locators` maps
`source_row_id:raw_index:normalized_index` to canonical original sentence text. Workers must
validate dataset/work ID and generation before checking the exact locator key. Completed works
with no indexable sentences have an empty locator object but retain their detail document.

Publishing the API collection adds no vectors and makes no embedding requests. Existing vector
points are preserved. See `scripts/qdrant_api/README.md` for the prepare, publish and verify steps.
