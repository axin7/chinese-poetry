"""The immutable Tang corpus contract; no embedding dependencies."""

from collections import Counter
import hashlib
import json
import math
import struct
import uuid


COLLECTION = "poetry_tang_20260922_v1"
GENERATION = "poetry-20260921-v1"
COUNTS = {"yudingquantangshi": 125500, "tangshisanbaishou": 1531}
TOTAL = sum(COUNTS.values())
DIMENSION = 1024
PATH = "/collections/" + COLLECTION
FILTER = {"must": [{"key": "generation", "match": {"value": GENERATION}}]}
INTEGER_FIELDS = ("source_row_id", "raw_index", "normalized_index")
STRING_FIELDS = ("dataset", "work_id", "generation", "translation_hash")


def count(client, dataset=None):
    payload = {"exact": True}
    if dataset is not None:
        payload["filter"] = {"must": FILTER["must"] + [
            {"key": "dataset", "match": {"value": dataset}},
        ]}
    return client.request("POST", PATH + "/points/count", payload)["count"]


def validate_counts(client):
    total = count(client)
    counts = {name: count(client, name) for name in COUNTS}
    if total != TOTAL or counts != COUNTS:
        raise ValueError("Collection total/dataset/generation counts differ from the Tang contract")
    return counts


def validate_config(info):
    config = info["config"]
    params = config["params"]
    vectors = params["vectors"]
    if (vectors.get("size") != DIMENSION or vectors.get("distance") != "Cosine"
            or vectors.get("on_disk") is not True or params.get("on_disk_payload") is not True):
        raise ValueError("Collection vector or payload disk contract differs")
    hnsw = vectors.get("hnsw_config") or config["hnsw_config"]
    if hnsw.get("m") != 16 or hnsw.get("on_disk", False):
        raise ValueError("Collection HNSW contract differs")
    quantization = vectors.get("quantization_config") or config.get("quantization_config", {})
    scalar = quantization.get("scalar", {})
    if scalar.get("type") != "int8" or scalar.get("always_ram") is not True:
        raise ValueError("Collection INT8 quantization contract differs")


def validate_indexes(info):
    for field in ("dataset", "generation"):
        if info.get("payload_schema", {}).get(field, {}).get("data_type") != "keyword":
            raise ValueError(f"Missing or incompatible {field} keyword index")


def creation_config(info):
    config = info["config"]
    params = config["params"]
    return {
        "vectors": params["vectors"], "shard_number": 1,
        "replication_factor": 1, "write_consistency_factor": 1,
        "on_disk_payload": True,
        "hnsw_config": {**config["hnsw_config"], "max_indexing_threads": 1},
        "quantization_config": config["quantization_config"],
        "optimizers_config": {
            "indexing_threshold": config["optimizer_config"]["indexing_threshold"],
            "default_segment_number": 2, "max_optimization_threads": 1,
        },
    }


def config_fingerprint(info):
    encoded = json.dumps(creation_config(info), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_point(point):
    identifier = point.get("id")
    if not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
        raise ValueError("Source must contain canonical UUID point IDs")
    payload, vector = point.get("payload"), point.get("vector")
    if not isinstance(payload, dict) or not isinstance(vector, list) or len(vector) != DIMENSION:
        raise ValueError("Point payload or vector dimension is invalid")
    if any(type(payload.get(field)) is not int or payload[field] < 0 for field in INTEGER_FIELDS):
        raise ValueError("Point locator payload types are invalid")
    if any(not isinstance(payload.get(field), str) or not payload[field]
           for field in STRING_FIELDS):
        raise ValueError("Point string payload fields are invalid")
    if payload["dataset"] not in COUNTS or payload["generation"] != GENERATION:
        raise ValueError("Point falls outside the immutable Tang generation")
    if payload["work_id"] != f"{payload['dataset']}:{payload['source_row_id']}":
        raise ValueError("Point work ID and locator disagree")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
        raise ValueError("Point vector contains a non-finite or invalid value")


def update_fingerprint(digest, point, vector=True):
    digest.update(point["id"].encode())
    payload = json.dumps(point["payload"], sort_keys=True, separators=(",", ":"))
    digest.update(payload.encode())
    if vector:
        digest.update(struct.pack(f"<{DIMENSION}f", *point["vector"]))


def scroll(client, batch_size, offset=None):
    payload = {"limit": batch_size, "with_vector": True, "with_payload": True}
    if offset is not None:
        payload["offset"] = offset
    return client.request("POST", PATH + "/points/scroll", payload)


def points(client, batch_size):
    offset, previous = None, None
    while True:
        page = scroll(client, batch_size, offset)
        for point in page["points"]:
            validate_point(point)
            if previous is not None and point["id"] <= previous:
                raise ValueError("Scroll IDs are duplicated or out of order")
            previous = point["id"]
            yield point
        offset = page.get("next_page_offset")
        if offset is None:
            return
        if not page["points"]:
            raise ValueError("Scroll made no progress")


def inventory(client, batch_size):
    digest, counts = hashlib.sha256(), Counter()
    for point in points(client, batch_size):
        update_fingerprint(digest, point)
        counts[point["payload"]["dataset"]] += 1
    if dict(counts) != COUNTS:
        raise ValueError("Full source inventory does not match expected dataset counts")
    return {"sha256": digest.hexdigest(), "counts": dict(counts), "total": sum(counts.values())}
