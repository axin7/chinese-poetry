"""Run bounded Worker vector load alongside read-only Qdrant telemetry."""

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import dotenv_values


BASE = "https://poetry-rust-lab-20260930.963600436.workers.dev/poetry"
ORIGIN = "cf-poetry-rust-qdrant-gateway-20260930"
COLLECTIONS = ("poetry_tang_20260922_v1", "poetry_tang_api_20260930_v1")
MEMORY_FIELDS = ("active_bytes", "allocated_bytes", "resident_bytes", "retained_bytes")


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def credentials(path):
    values = dotenv_values(path)
    url = next((values.get(key) for key in (
        "QDRANT_REST_URL", "QDRANT_CLUSTER_ENDPOINT", "QDRANT_URL",
    ) if values.get(key)), None)
    key = values.get("QDRANT_API_KEY")
    if not url or not key:
        raise ValueError("Private Qdrant credentials unavailable")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ValueError("Qdrant credentials require a valid HTTPS endpoint")
    if parsed.port == 6334:
        parsed = parsed._replace(netloc=parsed.hostname + ":6333")
    return urllib.parse.urlunsplit(parsed).rstrip("/"), key


def metadata(url, key, path):
    started = time.monotonic()
    request = urllib.request.Request(url + path, headers={"api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            data = json.load(response)
        if data.get("status") != "ok" or not isinstance(data.get("result"), dict):
            return {"available": False, "error": "unexpected_metadata"}
        return {"available": True, "result": data["result"],
                "duration_ms": (time.monotonic() - started) * 1000}
    except urllib.error.HTTPError as error:
        return {"available": False, "http_status": error.code}
    except (ValueError, OSError):
        return {"available": False, "error": "metadata_unavailable"}


def resources(url, key):
    started = utc()
    response = metadata(url, key, "/telemetry?details_level=1")
    row = {"started_at_utc": started, "completed_at_utc": utc()}
    if not response["available"]:
        return {**row, **response}
    data = response["result"]
    return {
        **row, "available": True, "duration_ms": response["duration_ms"],
        "cpu_cores_used": data.get("app", {}).get("system", {}).get("cpu_cores_used"),
        "memory": {name: data.get("memory", {}).get(name) for name in MEMORY_FIELDS},
    }


def health(url, key):
    result = {}
    for collection in COLLECTIONS:
        response = metadata(url, key, "/collections/" + collection)
        if not response["available"]:
            result[collection] = response
            continue
        data = response["result"]
        result[collection] = {name: data.get(name) for name in (
            "status", "points_count", "indexed_vectors_count", "segments_count",
        )}
        result[collection]["optimizer_status"] = (
            "ok" if data.get("optimizer_status") == "ok" else "not_ok"
        )
    return {"collected_at_utc": utc(), "collections": result}


def load_command(args):
    return [
        str(args.binary), "--base-url", BASE, "--token-file", str(args.token_file),
        "--expect-origin", ORIGIN, "--workload", "vector", "--concurrency", "64",
        "--duration", "10s", "--rest", "0s", "--max-requests", "900",
        "--report", str(args.load_report),
    ]


def run_load(args, url, key):
    samples = [resources(url, key)]
    with args.log.open("x") as output:
        process = subprocess.Popen(load_command(args), stdout=output, stderr=output)
        deadline = time.monotonic() + 40
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Owned load process exceeded its bounded window")
                samples.append(resources(url, key))
                time.sleep(2)
            return_code = process.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    samples.append(resources(url, key))
    if return_code != 0:
        raise RuntimeError("Load process failed; inspect its redacted log")
    return samples


def summary(load, samples):
    stage = load["stages"][0]
    start = dt.datetime.fromisoformat(stage["utc"].replace("Z", "+00:00"))
    end = start + dt.timedelta(seconds=stage["wall_seconds"])
    overlap = [row for row in samples if row["available"] and
               start <= dt.datetime.fromisoformat(row["completed_at_utc"]) <= end]
    cpu = [row["cpu_cores_used"] for row in overlap
           if isinstance(row["cpu_cores_used"], (float, int))]
    return {
        "load_start_utc": start.isoformat(), "load_end_utc": end.isoformat(),
        "concurrency": stage["concurrency"], "requests": stage["requests"],
        "passed": stage["passed"], "success_rps": stage["success_rps"],
        "wall_seconds": stage["wall_seconds"], "p95_seconds": stage["p95_seconds"],
        "overlapping_samples": len(overlap), "overlapping_cpu_samples": len(cpu),
        "cpu_cores_used_min": min(cpu) if cpu else None,
        "cpu_cores_used_max": max(cpu) if cpu else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--load-report", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.report, args.load_report, args.log):
        if path.exists():
            raise ValueError("Output files must be new")
    url, key = credentials(args.env_file)
    before = health(url, key)
    samples = run_load(args, url, key)
    load = json.loads(args.load_report.read_text())
    document = {
        "collected_at_utc": utc(), "credentials_and_endpoint_redacted": True,
        "qdrant_mutations": 0, "query_embeddings": 0, "request_budget": 900,
        "planned_sample_cadence_seconds": 2,
        "cpu_measurement": "Qdrant process cores averaged over roughly the last 2 seconds",
        "health_before": before, "health_after": health(url, key),
        "samples": samples, "summary": summary(load, samples),
        "limitations": [
            "Telemetry requests and sampling gaps can affect CPU measurements.",
            "Advertised 0.5 vCPU is not independently verified as an enforced limit.",
            "A short closed-loop probe does not establish a sustainable capacity limit.",
        ],
    }
    with args.report.open("x") as handle:
        json.dump(document, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps(document["summary"]))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError):
        raise SystemExit("Probe failed; private endpoint and credentials were redacted") from None
