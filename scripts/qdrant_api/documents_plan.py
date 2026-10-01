"""Create a local, canonical document plan with no remote requests."""

import hashlib
import json
from pathlib import Path

from canonical import validate_document
from documents_contract import (COLLECTION, CREATE_CONFIG, DOCUMENT_PATH, DocumentCorpus,
                                FIELDS, document_batches)
from support import GENERATION, SCHEMA, atomic_json, digest_file, encoded, json_lines, write_lines


def sizing(manifest):
    result = {"points": 0, "payload_utf8_bytes": 0, "max_payload_bytes": 0,
              "batch_requests": 0, "wire_request_bytes": 0, "max_batch_bytes": 0}
    for point in json_lines(manifest):
        size = len(encoded(point["payload"], compact=True))
        result["points"] += 1
        result["payload_utf8_bytes"] += size
        result["max_payload_bytes"] = max(result["max_payload_bytes"], size)
    for batch in document_batches(json_lines(manifest)):
        size = len(encoded(batch))
        result["batch_requests"] += 1
        result["wire_request_bytes"] += size
        result["max_batch_bytes"] = max(result["max_batch_bytes"], size)
    result["minimum_apply_seconds"] = result["batch_requests"] / 2
    return result


def prepare(database, report, directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Document plan directory must be empty")
    corpus = DocumentCorpus(database, report)
    manifest = directory / "documents.jsonl"
    write_lines(manifest, corpus.points())
    plan = {"version": 1, "collection": COLLECTION, "generation": GENERATION, "schema": SCHEMA,
            "canonical_sqlite_sha256": corpus.source_sha256,
            "manifest_sha256": digest_file(manifest), "manifest_bytes": manifest.stat().st_size,
            "points": len(corpus.documents), "original_vector_points": corpus.vector_points,
            "create_config": CREATE_CONFIG, "sizing": sizing(manifest),
            "model_calls": 0, "vector_definitions": 0, "existing_collection_writes": 0}
    atomic_json(directory / "plan.json", plan)
    atomic_json(directory / "rollback.json", {
        "collection": COLLECTION, "method": "DELETE", "path": DOCUMENT_PATH,
        "allowed_only_if_checkpoint_created": True, "must_match_checkpoint_collection_uuid": True,
        "excluded_collection": "poetry_tang_20260922_v1", "executed": False})
    return plan


def reviewed_manifest(directory):
    directory = Path(directory)
    plan = json.loads((directory / "plan.json").read_text())
    manifest = directory / "documents.jsonl"
    if plan.get("version") != 1 or plan.get("collection") != COLLECTION or \
            plan.get("generation") != GENERATION or plan.get("schema") != SCHEMA or \
            plan.get("create_config") != CREATE_CONFIG:
        raise ValueError("Reviewed document plan configuration mismatch")
    if digest_file(manifest) != plan["manifest_sha256"]:
        raise ValueError("Reviewed document manifest changed")
    expected, previous, works = {}, None, set()
    for point in json_lines(manifest):
        payload = point["payload"]
        if set(point) != {"id", "vector", "payload"} or point["vector"] != {} or \
                (previous is not None and point["id"] <= previous):
            raise ValueError("Document plan can only contain ordered payload-only points")
        if set(payload) != FIELDS or payload["generation"] != GENERATION or \
                payload["poetry_api_schema"] != SCHEMA or payload["work_id"] in works:
            raise ValueError("Document plan payload fields or unique work IDs are invalid")
        validate_document(payload["work_id"], payload["poetry_work_document"])
        if not isinstance(payload["poetry_locators"], str) or \
                not isinstance(json.loads(payload["poetry_locators"]), dict):
            raise ValueError("Document locator map must remain a canonical raw JSON string")
        works.add(payload["work_id"])
        previous = point["id"]
        expected[point["id"]] = point
    if len(expected) != plan["points"]:
        raise ValueError("Reviewed document point count mismatch")
    return plan, manifest, expected


def payload_fingerprint(points):
    digest = hashlib.sha256()
    for point in points:
        digest.update(encoded({"id": point["id"], "payload": point["payload"]}, compact=True))
    return digest.hexdigest()
