"""Explicit, resumable set-payload application; every request retains existing vectors."""

from itertools import islice
import json
from pathlib import Path
import time

from batches import batches
from canonical import validate_document
from inventory import compare_inventory
from support import (GENERATION, OWNED, PATH, SCHEMA, atomic_json, collection_info,
                     digest_file, json_lines, payload_index)


def reviewed_plan(directory, client):
    directory = Path(directory)
    plan = json.loads((directory / "plan.json").read_text())
    manifest = directory / "manifest.jsonl"
    if plan.get("version") != 1 or plan.get("schema") != SCHEMA or \
            plan.get("generation") != GENERATION or plan.get("target_endpoint") != client.url:
        raise ValueError("Plan generation, schema, or target endpoint mismatch")
    if digest_file(manifest) != plan["manifest_sha256"]:
        raise ValueError("Reviewed manifest was modified")
    validate_manifest(manifest, plan)
    return plan, manifest


def validate_manifest(manifest, plan):
    previous, total, documents, work_ids = None, 0, 0, set()
    for record in json_lines(manifest):
        if set(record) != {"id", "before", "additions"} or \
                (previous is not None and record["id"] <= previous):
            raise ValueError("Manifest format or UUID ordering mismatch")
        additions = record["additions"]
        if not set(additions).issubset(OWNED) or additions.get("poetry_api_schema") != SCHEMA or \
                not isinstance(additions.get("poetry_original"), str):
            raise ValueError("Manifest can only add the reviewed API-owned payload fields")
        if any(key in record["before"] for key in OWNED):
            raise ValueError("Manifest baseline contains preexisting API-owned fields")
        work_id = record["before"]["work_id"]
        first = work_id not in work_ids
        if ("poetry_work_document" in additions) is not first:
            raise ValueError("Work document must occur only on the first UUID point of its work")
        work_ids.add(work_id)
        if "poetry_work_document" in additions:
            if not isinstance(additions["poetry_work_document"], str):
                raise ValueError("Work document must remain a canonical raw JSON string")
            validate_document(work_id, additions["poetry_work_document"])
            documents += 1
        previous, total = record["id"], total + 1
    if total != plan["inventory"]["points"] or documents != plan["works"]:
        raise ValueError("Manifest counts differ from the reviewed plan")
    return {"points": total, "work_id_coverage": len(work_ids),
            "document_anchors": documents, "anchor_order": "default UUID ascending"}


def initial_state(plan):
    return {"version": 1, "binding": {key: plan[key] for key in (
        "target_endpoint", "manifest_sha256", "canonical_sqlite_sha256", "generation")},
        "next_operation": 0, "index_created": False, "phase": "prepared"}


def load_state(checkpoint, plan):
    expected = initial_state(plan)
    path = Path(checkpoint)
    if not path.exists():
        return expected, True
    state = json.loads(path.read_text())
    if state.get("version") != 1 or state.get("binding") != expected["binding"]:
        raise ValueError("Checkpoint cannot be reused with another corpus, manifest, or endpoint")
    index = state.get("next_operation")
    if type(index) is not int or not 0 <= index <= plan["inventory"]["points"]:
        raise ValueError("Checkpoint progress is invalid")
    if type(state.get("index_created")) is not bool:
        raise ValueError("Checkpoint index ownership is invalid")
    return state, False


def preflight(client, plan, manifest, state, fresh, log=False):
    info = collection_info(client)
    if info.get("points_count") != plan["inventory"]["points"] or \
            info.get("indexed_vectors_count", 0) < plan["inventory"]["points"]:
        raise ValueError("Qdrant point/vector count changed after preparation")
    if info["config"] != plan["collection_config"]:
        raise ValueError("Collection settings changed after the plan was prepared")
    before = compare_inventory(client, json_lines(manifest),
                               completed=None if fresh else state["next_operation"], log=log)
    if before["original_payload_sha256"] != plan["inventory"]["original_payload_sha256"]:
        raise ValueError("Original metadata fingerprint changed after preparation")
    return info, before


def prepare_index(client, plan, checkpoint, state):
    info = collection_info(client)
    existing = payload_index(info)
    if plan["work_id_index_before"] is not None:
        if existing != plan["work_id_index_before"]:
            raise ValueError("Preexisting work_id index changed; refusing to adopt it")
        return
    if existing is not None and not state.get("index_create_intent"):
        raise ValueError("Another writer created work_id index after preparation")
    state["index_create_intent"] = True
    atomic_json(checkpoint, state)
    if existing is None:
        result = client.request("PUT", PATH + "/index?wait=true", {
            "field_name": "work_id", "field_schema": {"type": "keyword", "on_disk": True}})
        require_completed(result)
    index = payload_index(collection_info(client))
    if index is None:
        raise ValueError("New work_id index did not become available")
    state["index_created"] = True
    atomic_json(checkpoint, state)


def require_completed(result):
    results = result if isinstance(result, list) else [result]
    if not results or any(not isinstance(row, dict) or row.get("status") != "completed"
                          for row in results):
        raise RuntimeError("Qdrant wait=true operation was not acknowledged as completed")


def apply_batches(client, manifest, checkpoint, state, delay=0.5, log=False):
    records = islice(json_lines(manifest), state["next_operation"], None)
    previous = 0.0
    for batch in batches(records):
        collection_info(client)
        remaining = delay - (time.monotonic() - previous)
        if remaining > 0:
            time.sleep(remaining)
        previous = time.monotonic()
        result = client.request("POST", PATH + "/points/batch?wait=true", batch)
        require_completed(result)
        state["next_operation"] += len(batch["operations"])
        state["phase"] = "applying"
        atomic_json(checkpoint, state)
        if log:
            print(json.dumps({"phase": "applying", "points": state["next_operation"]}), flush=True)


def verify(client, directory, log=False):
    plan, manifest = reviewed_plan(directory, client)
    info = collection_info(client)
    if info.get("points_count") != plan["inventory"]["points"] or \
            info.get("indexed_vectors_count", 0) < plan["inventory"]["points"]:
        raise ValueError("Point/vector counts changed during enrichment")
    if payload_index(info) is None:
        raise ValueError("work_id keyword index is missing")
    result = compare_inventory(client, json_lines(manifest), exact=True, log=log)
    if result["original_payload_sha256"] != plan["inventory"]["original_payload_sha256"] or \
            result["work_documents"] != plan["works"]:
        raise ValueError("Original metadata or canonical work document verification failed")
    result.update(success=True, model_calls=0, vector_writes=0, new_points=0,
                  indexed_vectors=info["indexed_vectors_count"])
    atomic_json(Path(directory) / "verification.json", result)
    return result


def apply(client, directory, checkpoint, explicit=False, log=False):
    if not explicit:
        raise ValueError("Remote mutation requires the explicit --apply flag")
    plan, manifest = reviewed_plan(directory, client)
    state, fresh = load_state(checkpoint, plan)
    preflight(client, plan, manifest, state, fresh, log)
    if fresh:
        atomic_json(checkpoint, state)
    prepare_index(client, plan, checkpoint, state)
    apply_batches(client, manifest, checkpoint, state, log=log)
    result = verify(client, directory, log)
    state["phase"] = "verified"
    atomic_json(checkpoint, state)
    return result


def inspect_plan(client, directory, checkpoint=None, log=False):
    plan, manifest = reviewed_plan(directory, client)
    state, fresh = ((initial_state(plan), True) if checkpoint is None
                    else load_state(checkpoint, plan))
    info, inventory = preflight(client, plan, manifest, state, fresh, log)
    result = validate_manifest(manifest, plan)
    result.update(success=True, mutation_requests=0, vector_writes=0,
                  work_id_index_required=plan["work_id_index_required"],
                  current_api_fields_points=inventory["api_points"],
                  current_work_documents=inventory["work_documents"],
                  indexed_vectors_raw=info["indexed_vectors_count"],
                  original_payload_sha256=inventory["original_payload_sha256"])
    return result
