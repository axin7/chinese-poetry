"""Full payload inventory and comparison with no vector downloads."""

from collections import Counter
import hashlib
import re
import uuid

from support import OWNED, ORIGINAL, PATH, SCHEMA, encoded


def points(client, batch_size=1024, log=False):
    offset, previous, seen = None, None, 0
    while True:
        request = {"limit": batch_size, "with_vector": False, "with_payload": True}
        if offset is not None:
            request["offset"] = offset
        page = client.request("POST", PATH + "/points/scroll", request)
        for point in page["points"]:
            point_id = point.get("id")
            if not isinstance(point_id, str) or str(uuid.UUID(point_id)) != point_id or \
                    (previous is not None and point_id <= previous):
                raise ValueError("Qdrant inventory IDs are invalid, duplicated, or out of order")
            if not isinstance(point.get("payload"), dict):
                raise ValueError("Qdrant inventory payload is not an object")
            previous, seen = point_id, seen + 1
            if log and seen % 8192 == 0:
                print(f'{{"phase":"inventory","points":{seen}}}', flush=True)
            yield point
        following = page.get("next_page_offset")
        if following is None:
            return
        if not page["points"] or following == offset:
            raise ValueError("Qdrant inventory scroll made no progress")
        offset = following


def canonical_point(point, canonical):
    point_id, payload = point["id"], point["payload"]
    expected = canonical.points.get(point_id)
    if expected is None:
        raise ValueError("Qdrant point UUID is not in the verified canonical export")
    for field in ORIGINAL:
        if field not in payload:
            raise ValueError(f"Qdrant point is missing original field {field}")
    for field, value in expected.items():
        if field == "original":
            continue
        if type(payload[field]) is not type(value) or payload[field] != value:
            raise ValueError(f"Qdrant locator differs from canonical field {field}")
    if not isinstance(payload["translation_hash"], str) or \
            not re.fullmatch(r"[0-9a-f]{64}", payload["translation_hash"]):
        raise ValueError("Qdrant translation hash is invalid")
    if any(field in payload for field in OWNED):
        raise ValueError("API-owned payload fields already exist; use the original plan checkpoint")
    additions = {"poetry_api_schema": SCHEMA, "poetry_original": expected["original"]}
    if canonical.anchors[expected["work_id"]] == point_id:
        additions["poetry_work_document"] = canonical.documents[expected["work_id"]]
    return {"id": point_id, "before": payload, "additions": additions}


def compare_inventory(client, manifest, completed=None, exact=False, log=False):
    before_hash, after_hash = hashlib.sha256(), hashlib.sha256()
    counts, documents, total, api_points = Counter(), 0, 0, 0
    expected = iter(manifest)
    for point in points(client, log=log):
        record = next(expected, None)
        if record is None or record["id"] != point["id"]:
            raise ValueError("Current collection IDs differ from the reviewed plan")
        payload, additions = point["payload"], record["additions"]
        old = {key: value for key, value in payload.items() if key not in OWNED}
        if encoded(old, compact=True) != encoded(record["before"], compact=True):
            if old != record["before"]:
                raise ValueError("Original Qdrant payload changed since the reviewed plan")
        present = {key: payload[key] for key in OWNED if key in payload}
        required = exact or (completed is not None and total < completed)
        if present != additions and (present or required):
            raise ValueError("API fields conflict with the plan or checkpoint progress")
        if completed is None and not exact and present:
            raise ValueError("Fresh apply cannot adopt preexisting API fields")
        digest = encoded({"id": point["id"], "payload": old}, compact=True)
        before_hash.update(digest)
        after_hash.update(encoded({"id": point["id"], "payload": payload}, compact=True))
        counts[old["dataset"]] += 1
        documents += "poetry_work_document" in present
        api_points += bool(present)
        total += 1
    if next(expected, None) is not None:
        raise ValueError("Current collection is missing points from the reviewed plan")
    return {"points": total, "api_points": api_points,
            "dataset_points": dict(counts), "work_documents": documents,
            "original_payload_sha256": before_hash.hexdigest(),
            "all_payload_sha256": after_hash.hexdigest()}
