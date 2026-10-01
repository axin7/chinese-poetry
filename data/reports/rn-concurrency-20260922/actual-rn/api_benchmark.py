"""Bounded RN full-chain search benchmark; Python standard library only.

Run gradually: python3 api_benchmark.py --concurrency 1 --seconds 25 --output c1.json
Then select --concurrency 2,4 or 8,16. Keep query_cursor.json between runs so text
queries never repeat. Any HTTP/content/client failure stops further admissions
and escalation. Successful p95 >= 4 seconds also prevents escalation by default.

API deployment: Linux amd64 vector-api, CA roots, read-only SQLite and datas.json.
HTTP_ADDR=127.0.0.1:18000
SQLITE_DB_PATH=<absolute frozen chinese_poetry.db path>
DATASETS_CONFIG_PATH=<absolute frozen datas.json path>
QDRANT_URL=http://127.0.0.1:<Qdrant gRPC port, normally 6334>
COLLECTION_NAME=poetry_sentences_20260921_v1
CORPUS_GENERATION=poetry-20260921-v1
VECTOR_DIM=1024
EMBEDDING_PROFILE=sf-bge-m3-1024-v1
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
SILICONFLOW_EMBED_MODEL=BAAI/bge-m3
EMBEDDING_CONCURRENCY=4
EMBEDDING_TIMEOUT=5
RERANK_ENABLED=false
QDRANT_HNSW_EF=64
SEARCH_CONCURRENCY=256
Inject SILICONFLOW_API_KEY securely into the API process; never into this runner.
SILICONFLOW_KEY is also supported and takes precedence if both names are present.
The binary uses modernc SQLite and requires no CGO/shared SQLite library.
For a container, mount SQLite/config read-only; mount Qdrant storage separately.
For separate containers use QDRANT_URL=http://<qdrant-service>:6334,
HTTP_ADDR=:8000, and publish the API as 127.0.0.1:18000:8000 only.
"""

import argparse
import concurrent.futures
import datetime
import fcntl
import http.client
import json
import pathlib
import platform
import statistics
import threading
import time
import urllib.parse


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
MOODS = [
    "quiet reflection", "a sense of hope", "gentle sadness", "peaceful acceptance",
    "fond memories", "renewed courage", "simple joy", "deep longing",
    "the beauty of nature", "the dignity of ordinary life",
]
PREFERENCES = [
    "Use vivid imagery", "Favor subtle emotion", "Choose a memorable setting",
    "Prefer a contemplative voice", "Emphasize the surrounding landscape",
    "Let the seasons shape the mood", "Look for a human connection",
    "Show the contrast of motion and stillness", "Focus on the passage of time",
    "Capture the feeling of the moment", "Find a scene with sensory detail",
    "Choose an expressive traditional passage",
]
QUERY_COUNT = len(SUBJECTS) * len(CONTEXTS) * len(MOODS) * len(PREFERENCES)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def query_for(index):
    choices = []
    for values in (SUBJECTS, CONTEXTS, MOODS, PREFERENCES):
        choices.append(values[index % len(values)])
        index //= len(values)
    subject, context, mood, preference = choices
    return (f"Find classical poetry about {subject} {context}, expressing {mood}. "
            f"{preference}.")


def reserve_queries(path, count):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        text = handle.read()
        start = json.loads(text)["next_query_index"] if text.strip() else 0
        if start + count > QUERY_COUNT:
            raise ValueError("Query corpus exhausted; do not reset cursor against a warm API")
        handle.seek(0)
        json.dump({"next_query_index": start + count}, handle)
        handle.truncate()
        handle.flush()
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return start


def arguments():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18000")
    parser.add_argument("--concurrency", default="1,2,4,8,16")
    parser.add_argument("--seconds", type=float, default=25)
    parser.add_argument("--max-requests", type=int, default=500)
    parser.add_argument("--stop-p95", type=float, default=4)
    parser.add_argument("--pause", type=float, default=3)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--state-file", type=pathlib.Path,
                        default=pathlib.Path(__file__).with_name("query_cursor.json"))
    args = parser.parse_args()
    args.levels = [int(value) for value in args.concurrency.split(",")]
    if not args.levels or any(value < 1 or value > 64 for value in args.levels):
        parser.error("concurrency must contain integers between 1 and 64")
    if not 0 < args.seconds <= 60 or not 1 <= args.max_requests <= 5000:
        parser.error("seconds must be in (0, 60], max-requests in [1, 5000]")
    if not 0 < args.stop_p95 <= 12 or not 0 <= args.pause <= 10:
        parser.error("stop-p95 must be in (0, 12], pause in [0, 10]")
    args.endpoint = urllib.parse.urlsplit(args.base_url)
    if args.endpoint.scheme != "http" or args.endpoint.hostname not in ("127.0.0.1", "localhost"):
        parser.error("Only loopback HTTP endpoints are allowed for this origin benchmark")
    if args.endpoint.username or args.endpoint.password or args.endpoint.query:
        parser.error("Credentials and query strings are not accepted")
    if args.output.exists():
        parser.error("Output already exists; choose a new filename to preserve evidence")
    return args


def connection(args):
    return http.client.HTTPConnection(args.endpoint.hostname, args.endpoint.port or 80, timeout=12)


def health(args):
    client = connection(args)
    try:
        client.request("GET", "/health")
        response = client.getresponse()
        payload = json.loads(response.read())
        return {"http_status": response.status, "payload": payload,
                "ok": response.status == 200 and payload.get("status") == "ok"}
    except Exception as error:
        return {"ok": False, "error": type(error).__name__}
    finally:
        client.close()


def content_valid(payload):
    match = payload.get("match", {})
    poem = payload.get("poem", {})
    return (isinstance(match, dict) and isinstance(poem, dict)
            and all(isinstance(match.get(key), str) and match[key].strip()
                    for key in ("original", "translation"))
            and isinstance(poem.get("id"), str) and bool(poem["id"])
            and isinstance(poem.get("detail_url"), str))


def request_one(client, index, concurrency, origin):
    query = query_for(index)
    started = time.monotonic()
    row = {"query_index": index, "query": query, "concurrency": concurrency,
           "started_utc": utc_now(), "started_s": started - origin}
    try:
        client.request("POST", "/search", json.dumps({"query": query}).encode(),
                       {"Content-Type": "application/json"})
        response = client.getresponse()
        payload = json.loads(response.read())
        valid = isinstance(payload, dict) and content_valid(payload)
        row.update(status=response.status, success=response.status == 200 and valid)
        if row["success"]:
            row["response"] = payload
        else:
            row["error"] = "HTTP error or invalid original/translation result"
    except Exception as error:
        row.update(status=0, success=False, error=type(error).__name__)
        client.close()
    row["latency_s"] = time.monotonic() - started
    return row


def worker(args, concurrency, tasks, deadline, state):
    client = connection(args)
    try:
        while time.monotonic() < deadline and not state["stop"].is_set():
            with state["lock"]:
                index = next(tasks, None)
            if index is None:
                break
            row = request_one(client, index, concurrency, state["origin"])
            with state["lock"]:
                state["rows"].append(row)
            if not row["success"] or row["latency_s"] >= 10:
                state["stop"].set()
    finally:
        client.close()


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summary(rows, elapsed, concurrency):
    successful = [row["latency_s"] for row in rows if row["success"]]
    return {
        "concurrency": concurrency, "requests": len(rows), "successes": len(successful),
        "errors": len(rows) - len(successful), "wall_seconds_including_drain": elapsed,
        "success_qps": len(successful) / elapsed,
        "success_latency_p50_s": percentile(successful, 0.5),
        "success_latency_p95_s": percentile(successful, 0.95),
        "success_latency_max_s": max(successful, default=None),
        "success_latency_mean_s": statistics.mean(successful) if successful else None,
        "all_latency_p95_s": percentile([row["latency_s"] for row in rows], 0.95),
        "status_counts": {str(status): sum(row["status"] == status for row in rows)
                          for status in sorted({row["status"] for row in rows})},
    }


def run_stage(args, concurrency, start_index):
    started = time.monotonic()
    state = {"rows": [], "stop": threading.Event(), "lock": threading.Lock(), "origin": started}
    tasks = iter(range(start_index, start_index + args.max_requests))
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        jobs = [pool.submit(worker, args, concurrency, tasks, started + args.seconds, state)
                for _ in range(concurrency)]
        for job in jobs:
            job.result()
    result = summary(state["rows"], time.monotonic() - started, concurrency)
    result["admission_target_seconds"] = args.seconds
    result["request_cap"] = args.max_requests
    result["stopped_admission_early"] = state["stop"].is_set()
    return result, sorted(state["rows"], key=lambda row: row["started_s"])


def save(args, data):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    args = arguments()
    data = {"started_utc": utc_now(), "hostname": platform.node(), "machine": platform.machine(),
            "base_url": args.base_url, "method": "closed-loop unique text full-chain search",
            "client_timeout_seconds": 12, "stop_p95_seconds": args.stop_p95,
            "stages": [], "raw_requests": [], "health_before": health(args)}
    if not data["health_before"]["ok"]:
        data["stop_reason"] = "Initial health check failed; no load sent"
        save(args, data)
        print(json.dumps({"stop_reason": data["stop_reason"]}), flush=True)
        return 2
    cursor = reserve_queries(args.state_file, args.max_requests * len(args.levels))
    for stage, concurrency in enumerate(args.levels):
        result, rows = run_stage(args, concurrency, cursor + stage * args.max_requests)
        data["stages"].append(result)
        data["raw_requests"].extend(rows)
        save(args, data)
        print(json.dumps(result), flush=True)
        if result["errors"] or result["stopped_admission_early"]:
            data["stop_reason"] = "Request failure or 10-second latency; escalation stopped"
            break
        p95 = result["success_latency_p95_s"]
        if p95 is None or p95 >= args.stop_p95:
            data["stop_reason"] = "Latency threshold reached; escalation stopped"
            break
        time.sleep(args.pause)
    data["health_after"] = health(args)
    data["ended_utc"] = utc_now()
    save(args, data)
    return 1 if data.get("stop_reason") or not data["health_after"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
