"""Publish only into the isolated payload-only document collection."""

from itertools import islice
import json
from pathlib import Path
import time

from documents_contract import (COLLECTION, CREATE_CONFIG, DOCUMENT_PATH, document_batches,
                                guard_health, stored_points, validate_collection)
from documents_plan import payload_fingerprint, reviewed_manifest
from support import PATH, atomic_json, json_lines


def completed(result):
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise RuntimeError("Document wait=true upsert was not acknowledged as completed")


def load_state(path, plan, client):
    binding = {"endpoint": client.url, "collection": COLLECTION,
               "canonical_sqlite_sha256": plan["canonical_sqlite_sha256"],
               "manifest_sha256": plan["manifest_sha256"]}
    if not Path(path).exists():
        return {"version": 1, "binding": binding, "next_point": 0,
                "created": False, "collection_uuid": None, "phase": "prepared"}, True
    state = json.loads(Path(path).read_text())
    if state.get("version") != 1 or state.get("binding") != binding:
        raise ValueError("Document checkpoint belongs to another source, endpoint, or collection")
    if type(state.get("next_point")) is not int or not 0 <= state["next_point"] <= plan["points"]:
        raise ValueError("Document checkpoint progress is invalid")
    if type(state.get("created")) is not bool:
        raise ValueError("Document checkpoint collection ownership is invalid")
    return state, False


def compare_existing(client, expected, required_ids=(), complete=False):
    seen, actual = set(), []
    for point in stored_points(client):
        wanted = expected.get(point["id"])
        if wanted is None or point["payload"] != wanted["payload"]:
            raise ValueError("Stored document ID or canonical payload differs from its plan")
        seen.add(point["id"])
        actual.append(point)
    if not set(required_ids).issubset(seen):
        raise ValueError("Document checkpoint is ahead of stored canonical points")
    if complete and seen != set(expected):
        raise ValueError("Document collection is missing canonical work points")
    return {"points": len(seen), "payload_sha256": payload_fingerprint(actual)}


def create_target(client, plan, expected, checkpoint, state, fresh):
    source = client.request("GET", PATH)
    if source.get("status") != "green" or source.get("optimizer_status") != "ok" or \
            source.get("points_count") != plan["original_vector_points"]:
        raise ValueError("Original vector collection must be healthy and retain its logical count")
    info = client.request("GET", DOCUMENT_PATH, missing=True)
    if fresh and info is not None:
        raise ValueError("Document collection already exists without this publisher checkpoint")
    if info is None:
        if state["created"]:
            raise ValueError("Owned document collection disappeared")
        state["creation_intent"] = True
        atomic_json(checkpoint, state)
        client.request("PUT", DOCUMENT_PATH, CREATE_CONFIG)
        info = client.request("GET", DOCUMENT_PATH)
    elif not state.get("creation_intent"):
        raise ValueError("Checkpoint cannot prove ownership of the existing document collection")
    validate_collection(info, state["collection_uuid"])
    required = list(sorted(expected))[:state["next_point"]]
    compare_existing(client, expected, required_ids=required)
    state.update(created=True, collection_uuid=info["config"].get("uuid"))
    atomic_json(checkpoint, state)
    for field in ("generation", "work_id"):
        if field not in info.get("payload_schema", {}):
            result = client.request("PUT", DOCUMENT_PATH + "/index?wait=true", {
                "field_name": field, "field_schema": {"type": "keyword", "on_disk": True}})
            completed(result)
    validate_collection(client.request("GET", DOCUMENT_PATH), state["collection_uuid"],
                        require_indexes=True)


def publish_batches(client, manifest, checkpoint, state, log=False, delay=0.5):
    remaining = islice(json_lines(manifest), state["next_point"], None)
    previous = 0.0
    for batch in document_batches(remaining):
        wait = delay - (time.monotonic() - previous)
        if wait > 0:
            time.sleep(wait)
        guard_health(client, state["collection_uuid"])
        previous = time.monotonic()
        result = client.request("PUT", DOCUMENT_PATH + "/points?wait=true", batch)
        completed(result)
        state["next_point"] += len(batch["points"])
        state["phase"] = "publishing"
        atomic_json(checkpoint, state)
        if log:
            print(json.dumps({"phase": "documents", "points": state["next_point"]}), flush=True)


def verify(client, directory, checkpoint, log=False):
    plan, _, expected = reviewed_manifest(directory)
    state, fresh = load_state(checkpoint, plan, client)
    if fresh or not state["created"]:
        raise ValueError("Verification requires the owned document collection checkpoint")
    info = guard_health(client, state["collection_uuid"])
    if info.get("points_count") != plan["points"]:
        raise ValueError("Document collection logical point count mismatch")
    for field in ("generation", "work_id"):
        if field not in info.get("payload_schema", {}):
            raise ValueError("Document collection keyword index is missing")
    result = compare_existing(client, expected, complete=True)
    if result["payload_sha256"] != payload_fingerprint(expected[key] for key in sorted(expected)):
        raise ValueError("Document full payload fingerprint mismatch")
    info = guard_health(client, state["collection_uuid"])
    if info.get("status") != "green" or info.get("points_count") != plan["points"]:
        raise ValueError("Document collection is not yet green after full verification")
    result.update(success=True, collection=COLLECTION, vector_definitions=0,
                  indexed_vectors=0, model_calls=0, existing_collection_writes=0)
    atomic_json(Path(directory) / "verification.json", result)
    if log:
        print(json.dumps(result), flush=True)
    return result


def publish(client, directory, checkpoint, explicit=False, log=False):
    if not explicit:
        raise ValueError("Document publication requires the explicit --apply flag")
    plan, manifest, expected = reviewed_manifest(directory)
    state, fresh = load_state(checkpoint, plan, client)
    create_target(client, plan, expected, checkpoint, state, fresh)
    publish_batches(client, manifest, checkpoint, state, log=log)
    result = verify(client, directory, checkpoint, log)
    state["phase"] = "verified"
    atomic_json(checkpoint, state)
    return result
