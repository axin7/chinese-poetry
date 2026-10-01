# Read-Only Recovery Analysis

No remote mutation or deletion was performed during this diagnosis.

The reported `625.69 MiB` required / `33.86 MiB` available is a stored optimizer error, not a live
free-space reading. After writes stopped, successful merges continued: the collection went from
six segments to four, memory resident usage fell from 389,677,056 to 252,706,816 bytes, and the
optimization endpoint reported no running or queued jobs. Logical point count remained 127,031;
the 128 extra indexed vectors are compatible with temporary deleted/copy records.

## Snapshot Evidence

Live collection and full snapshot lists are empty. Telemetry reports zero snapshot creations,
no active snapshot creation/recovery, and only the one Tang collection. There is no identified
Cloud snapshot whose removal could reclaim space. Evidence: `snapshot-inspection.json`.

The retained RN rollback snapshot is separate from Cloud storage:

- File: `/opt/poetry-tang-20260922/snapshots/poetry_tang_20260922_v1-829596450146656-2026-09-22-05-25-39.snapshot`.
- Live RN stat confirms 781,660,672 bytes, `root:root`, last modification
  `2026-09-22 05:35:03.115200676 UTC`; filename creation timestamp is 05:25:39.
- Recorded SHA256: `a612af484f93cf87dc0b85f35d9e89e8ce6ac1be9d7ddcb0f49cc40cb3d6fb4a`.
- Ownership evidence: `rn-tang-deploy-20260922/build_subset.py` creates the snapshot; the subset
  manifest records its checksum and upload/verify reports identify the RN destination.
- Cloud migration copied points because restoring a 1.15.5 snapshot into 1.19.1 is unsupported;
  its migration scripts contain no Cloud snapshot creation call.

## Same-Configuration Reset

Pinned Qdrant v1.19.1 source establishes a concrete way to reset a stored optimizer error:

- [`collection_meta_ops.rs`](https://github.com/qdrant/qdrant/blob/v1.19.1/lib/storage/src/content_manager/toc/collection_meta_ops.rs#L172)
  sets recreation whenever an optimizer diff is supplied, without checking whether values changed.
- [`updaters.rs`](https://github.com/qdrant/qdrant/blob/v1.19.1/lib/collection/src/shards/local_shard/updaters.rs#L74)
  stops/restarts workers, assigns `optimizer_errors = None` at line 138, and sends `Nop` at line 140.
- [`optimizer_config_update_tests.rs`](https://github.com/qdrant/qdrant/blob/v1.19.1/lib/collection/src/shards/local_shard/optimizer_config_update_tests.rs#L152)
  explicitly verifies that an unchanged configuration update clears optimizer errors.

A single root-reviewed PATCH containing
`{"optimizers_config":{"max_optimization_threads":1}}` therefore preserves the current value,
clears the stored error, and triggers the planner. This analysis did not execute it. Its success
must be judged from subsequent health, optimizer status, configuration equality, counts and search
checks. If actual free space is still inadequate, the optimizer can fail again.

## Space Constraints

[`optimize.rs`](https://github.com/qdrant/qdrant/blob/v1.19.1/lib/shard/src/optimize.rs#L723)
requires twice the physical size of segments selected for rebuilding. JSON payload estimates
exclude this temporary reserve and filesystem/WAL overhead. The telemetry filesystem size is
capacity, not available space.

`wal_capacity_mb=32`, `wal_segments_ahead=0`, and `wal_retain_closed=1` were already in use.
No WAL file sizes or reclaimable backlog are exposed by the inspected endpoints. Reducing the
32 MiB segment capacity or one retained segment cannot establish the missing 591.83 MiB reserve.
WAL/segment files must not be deleted manually.

`memmap_threshold` changes placement, while vectors and payloads already reside on disk.
Changing index thresholds or maximum segment sizes does not reclaim existing files immediately.
Disabling optimizer workers can suppress background work but does not solve disk pressure and
may leave search coverage incomplete; it is not a confirmed recovery here.

[`payload.rs`](https://github.com/qdrant/qdrant/blob/v1.19.1/lib/shard/src/update/payload.rs#L28)
uses conditional point moves for both additive updates and payload deletions. Logical vector
values can remain identical while physical vector/segment copies consume temporary space.
Deleting the newly added fields is therefore not an immediate disk-space remedy.

If the same-value reset still fails, keep enrichment stopped and retain the original RN fallback.
The next safe investigation is Cloud physical disk/segment/WAL usage through account diagnostics
or provider support; no removable Cloud-owned artifact was identified in this read-only audit.
