"""Bounded closed-loop benchmark of the existing local full-corpus search API."""

import concurrent.futures
import datetime
import http.client
import itertools
import json
import pathlib
import statistics
import subprocess
import threading
import time


ROOT = pathlib.Path(__file__).resolve().parent
STOP = threading.Event()
LOCK = threading.Lock()
ROWS = []
STATS = []
START = time.monotonic()
SUBJECTS = [
    "homesickness", "friendship", "spring flowers", "a quiet mountain village",
    "falling autumn leaves", "a boat on the river", "a lonely traveler", "moonlight",
    "a distant loved one", "returning home", "farewell", "a snowy landscape",
    "a summer breeze", "a scholar reading", "passing time", "a peaceful garden",
    "birds singing", "harvest", "ancient ruins", "a distant mountain",
]
CONTEXTS = [
    "at sunrise", "after the rain", "before a long journey", "at dusk",
    "during a sleepless night", "near an old bridge", "in early spring",
    "while remembering childhood", "beside a flowing stream", "in winter",
    "while listening to the wind", "after meeting an old friend", "far from family",
    "beside a bamboo grove", "under a clear sky", "during a quiet evening",
    "in a small courtyard", "beyond the city walls", "during a festival",
    "after many years away",
]
QUERIES = [
    f"Find a classical poem about {subject} {context}, with vivid imagery and emotion."
    for subject, context in itertools.product(SUBJECTS, CONTEXTS)
]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def json_get(path):
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=12)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        payload = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(f"GET {path} returned {response.status}")
        return payload
    finally:
        connection.close()


def request_one(connection, query, concurrency, index):
    started = time.monotonic()
    row = {"index": index, "concurrency": concurrency, "query": query,
           "started_s": started - START, "started_utc": utc_now()}
    try:
        body = json.dumps({"query": query}).encode()
        connection.request("POST", "/search", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
        match = payload.get("match", {})
        valid = bool(match.get("original") and match.get("translation"))
        row.update(status=response.status, success=response.status == 200 and valid)
        row["response"] = payload
        if response.status in (401, 402, 403, 429):
            STOP.set()
    except Exception as error:
        row.update(status=0, success=False, error=f"{type(error).__name__}: {error}")
        connection.close()
    row["latency_s"] = time.monotonic() - started
    row["finished_s"] = time.monotonic() - START
    with LOCK:
        ROWS.append(row)
    return row


def worker(deadline, concurrency, tasks):
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=12)
    try:
        while time.monotonic() < deadline and not STOP.is_set():
            with LOCK:
                task = next(tasks, None)
            if task is None:
                return
            index, query = task
            request_one(connection, query, concurrency, index)
    finally:
        connection.close()


def stats_worker(done):
    while not done.is_set():
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}",
             "poetry-qdrant", "poetry-vector-api"],
            capture_output=True, text=True, timeout=15,
        )
        STATS.append({"at_utc": utc_now(), "elapsed_s": time.monotonic() - START,
                      "containers": [json.loads(line) for line in result.stdout.splitlines()]})
        done.wait(1)


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(rows, started, stopped, concurrency):
    success = [row for row in rows if row["success"]]
    latencies = [row["latency_s"] for row in success]
    elapsed = stopped - started
    return {
        "concurrency": concurrency, "requests": len(rows), "successes": len(success),
        "errors": len(rows) - len(success), "wall_seconds_including_drain": elapsed,
        "success_qps": len(success) / elapsed,
        "all_request_qps": len(rows) / elapsed,
        "success_latency_p50_s": percentile(latencies, 0.5),
        "success_latency_p95_s": percentile(latencies, 0.95),
        "success_latency_max_s": max(latencies, default=None),
        "success_latency_mean_s": statistics.mean(latencies) if latencies else None,
        "all_latency_p95_s": percentile([row["latency_s"] for row in rows], 0.95),
        "status_counts": {str(status): sum(r["status"] == status for r in rows)
                          for status in sorted({row["status"] for row in rows})},
    }


def metadata():
    result = subprocess.run(
        ["docker", "inspect", "poetry-vector-api"],
        capture_output=True, text=True, check=True,
    )
    inspected = json.loads(result.stdout)[0]
    allowed = {"EMBEDDING_CONCURRENCY", "EMBEDDING_TIMEOUT", "RERANK_ENABLED",
               "RERANK_CONCURRENCY", "RERANK_TIMEOUT", "SEARCH_CONCURRENCY",
               "RESPONSE_CACHE_BYTES", "VECTOR_CACHE_BYTES", "QDRANT_HNSW_EF",
               "SILICONFLOW_EMBED_MODEL", "CORPUS_GENERATION", "COLLECTION_NAME"}
    environment = dict(item.split("=", 1) for item in inspected["Config"]["Env"])
    info = subprocess.run(
        ["docker", "info", "--format", "{{json .}}"],
        capture_output=True, text=True, check=True,
    )
    docker = json.loads(info.stdout)
    return {
        "host_scope": "existing local Docker Desktop; NOT rn server",
        "api_image": inspected["Image"],
        "safe_runtime_environment": {key: environment[key] for key in sorted(allowed)
                                     if key in environment},
        "docker_vm_cpus": docker["NCPU"], "docker_vm_memory_bytes": docker["MemTotal"],
        "api_health_before": json_get("/health"), "started_utc": utc_now(),
        "method": "closed-loop unique uncached query text, default rerank, keep-alive",
        "stage_admission_seconds": 20, "stage_request_cap": 60,
        "planned_concurrency": [1, 2, 4, 8, 16],
    }


def save(data):
    data["raw_requests"] = sorted(ROWS, key=lambda row: row["started_s"])
    data["docker_stats"] = STATS
    (ROOT / "benchmark.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def main():
    data = metadata()
    data["stages"] = []
    done = threading.Event()
    monitor = threading.Thread(target=stats_worker, args=(done,), daemon=True)
    monitor.start()
    try:
        for stage, concurrency in enumerate(data["planned_concurrency"]):
            if STOP.is_set():
                break
            before = len(ROWS)
            tasks = iter(enumerate(QUERIES[stage * 60:(stage + 1) * 60], stage * 60))
            started = time.monotonic()
            deadline = started + data["stage_admission_seconds"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                jobs = [pool.submit(worker, deadline, concurrency, tasks)
                        for _ in range(concurrency)]
                for job in jobs:
                    job.result()
            summary = summarize(ROWS[before:], started, time.monotonic(), concurrency)
            data["stages"].append(summary)
            save(data)
            print(json.dumps(summary), flush=True)
            if summary["successes"] == 0:
                STOP.set()
            time.sleep(2)
    finally:
        done.set()
        monitor.join(timeout=20)
        data["ended_utc"] = utc_now()
        data["stopped_early"] = STOP.is_set()
        data["api_health_after"] = json_get("/health")
        save(data)


if __name__ == "__main__":
    main()
