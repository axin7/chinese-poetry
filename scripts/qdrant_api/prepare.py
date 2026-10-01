"""Create a reviewed additive manifest from a full, read-only cloud inventory."""

from collections import Counter
import hashlib
from pathlib import Path

from batches import batches
from canonical import Canonical
from inventory import canonical_point, points
from support import (COLLECTION, GENERATION, OWNED, PATH, SCHEMA, atomic_json,
                     collection_info, digest_file, encoded, json_lines, payload_index, write_lines)


def inventory_records(client, canonical, summary, log=False):
    digest, seen, counts = hashlib.sha256(), set(), Counter()
    for point in points(client, log=log):
        record = canonical_point(point, canonical)
        seen.add(record["id"])
        counts[record["before"]["dataset"]] += 1
        digest.update(encoded({"id": record["id"], "payload": record["before"]}, compact=True))
        yield record
    if seen != set(canonical.points) or counts != canonical.counts:
        raise ValueError("Full Qdrant locator inventory differs from the canonical Go export")
    summary.update(original_payload_sha256=digest.hexdigest(), points=len(seen))


def sizing(manifest):
    result = {"batch_requests": 0, "wire_request_bytes": 0, "max_batch_bytes": 0,
              "payload_addition_utf8_bytes": 0, "work_documents": 0,
              "max_point_addition_bytes": 0}
    for record in json_lines(manifest):
        size = len(encoded(record["additions"], compact=True)) - 1
        result["payload_addition_utf8_bytes"] += size
        result["max_point_addition_bytes"] = max(result["max_point_addition_bytes"], size)
        result["work_documents"] += "poetry_work_document" in record["additions"]
    for batch in batches(json_lines(manifest)):
        size = len(encoded(batch))
        result["batch_requests"] += 1
        result["wire_request_bytes"] += size
        result["max_batch_bytes"] = max(result["max_batch_bytes"], size)
    result["minimum_apply_seconds"] = result["batch_requests"] / 2
    return result


def prepare(client, canonical_db, canonical_report, output, log=False):
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Preparation directory must be empty to preserve prior review artifacts")
    canonical = Canonical(canonical_db, canonical_report)
    info = collection_info(client)
    if info.get("points_count") != canonical.total or \
            info.get("indexed_vectors_count") != canonical.total:
        raise ValueError("Cloud points/vector counts differ from the verified corpus")
    index = payload_index(info)
    manifest, summary = output / "manifest.jsonl", {}
    write_lines(manifest, inventory_records(client, canonical, summary, log))
    plan = {"version": 1, "schema": SCHEMA, "target_endpoint": client.url,
            "collection": COLLECTION, "generation": GENERATION,
            "canonical_sqlite_sha256": canonical.sha256,
            "manifest_sha256": digest_file(manifest), "manifest_bytes": manifest.stat().st_size,
            "inventory": summary, "dataset_points": dict(canonical.counts),
            "works": len(canonical.documents), "work_id_index_before": index,
            "work_id_index_required": index is None,
            "collection_config": info["config"], "sizing": sizing(manifest),
            "model_calls": 0, "vector_writes": 0, "new_points": 0}
    atomic_json(output / "plan.json", plan)
    atomic_json(output / "rollback.json", rollback_plan(plan))
    return plan


def rollback_plan(plan):
    return {"version": 1, "manifest": "manifest.jsonl",
            "manifest_sha256": plan["manifest_sha256"],
            "owned_fields": list(OWNED), "payload_values_before": "absent on every inventoried ID",
            "payload_delete": {"method": "POST", "path": PATH + "/points/payload/delete?wait=true",
                               "body_template": {"keys": list(OWNED),
                                                 "points": "128 manifest IDs"}},
            "index_delete": {"method": "DELETE", "path": PATH + "/index/work_id?wait=true",
                             "allowed_only_if_checkpoint_index_created": True,
                             "index_existed_before": plan["work_id_index_before"] is not None},
            "execution": "not executed; review checkpoint ownership before rollback"}
