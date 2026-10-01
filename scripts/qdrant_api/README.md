# Qdrant-only Worker API payload

The live Worker uses the separate payload-only API collection described below. Direct payload
enrichment of the vector collection is retired: Qdrant can physically copy vector segments when
setting payload, and optimization requires temporary disk space. Existing direct-enrichment
artifacts remain only for auditing and controlled recovery.

The runtime uses only Qdrant. The canonical Go export is an offline preparation input.
No embedding, vector replacement, new point, collection creation, or payload overwrite is used.

Prepare a reviewable plan with a complete cloud locator inventory:

```sh
uv run --env-file .env python scripts/qdrant_api/cli.py prepare \
  --canonical-db data/reports/worker-rust-20260930/corpus/poetry-d1.sqlite \
  --canonical-report data/reports/worker-rust-20260930/corpus/verification.json \
  --plan-dir /path/to/new/payload-plan
```

Each existing point gains `poetry_api_schema="poetry-api-v1"` and `poetry_original`.
Only the minimum UUID point of each work gains `poetry_work_document`, the exact canonical API
detail document as a JSON string. Add an `on_disk` keyword `work_id` index when absent. Worker
detail reads filter `generation` and `work_id`, scroll one point in default UUID order, and parse
the document. Search requests include only locator, schema, and original payload fields.

The preparation directory contains the full before/additions manifest, counts, byte estimates,
and a rollback description. It rejects field conflicts, unexpected UUIDs, changed locators,
incomplete corpora, and a canonical export without its matching full Go verification report.

Read-only preflight repeats the inventory and confirms minimum UUID anchors before any writes:

```sh
uv run --env-file .env python scripts/qdrant_api/cli.py preflight --plan-dir /path/to/payload-plan
```

After reviewing the concrete plan, apply with an explicit flag and a new checkpoint:

```sh
uv run --env-file .env python scripts/qdrant_api/cli.py apply --apply \
  --plan-dir /path/to/payload-plan --checkpoint /path/to/new/payload-checkpoint.json
```

Apply inventories every original field again before mutation. It sends at most 128 additive
operations and 1 MiB per request, waits for acknowledgement, and starts at most two batches per
second. Checkpoints bind the endpoint and manifest. Ambiguous completed batches replay safely;
conflicting fields or a checkpoint ahead of confirmed metadata are rejected. Final verification
reads every payload without vectors and compares original fields and all expected additions.
Every batch now checks collection health before writing; a non-green state stops the operation.

Rollback is described but never executed automatically. Remove only the three owned fields from
the exact manifest IDs; remove the `work_id` index only when the checkpoint proves it was created
by this operation. Existing Go importer full upserts replace payloads; future imports must retain
these fields before a new corpus generation can serve the Worker.

```sh
uv run python -m unittest discover -s scripts/qdrant_api -p 'test_*.py'
```

## Isolated Payload-Only Documents

The document publisher creates `poetry_tang_api_20260930_v1` with zero vector definitions.
It stores one point per canonical work, using its minimum existing vector UUID as the document ID.
The payload retains raw canonical `poetry_work_document` and `poetry_locators` JSON strings plus
`work_id`, `generation`, and `poetry_api_schema`. Both lookup indexes are on-disk keyword indexes.

Create a local plan before publishing:

```sh
uv run python scripts/qdrant_api/documents.py plan \
  --canonical-db data/reports/worker-rust-20260930/corpus/poetry-d1.sqlite \
  --canonical-report data/reports/worker-rust-20260930/corpus/verification.json \
  --plan-dir data/reports/worker-qdrant-20260930/document-plan
```

After reviewing its byte estimates, publish with an explicit flag and a new checkpoint:

```sh
uv run --env-file .env python scripts/qdrant_api/documents.py publish --apply \
  --plan-dir data/reports/worker-qdrant-20260930/document-plan \
  --checkpoint data/reports/worker-qdrant-20260930/document-checkpoint.json
```

Requests contain at most 128 payload-only points and 1 MiB, starting at most twice per second.
Each batch first checks both collection health states. Resume checks every stored document against
the reviewed manifest; final verification compares all IDs and raw strings, confirms zero vectors,
and writes `verification.json`. The rollback description covers only this newly owned collection.
The existing vector collection receives no writes from this publisher.
