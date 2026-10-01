"""Shared protocol and durable files for additive API payload updates."""

import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qdrant_cloud.common import Client, atomic_json, checkpoint_lock, endpoint
from qdrant_cloud.contract import validate_config, validate_indexes

COLLECTION = "poetry_tang_20260922_v1"
GENERATION = "poetry-20260921-v1"
SCHEMA = "poetry-api-v1"
PATH = "/collections/" + COLLECTION
OWNED = ("poetry_api_schema", "poetry_original", "poetry_work_document")
ORIGINAL = ("dataset", "generation", "work_id", "source_row_id", "raw_index",
            "normalized_index", "translation_hash")
MAX_BATCH_BYTES = 1024 * 1024


def encoded(value, compact=False):
    kwargs = {"allow_nan": False}
    if compact:
        kwargs.update(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, **kwargs).encode("utf-8")


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def json_lines(path):
    with Path(path).open() as handle:
        for line in handle:
            yield json.loads(line)


def write_lines(path, records):
    with Path(path).open("xb") as handle:
        os.chmod(path, 0o600)
        for record in records:
            handle.write(encoded(record, compact=True) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def cloud_client():
    url = os.environ.get("QDRANT_CLUSTER_ENDPOINT", "")
    key = os.environ.get("QDRANT_API_KEY", "")
    if not url or not key:
        raise ValueError("QDRANT_CLUSTER_ENDPOINT and QDRANT_API_KEY are required")
    endpoint(url, cloud=True)
    return Client(url, key, timeout=120)


def collection_info(client):
    info = client.request("GET", PATH)
    validate_config(info)
    validate_indexes(info)
    if info.get("status") != "green":
        raise ValueError("Collection must be green before payload preparation")
    return info


def payload_index(info):
    schema = info.get("payload_schema", {}).get("work_id")
    if schema is not None and schema.get("data_type") != "keyword":
        raise ValueError("Existing work_id index has an incompatible type")
    return schema
