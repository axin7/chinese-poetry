"""Ten read-only Qdrant metadata requests, with credential-free latency evidence."""

from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import time
from urllib.parse import urlsplit


COLLECTION = "poetry_tang_api_20260930_v1"
GENERATION = "poetry-20260921-v1"
WORKS = ("tangshisanbaishou:1", "yudingquantangshi:15662")


def credentials():
    values = {}
    for line in Path("/opt/poetry-tang-20260922/.env.cloud").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip().strip("\"'")
    url = urlsplit(values["QDRANT_CLUSTER_ENDPOINT"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Credential-free HTTPS endpoint is required")
    port = 6333 if url.port == 6334 else url.port
    return url, port, values["QDRANT_API_KEY"]


def scroll_body(work_id, field):
    return {"limit": 1, "with_vector": False,
            "with_payload": ["work_id", "generation", "poetry_api_schema", field],
            "filter": {"must": [
                {"key": "generation", "match": {"value": GENERATION}},
                {"key": "work_id", "match": {"value": work_id}},
            ]}}


def request(connection, key, path, body, record, cold):
    start = time.monotonic()
    try:
        if cold:
            connection.connect()
        connected = time.monotonic()
        connection.request("POST", path, json.dumps(body),
                           {"api-key": key, "Content-Type": "application/json"})
        response = connection.getresponse()
        headers_at = time.monotonic()
        raw = response.read()
        ended = time.monotonic()
        value = json.loads(raw)
        result = value.get("result", {})
        points = result if isinstance(result, list) else result.get("points", [])
        record.update(http_status=response.status, qdrant_seconds=value.get("time"),
                      connect_ms=round((connected - start) * 1000, 3),
                      request_to_headers_ms=round((headers_at - connected) * 1000, 3),
                      body_read_ms=round((ended - headers_at) * 1000, 3),
                      client_ms=round((ended - start) * 1000, 3),
                      response_bytes=len(raw), points_count=len(points))
        if points:
            record["field_utf8_bytes"] = len(points[0]["payload"].get(
                record["field"], "").encode("utf-8"))
        return points[0]["id"] if points else None
    except (OSError, ValueError, http.client.HTTPException) as error:
        record.update(error_type=type(error).__name__,
                      client_ms=round((time.monotonic() - start) * 1000, 3))
        return None


def measure_work(url, port, key, work_id):
    records = []
    connection = http.client.HTTPSConnection(url.hostname, port, timeout=12)
    path = url.path.rstrip("/") + "/collections/" + COLLECTION + "/points/scroll"
    point_id = None
    for position, field in enumerate(("poetry_locators", "poetry_locators",
                                      "poetry_work_document", "poetry_work_document")):
        record = {"work_id": work_id, "field": field, "kind": "scroll",
                  "connection": "new" if position == 0 else "reused"}
        point_id = request(connection, key, path, scroll_body(work_id, field),
                           record, cold=position == 0) or point_id
        records.append(record)
    connection.close()
    if point_id:
        connection = http.client.HTTPSConnection(url.hostname, port, timeout=12)
        record = {"work_id": work_id, "field": "poetry_locators",
                  "kind": "retrieve_by_id", "connection": "new"}
        body = {"ids": [point_id], "with_vector": False,
                "with_payload": ["work_id", "generation", "poetry_api_schema", "poetry_locators"]}
        request(connection, key, path.removesuffix("/scroll"), body, record, cold=True)
        records.append(record)
        connection.close()
    return records


def main():
    url, port, key = credentials()
    records = []
    for work_id in WORKS:
        records.extend(measure_work(url, port, key, work_id))
    print(json.dumps({"at_utc": datetime.now(timezone.utc).isoformat(),
                      "vantage": "RN", "read_only": True, "mutation_requests": 0,
                      "model_requests": 0, "collection": COLLECTION,
                      "planned_requests": 10, "completed_requests": len(records),
                      "endpoint_logged": False, "credentials_logged": False,
                      "records": records}, indent=2), flush=True)


if __name__ == "__main__":
    main()
