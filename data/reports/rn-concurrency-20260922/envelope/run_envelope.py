"""Isolated full-corpus Qdrant resource-budget diagnostic, not an RN benchmark."""

import argparse
import concurrent.futures
import http.client
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[3] if len(ROOT.parents) > 3 else ROOT
COLLECTION = "poetry_sentences_20260921_v1"
GENERATION = "poetry-20260921-v1"
SNAPSHOT = (
    PROJECT / "data/snapshots" / COLLECTION
    / f"{COLLECTION}-829596450146656-2026-09-20-19-11-31.snapshot"
)
EXPECTED = 1717558
PORT = 16333
HOST = "127.0.0.1"
OBSERVE_CONTAINER = True


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def command(args, timeout=20):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def inspect(name):
    if not OBSERVE_CONTAINER:
        return {"observation_disabled": True}
    result = command(["docker", "inspect", name])
    return json.loads(result["stdout"])[0] if result["code"] == 0 else result


def resources(name):
    if not OBSERVE_CONTAINER:
        return {"observation_disabled": True}
    paths = ["memory.current", "memory.peak", "memory.swap.current", "memory.swap.peak"]
    paths += ["memory.events", "memory.stat", "cpu.stat"]
    script = "\n".join(f"echo {path}; cat /sys/fs/cgroup/{path}" for path in paths)
    result = command(["docker", "exec", name, "sh", "-c", script])
    return {"time": now(), **result}


def request(path, payload=None, timeout=10, conn=None):
    own = conn is None
    conn = conn or http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    body = None if payload is None else json.dumps(payload).encode()
    method = "GET" if payload is None else "POST"
    conn.request(method, path, body, {"Content-Type": "application/json"})
    response = conn.getresponse()
    raw = response.read()
    if own:
        conn.close()
    if response.status != 200:
        raise RuntimeError(f"HTTP {response.status}: {raw[:200]!r}")
    return json.loads(raw)


def compact_collection():
    info = request(f"/collections/{COLLECTION}")["result"]
    return {key: info[key] for key in (
        "status", "optimizer_status", "points_count", "indexed_vectors_count", "config"
    )}


def launch(args):
    storage = ROOT / "storage"
    storage.mkdir(exist_ok=True)
    cmd = ["docker", "run", "-d", "--name", args.name, "--restart=no"]
    cmd += ["--cpus=2", f"--memory={args.memory}m", f"--memory-swap={args.swap_total}m"]
    cmd += ["-p", f"127.0.0.1:{PORT}:6333"]
    cmd += ["--mount", f"type=bind,src={storage},dst=/qdrant/storage"]
    cmd += ["--env", "QDRANT__TELEMETRY_DISABLED=true"]
    if args.restore:
        cmd += ["--mount", f"type=bind,src={SNAPSHOT},dst=/input/corpus.snapshot,readonly"]
    cmd += ["--entrypoint", "/qdrant/qdrant", "qdrant/qdrant:v1.15.5"]
    if args.restore:
        cmd += ["--snapshot", f"/input/corpus.snapshot:{COLLECTION}"]
    started = time.monotonic()
    result = command(cmd)
    write_json(ROOT / f"{args.name}-launch.json", {"at": now(), "cmd": cmd, **result})
    if result["code"] != 0:
        raise RuntimeError(result)
    monitor_startup(args, started)


def monitor_startup(args, started):
    samples, ready, exact_count, last_error = [], False, None, None
    while time.monotonic() - started < args.deadline:
        meta = inspect(args.name)
        state = meta.get("State", {})
        if not state.get("Running"):
            break
        samples.append(resources(args.name))
        try:
            collection = compact_collection()
            if collection["status"] == "green":
                counted = request(f"/collections/{COLLECTION}/points/count", {"exact": True})
                exact_count = counted["result"]["count"]
                ready = exact_count == EXPECTED
                if ready:
                    break
        except Exception as exc:
            last_error = str(exc)
        time.sleep(3)
    result = {
        "at": now(), "elapsed_seconds": time.monotonic() - started,
        "ready": ready, "exact_count": exact_count, "last_error": last_error,
        "inspect": inspect(args.name), "samples": samples,
        "collection": compact_collection() if ready else None,
        "logs": command(["docker", "logs", "--tail", "100", args.name]),
    }
    write_json(ROOT / f"{args.name}-startup.json", result)
    print(json.dumps({key: result[key] for key in (
        "elapsed_seconds", "ready", "exact_count", "last_error"
    )}), flush=True)


def query_payload(vector):
    return {
        "query": vector,
        "filter": {"must": [{"key": "generation", "match": {"value": GENERATION}}]},
        "params": {"hnsw_ef": 64, "exact": False, "indexed_only": True,
                   "quantization": {"rescore": False}},
        "limit": 1,
        "with_payload": ["dataset", "source_row_id", "raw_index", "normalized_index",
                         "work_id", "generation"],
        "with_vector": False,
    }


def load_vectors(path):
    if path:
        raw = json.loads(Path(path).read_text())
        if isinstance(raw, dict):
            raw = raw.get("vectors", raw.get("data"))
        return [item.get("vector", item.get("embedding")) if isinstance(item, dict)
                else item for item in raw]
    sampled = request(f"/collections/{COLLECTION}/points/scroll", {
        "limit": 128, "with_payload": False, "with_vector": True,
    }, timeout=30)
    vectors = [point["vector"] for point in sampled["result"]["points"]]
    write_json(ROOT / "corpus_sample_vectors.json", vectors)
    return vectors


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * p))]


def run_worker(index, parallel, payloads, deadline, barrier):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
    latencies, errors, server_times = [], [], []
    barrier.wait()
    iteration = index
    while time.monotonic() < deadline:
        started = time.monotonic()
        try:
            result = request(f"/collections/{COLLECTION}/points/query",
                             payloads[iteration % len(payloads)], conn=conn)
            points = result["result"]["points"]
            if len(points) != 1 or points[0]["payload"]["generation"] != GENERATION:
                raise RuntimeError("Unexpected result shape or generation")
            latencies.append((time.monotonic() - started) * 1000)
            server_times.append(result.get("time", 0) * 1000)
        except Exception as exc:
            errors.append({"elapsed_ms": (time.monotonic() - started) * 1000,
                           "error": str(exc)})
            conn.close()
            conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
            time.sleep(0.1)
        iteration += parallel
    conn.close()
    return {"latencies_ms": latencies, "errors": errors, "server_ms": server_times}


def sample_loop(name, stop, samples):
    while not stop.is_set():
        samples.append(resources(name))
        stop.wait(3)


def run_phase(name, parallel, seconds, payloads):
    samples, stop = [], threading.Event()
    monitor = threading.Thread(target=sample_loop, args=(name, stop, samples))
    monitor.start()
    barrier = threading.Barrier(parallel + 1)
    started = time.monotonic()
    deadline = started + seconds
    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as executor:
        pending = [executor.submit(run_worker, i, parallel, payloads, deadline, barrier)
                   for i in range(parallel)]
        barrier.wait()
        results = [future.result() for future in pending]
    elapsed = time.monotonic() - started
    stop.set()
    monitor.join()
    latencies = [item for result in results for item in result["latencies_ms"]]
    errors = [item for result in results for item in result["errors"]]
    summary = {
        "concurrency": parallel, "duration_seconds": elapsed,
        "success": len(latencies), "errors": len(errors),
        "successful_qps": len(latencies) / elapsed,
        "p50_ms": percentile(latencies, 0.5), "p95_ms": percentile(latencies, 0.95),
        "p99_ms": percentile(latencies, 0.99), "max_ms": max(latencies, default=None),
        "error_details": errors[:20],
    }
    write_json(ROOT / f"{name}-c{parallel}.json", {
        "summary": summary, "samples": samples, "results": results,
    })
    print(json.dumps(summary), flush=True)
    return summary


def benchmark(args):
    vectors = load_vectors(args.vectors)
    if not vectors or any(len(vector) != 1024 for vector in vectors):
        raise ValueError("Expected nonempty 1024-dimensional vectors")
    payloads = [query_payload(vector) for vector in vectors]
    for payload in payloads[:16]:
        request(f"/collections/{COLLECTION}/points/query", payload, timeout=30)
    summaries = []
    for concurrency in [int(value) for value in args.levels.split(",")]:
        summary = run_phase(args.name, concurrency, args.seconds, payloads)
        summaries.append(summary)
        if summary["errors"] or (summary["p95_ms"] or 0) >= args.stop_p95_ms:
            break
    write_json(ROOT / f"{args.name}-benchmark.json", {
        "at": now(), "vector_source": args.vectors or "128 corpus stored vectors",
        "target_host": HOST, "target_port": PORT,
        "unique_vectors": len(vectors), "summaries": summaries,
        "final_inspect": inspect(args.name), "final_resources": resources(args.name),
    })


def stop(args):
    before = resources(args.name)
    result = command(["docker", "stop", "--time", "30", args.name], timeout=40)
    write_json(ROOT / f"{args.name}-stop.json", {
        "at": now(), "resources_before": before, "stop": result,
        "inspect": inspect(args.name),
        "logs": command(["docker", "logs", "--tail", "100", args.name]),
    })
    print(json.dumps(result), flush=True)


def main():
    global HOST, PORT, OBSERVE_CONTAINER
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "benchmark", "stop"])
    parser.add_argument("--name", required=True)
    parser.add_argument("--memory", type=int, default=1280)
    parser.add_argument("--swap-total", type=int, default=2304)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--deadline", type=int, default=240)
    parser.add_argument("--seconds", type=int, default=25)
    parser.add_argument("--levels", default="1,2,4,8")
    parser.add_argument("--stop-p95-ms", type=float, default=4000)
    parser.add_argument("--vectors")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=16333)
    parser.add_argument("--no-container-observations", action="store_true")
    args = parser.parse_args()
    if args.no_container_observations and args.action != "benchmark":
        parser.error("Container observations can only be disabled for remote benchmarks")
    HOST, PORT = args.host, args.port
    OBSERVE_CONTAINER = not args.no_container_observations
    {"start": launch, "benchmark": benchmark, "stop": stop}[args.action](args)


if __name__ == "__main__":
    main()
