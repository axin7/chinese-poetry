"""Bounded Tang-only Qdrant benchmark; no model or public network calls.

Reads distinct stored corpus vectors, then searches at 4/8/16 concurrency using
the deployed Go service's unfiltered search settings. Each stage has a 10-second
wall deadline and at most 300 queries. Results describe the local warmed index,
not real query embeddings, API/SQLite work, public networking, or sustained SLA.
"""

import argparse
import collections
import concurrent.futures
import datetime
import hashlib
import http.client
import json
import math
import pathlib
import random
import threading
import time


HOST, PORT = "127.0.0.1", 17333
COLLECTION = "poetry_tang_20260922_v1"
GENERATION = "poetry-20260921-v1"
EXPECTED_POINTS = 127031
DATASETS = {"yudingquantangshi", "tangshisanbaishou"}
PAYLOAD_FIELDS = ["dataset", "source_row_id", "raw_index", "normalized_index",
                  "work_id", "generation"]
FILTER = {"must": [{"key": "generation", "match": {"value": GENERATION}}]}
SETTINGS = {"filter": FILTER, "limit": 1, "with_payload": PAYLOAD_FIELDS,
            "with_vector": False, "params": {"hnsw_ef": 64, "exact": False,
            "indexed_only": True, "quantization": {"rescore": False}}}
BASE_PATH = "/collections/" + COLLECTION


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--requests-per-stage", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 10 or not 1 <= args.requests_per_stage <= 300:
        parser.error("seconds must be 1..10; requests-per-stage must be 1..300")
    if args.output.exists() and not args.overwrite and not args.dry_run:
        parser.error("Output exists; use a new path or explicitly use --overwrite")
    return args


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def connection(timeout=10):
    return http.client.HTTPConnection(HOST, PORT, timeout=timeout)


def request(client, method, path, body=None):
    started = time.monotonic()
    row = {"status": 0, "success": False}
    try:
        headers = {"Content-Type": "application/json"} if body else {}
        client.request(method, path, body=body, headers=headers)
        response = client.getresponse()
        row["status"] = response.status
        content = response.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            raise ValueError("Response exceeds size limit")
        payload = json.loads(content)
        row["payload"] = payload
        row["success"] = (response.status == 200 and isinstance(payload, dict)
                          and payload.get("status") == "ok")
        if not row["success"]:
            row["error"] = "HTTP or Qdrant envelope failure"
    except (OSError, ValueError, http.client.HTTPException) as error:
        row["error"] = type(error).__name__
        client.close()
    row["latency_s"] = time.monotonic() - started
    return row


def require_result(row):
    if not row["success"]:
        raise ValueError(row.get("error", "Qdrant request failed"))
    return row["payload"]["result"]


def collection_info():
    client = connection()
    try:
        info = require_result(request(client, "GET", BASE_PATH))
    finally:
        client.close()
    if info.get("status") != "green" or info.get("points_count") != EXPECTED_POINTS:
        raise ValueError("Collection must be green and contain exactly 127031 points")
    fields = ("status", "optimizer_status", "points_count", "indexed_vectors_count",
              "segments_count", "config")
    return {key: info.get(key) for key in fields}


def vector_sample(point):
    payload, vector = point.get("payload", {}), point.get("vector")
    if (payload.get("dataset") not in DATASETS or payload.get("generation") != GENERATION
            or not isinstance(vector, list) or len(vector) != 1024
            or any(type(value) not in (int, float) or not math.isfinite(value)
                   for value in vector)):
        raise ValueError("Sample has invalid dataset, generation, or vector")
    encoded = json.dumps(vector, separators=(",", ":"), allow_nan=False)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    body = json.dumps({**SETTINGS, "query": vector}, separators=(",", ":"),
                      allow_nan=False).encode()
    return {"point_id": point["id"], "dataset": payload["dataset"],
            "vector_sha256": fingerprint, "body": body}


def sample_vectors(count):
    client, samples, seen, offset = connection(), [], set(), None
    try:
        for _ in range(16):
            body = {"limit": 128, "with_vector": True,
                    "with_payload": ["dataset", "generation"], "filter": FILTER}
            if offset is not None:
                body["offset"] = offset
            result = require_result(request(client, "POST", BASE_PATH + "/points/scroll",
                                             json.dumps(body).encode()))
            for point in result["points"]:
                sample = vector_sample(point)
                if sample["vector_sha256"] not in seen:
                    seen.add(sample["vector_sha256"])
                    samples.append(sample)
                if len(samples) == count:
                    random.Random(20260922).shuffle(samples)
                    return samples
            offset = result.get("next_page_offset")
            if offset is None:
                break
    finally:
        client.close()
    raise ValueError("Not enough distinct vectors within the 2048-point sampling limit")


def validate_hit(row):
    if not row["success"]:
        return
    points = row["payload"].get("result", {}).get("points", [])
    if not isinstance(points, list) or len(points) != 1:
        row.update(success=False, error="Expected exactly one nearest result")
        return
    hit = points[0]
    payload = hit.get("payload", {})
    valid = (payload.get("dataset") in DATASETS and payload.get("generation") == GENERATION
             and isinstance(payload.get("work_id"), str)
             and payload["work_id"].startswith(payload["dataset"] + ":")
             and all(type(payload.get(key)) is int and payload[key] >= 0
                     for key in ("source_row_id", "raw_index", "normalized_index")))
    row.update(success=valid, result_point_id=hit.get("id"), score=hit.get("score"),
               dataset=payload.get("dataset"), server_seconds=row["payload"].get("time"))
    if not valid:
        row["error"] = "Returned locator or scope validation failed"


def query_one(client, sample):
    row = request(client, "POST", BASE_PATH + "/points/query", sample["body"])
    validate_hit(row)
    row.pop("payload", None)
    row.update(source_point_id=sample["point_id"], source_dataset=sample["dataset"],
               vector_sha256=sample["vector_sha256"])
    return row


def worker(tasks, deadline, state):
    client = connection()
    try:
        while not state["stop"].is_set():
            with state["lock"]:
                remaining = deadline - time.monotonic()
                sample = next(tasks, None) if remaining > 0 else None
            if sample is None:
                break
            client.timeout = min(5, remaining)
            if client.sock is not None:
                client.sock.settimeout(client.timeout)
            row = query_one(client, sample)
            with state["lock"]:
                state["rows"].append(row)
            if not row["success"]:
                state["stop"].set()
    finally:
        client.close()


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summary(rows, elapsed, concurrency):
    latencies = [row["latency_s"] for row in rows if row["success"]]
    server = [row["server_seconds"] for row in rows if row["success"]
              and isinstance(row.get("server_seconds"), (int, float))]
    return {"concurrency": concurrency, "requests": len(rows),
            "successes": len(latencies), "errors": len(rows) - len(latencies),
            "wall_seconds_including_drain": elapsed, "success_qps": len(latencies) / elapsed,
            "success_p50_s": percentile(latencies, 0.5),
            "success_p95_s": percentile(latencies, 0.95),
            "server_p50_s": percentile(server, 0.5), "server_p95_s": percentile(server, 0.95),
            "status_counts": dict(collections.Counter(str(row["status"]) for row in rows)),
            "error_counts": dict(collections.Counter(row["error"]
                for row in rows if "error" in row)),
            "result_dataset_counts": dict(collections.Counter(row["dataset"]
                for row in rows if row["success"]))}


def run_stage(args, samples, concurrency):
    started = time.monotonic()
    state = {"rows": [], "lock": threading.Lock(), "stop": threading.Event()}
    tasks, deadline = iter(samples), started + args.seconds
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        jobs = [pool.submit(worker, tasks, deadline, state) for _ in range(concurrency)]
        for job in jobs:
            job.result()
    result = summary(state["rows"], time.monotonic() - started, concurrency)
    result.update(request_cap=len(samples), wall_deadline_seconds=args.seconds,
                  stopped_on_failure=state["stop"].is_set())
    return result, state["rows"]


def save(report, result):
    report.seek(0)
    json.dump(result, report, ensure_ascii=False, indent=2)
    report.write("\n")
    report.truncate()
    report.flush()


def run(args, report, plan):
    result = {"started_utc": utc_now(), "plan": plan, "stages": [], "requests": []}
    result["collection_before"] = collection_info()
    samples = sample_vectors(3 * args.requests_per_stage)
    result["sample_dataset_counts"] = dict(collections.Counter(s["dataset"] for s in samples))
    result["sample_count"] = len(samples)
    save(report, result)
    for stage, concurrency in enumerate((4, 8, 16)):
        start, end = stage * args.requests_per_stage, (stage + 1) * args.requests_per_stage
        metrics, rows = run_stage(args, samples[start:end], concurrency)
        result["stages"].append(metrics)
        result["requests"].extend(rows)
        save(report, result)
        print(json.dumps(metrics), flush=True)
        if metrics["errors"] or not metrics["requests"]:
            result["stop_reason"] = "Failed or empty stage; escalation stopped"
            break
    result["collection_after"] = collection_info()
    result["success"] = "stop_reason" not in result
    result["ended_utc"] = utc_now()
    save(report, result)
    return 0 if result["success"] else 1


def main():
    args = arguments()
    plan = {"origin": f"http://{HOST}:{PORT}", "collection": COLLECTION,
            "query_settings": SETTINGS, "settings_source": "internal/qdrantstore/store.go",
            "concurrency": [4, 8, 16], "wall_seconds_per_stage": args.seconds,
            "max_search_requests": 3 * args.requests_per_stage, "max_scroll_requests": 16,
            "model_calls": 0, "sampling": "First up to 2048 IDs; distinct vectors; shuffled",
            "timing": "Bodies serialized before timing; loopback REST with keep-alive",
            "limits": ["Warm/page-cache state not controlled; not a cold-start measurement",
                       "Stored corpus vectors can be easier than natural query embeddings",
                       "Includes REST/Python overhead; deployed API uses gRPC",
                       "Excludes embedding provider, Go/SQLite, public network, and generation",
                       "Short bounded workload is not a sustained capacity or SLA claim"]}
    print(json.dumps(plan), flush=True)
    if args.dry_run:
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w" if args.overwrite else "x", encoding="utf-8") as report:
        return run(args, report, plan)


if __name__ == "__main__":
    raise SystemExit(main())
