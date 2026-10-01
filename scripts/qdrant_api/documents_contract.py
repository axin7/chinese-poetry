"""Canonical payload-only collection contract and bounded REST batches."""

from pathlib import Path
import sqlite3

from canonical import Canonical
from support import GENERATION, MAX_BATCH_BYTES, PATH, SCHEMA, encoded

COLLECTION = "poetry_tang_api_20260930_v1"
DOCUMENT_PATH = "/collections/" + COLLECTION
FIELDS = {"work_id", "generation", "poetry_api_schema", "poetry_work_document", "poetry_locators"}
CREATE_CONFIG = {"vectors": {}, "on_disk_payload": True, "shard_number": 1,
                 "replication_factor": 1, "write_consistency_factor": 1,
                 "optimizers_config": {"default_segment_number": 1, "max_optimization_threads": 1}}


class DocumentCorpus:
    def __init__(self, database, report):
        canonical = Canonical(database, report)
        self.source_sha256 = canonical.sha256
        self.vector_points = canonical.total
        self.documents = {}
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            rows = connection.execute("SELECT work_id,generation,document,locators FROM works")
            for work_id, generation, document, locators in rows:
                if generation != GENERATION or document != canonical.documents[work_id]:
                    raise ValueError("Canonical document changed after Go verification")
                identifier = canonical.anchors[work_id]
                self.documents[identifier] = {"id": identifier, "vector": {}, "payload": {
                    "work_id": work_id, "generation": generation, "poetry_api_schema": SCHEMA,
                    "poetry_work_document": document, "poetry_locators": locators}}
        if len(self.documents) != len(canonical.documents):
            raise ValueError("Payload-only point IDs must be unique for every completed work")

    def points(self):
        for identifier in sorted(self.documents):
            yield self.documents[identifier]


def document_batches(points, max_points=128):
    if not 1 <= max_points <= 128:
        raise ValueError("Document batches must contain at most 128 points")
    batch, size = [], len(encoded({"points": []}))
    for point in points:
        item_size = len(encoded(point))
        following = size + item_size + (2 if batch else 0)
        if batch and (len(batch) == max_points or following > MAX_BATCH_BYTES):
            yield {"points": batch}
            batch, size = [], len(encoded({"points": []}))
        size += item_size + (2 if batch else 0)
        batch.append(point)
        if size > MAX_BATCH_BYTES:
            raise ValueError("One document exceeds the 1 MiB encoded request limit")
    if batch:
        yield {"points": batch}


def validate_collection(info, expected_uuid=None, require_indexes=False):
    params = info.get("config", {}).get("params", {})
    if params.get("vectors") != {} or params.get("on_disk_payload") is not True:
        raise ValueError("Document collection must have no vector definitions and on-disk payloads")
    if info.get("indexed_vectors_count", 0) != 0:
        raise ValueError("Payload-only collection unexpectedly contains indexed vectors")
    if expected_uuid is not None and info["config"].get("uuid") != expected_uuid:
        raise ValueError("Document collection identity changed after creation")
    for field in ("generation", "work_id"):
        index = info.get("payload_schema", {}).get(field)
        if require_indexes and index is None:
            raise ValueError("Document collection keyword lookup index is missing")
        if index is not None and (index.get("data_type") != "keyword" or
                                  index.get("params", {}).get("on_disk") is not True):
            raise ValueError("Document indexes must be on-disk keyword indexes")


def guard_health(client, expected_uuid=None):
    source = client.request("GET", PATH)
    if source.get("status") != "green" or source.get("optimizer_status") != "ok":
        raise ValueError("Original vector collection is unhealthy; stop document publication")
    info = client.request("GET", DOCUMENT_PATH)
    validate_collection(info, expected_uuid, require_indexes=True)
    if info.get("status") not in {"green", "yellow"} or info.get("optimizer_status") != "ok":
        raise ValueError("Document collection is unhealthy; stop publication")
    return info


def stored_points(client, page_size=256):
    previous, offset = None, None
    while True:
        body = {"limit": page_size, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        result = client.request("POST", DOCUMENT_PATH + "/points/scroll", body)
        for point in result["points"]:
            identifier = point["id"]
            if previous is not None and identifier <= previous:
                raise ValueError("Document scroll has duplicate or unordered IDs")
            if point.get("vector") not in ({}, None):
                raise ValueError("Document point unexpectedly contains a vector")
            previous = identifier
            yield point
        following = result.get("next_page_offset")
        if following is None:
            return
        if not result["points"] or following == offset:
            raise ValueError("Document scroll made no progress")
        offset = following
