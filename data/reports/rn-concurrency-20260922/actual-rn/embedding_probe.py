"""Bounded RN-to-provider embedding probe; not a full API capacity test."""

import concurrent.futures
import datetime
import hashlib
import http.client
import json
import math
import pathlib
import shlex
import statistics
import threading
import time
import urllib.parse


ROOT = pathlib.Path("/opt/poetry-rn-benchmark-20260922")
OUTPUT = ROOT / "embedding_probe.json"
SUBJECTS = [
    "明月下思念故乡", "春雨过后的庭院", "送别即将远行的朋友", "秋日落叶中的惆怅",
    "雪后寂静的山林", "乘船欣赏江边风景", "读书时内心的平静", "重逢旧友的喜悦",
    "远离尘嚣的田园生活", "夕阳下独自归家的旅人", "花开时对生命的感悟", "风中竹林的清幽",
]
MOODS = ["希望有细腻的自然描写", "希望表达真挚而含蓄的情感", "希望展现宁静悠远的意境"]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def settings():
    allowed = {"SILICONFLOW_KEY", "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL",
               "SILICONFLOW_EMBED_MODEL"}
    values = {}
    for line in (ROOT / "private.env").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.removeprefix("export ").partition("=")
        if not separator or key.strip() not in allowed:
            continue
        tokens = shlex.split(value.strip(), comments=True)
        if len(tokens) > 1:
            raise ValueError("Invalid private configuration format")
        values[key.strip()] = tokens[0] if tokens else ""
    key = values.get("SILICONFLOW_KEY") or values.get("SILICONFLOW_API_KEY")
    if not key:
        raise ValueError("Embedding credential is missing")
    base = values.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")
    endpoint = urllib.parse.urlsplit(base.rstrip("/") + "/embeddings")
    if endpoint.scheme != "https" or endpoint.username or endpoint.password:
        raise ValueError("Expected a credential-free HTTPS provider endpoint")
    return key, endpoint, values.get("SILICONFLOW_EMBED_MODEL", "BAAI/bge-m3")


def valid_vector(payload):
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if len(rows) != 1 or rows[0].get("index") != 0:
        return False
    vector = rows[0].get("embedding")
    return (isinstance(vector, list) and len(vector) == 1024
            and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) for value in vector)
            and any(value != 0 for value in vector))


def request_one(client, config, index, concurrency):
    key, endpoint, model = config
    query = f"请找一句描写{SUBJECTS[index % 12]}的古诗，{MOODS[index // 12]}。"
    body = json.dumps({"model": model, "input": [query], "encoding_format": "float"}).encode()
    started = time.monotonic()
    row = {"index": index, "concurrency": concurrency, "started_utc": utc_now(),
           "query_sha256": hashlib.sha256(query.encode()).hexdigest()}
    try:
        client.request("POST", endpoint.path, body,
                       {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        response = client.getresponse()
        raw = response.read()
        row["status"] = response.status
        row["success"] = response.status == 200 and valid_vector(json.loads(raw))
        if not row["success"]:
            row["error"] = "HTTP error or invalid 1024-dimensional embedding"
    except Exception as error:
        row.update(status=row.get("status", 0), success=False, error=type(error).__name__)
        client.close()
    row["latency_s"] = time.monotonic() - started
    return row


def worker(config, concurrency, tasks, stop, lock, rows):
    endpoint = config[1]
    client = http.client.HTTPSConnection(endpoint.hostname, endpoint.port or 443, timeout=5)
    try:
        while not stop.is_set():
            with lock:
                index = next(tasks, None)
            if index is None:
                break
            row = request_one(client, config, index, concurrency)
            with lock:
                rows.append(row)
            if not row["success"]:
                stop.set()
    finally:
        client.close()


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def run_stage(config, stage, concurrency):
    rows = []
    started = time.monotonic()
    tasks = iter(range(stage * 12, (stage + 1) * 12))
    stop, lock = threading.Event(), threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        jobs = [pool.submit(worker, config, concurrency, tasks, stop, lock, rows)
                for _ in range(concurrency)]
        for job in jobs:
            job.result()
    elapsed = time.monotonic() - started
    latencies = [row["latency_s"] for row in rows if row["success"]]
    summary = {
        "concurrency": concurrency, "requests": len(rows), "successes": len(latencies),
        "errors": len(rows) - len(latencies), "wall_seconds": elapsed,
        "success_qps": len(latencies) / elapsed,
        "success_p50_s": percentile(latencies, 0.5),
        "success_p95_s": percentile(latencies, 0.95),
        "success_mean_s": statistics.mean(latencies) if latencies else None,
        "success_max_s": max(latencies, default=None),
        "status_counts": {str(status): sum(row["status"] == status for row in rows)
                          for status in sorted({row["status"] for row in rows})},
    }
    return summary, rows


def main():
    if OUTPUT.exists():
        raise ValueError("Output already exists; preserve the previous evidence")
    config = settings()
    result = {"scope": "actual RN to configured embedding provider only; NOT full API capacity",
              "started_utc": utc_now(), "timeout_seconds": 5, "model": config[2],
              "endpoint_host": config[1].hostname, "dimension": 1024,
              "max_requests": 36, "stages": [], "raw_requests": []}
    for stage, concurrency in enumerate((1, 2, 4)):
        summary, rows = run_stage(config, stage, concurrency)
        result["stages"].append(summary)
        result["raw_requests"].extend(rows)
        OUTPUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary), flush=True)
        if summary["errors"]:
            result["stop_reason"] = "Failure observed; no further escalation"
            break
        time.sleep(2)
    result["ended_utc"] = utc_now()
    OUTPUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
