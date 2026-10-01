# RN Tang collection to Qdrant Cloud

This tool copies only `poetry_tang_20260922_v1`: 127,031 existing 1,024-dimensional
BGE-M3 vectors and their original IDs/payloads. It makes no embedding requests.
Run on RN while the source collection remains immutable; keep production reads on
the old collection until migration verification and API validation complete.

The source is Qdrant 1.15.5 and the Cloud cluster is 1.19.1. Direct snapshot restore
is incompatible with the supported same/next-minor version rule. The official
migration CLI supports copying and resumable offsets, but its current API key
configuration uses CLI flags and its sequential offset key is the collection name.
This tool reads credentials from the environment and binds a local checkpoint to
both endpoints, the collection, generation, source config and full source digest.

## Run

Provide `QDRANT_CLUSTER_ENDPOINT` and `QDRANT_API_KEY` through a private environment
file. `QDRANT_SOURCE_ENDPOINT` defaults to `http://127.0.0.1:17333`. No key is passed
as a CLI argument or written into reports/checkpoints.

```sh
uv run --env-file /opt/poetry-tang-20260922/.env.cloud python \
  /opt/poetry-tang-20260922/tools/qdrant_cloud/migrate.py plan \
  --checkpoint /opt/poetry-tang-20260922/cloud-migration/checkpoint.json \
  --report /opt/poetry-tang-20260922/cloud-migration/plan.json
```

Replace `plan` with `migrate` and choose a report filename. The migration creates a
new collection, keyword indexes, and copies points sequentially in batches of 128.
It preserves on-disk vectors/payloads, Cosine, HNSW M=16 and INT8 always-RAM settings.
Indexing/optimization threads are limited to one. The target collection must be
absent on the first run; an existing collection requires this exact checkpoint.
Rerunning the same command resumes after interruption. The checkpoint advances
only after acknowledged `wait=true` upserts, so repeating an interrupted batch is
idempotent. Do not remove or edit a checkpoint to bypass a failed integrity check.

Success requires all 127,031 vectors indexed, both exact dataset/generation counts,
full ordered ID/payload/float32 comparison, and generation/dataset-filtered stored
vector searches using EF=64, indexed-only and disabled quantization rescoring.
Search reports include top-1 IDs, scores and payloads for investigation. Identical
stored-vector ties are recorded separately from changed search results.

`verify` repeats verification without upserts. Float32 digest differences fail by
default. A failed report lists changed vector counts, maximum component delta,
norms and normalization candidates. After investigation,
`--allow-normalization-drift` explicitly accepts only differences bounded by 1e-6
whose normalized direction agrees within 1e-7; it still rejects metadata changes,
non-normalization vector changes and unequal-vector top-1 changes.

Retain RN storage/snapshots and the original API configuration for rollback.
The tool never switches the API or deletes collections. Free Cloud inactivity
deletion and missing automated backups require an independent backup policy.

## Local Checks

```sh
uv run python -m unittest discover -s scripts/qdrant_cloud -p 'test_*.py'
```

References: [migration CLI](https://github.com/qdrant/migration),
[snapshot compatibility](https://qdrant.tech/documentation/snapshots/),
[bulk uploading](https://qdrant.tech/documentation/database-tutorials/bulk-upload/).
