"""Read minimal Qdrant resource telemetry during an externally driven load stage."""

import argparse
import datetime as dt
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values

from cloudflare_worker_metrics import REPORT_DIR, utc_time
from qdrant_cloud.common import atomic_json, endpoint


COLLECTIONS = ("poetry_tang_20260922_v1", "poetry_tang_api_20260930_v1")
MEMORY_FIELDS = (
    "active_bytes", "allocated_bytes", "metadata_bytes", "resident_bytes", "retained_bytes",
)
HEALTH_FIELDS = (
    "status", "optimizer_status", "points_count", "indexed_vectors_count", "segments_count",
)


def credentials():
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    url, key = values.get("QDRANT_CLUSTER_ENDPOINT"), values.get("QDRANT_API_KEY")
    if not url or not key:
        raise ValueError("Private Qdrant credentials are unavailable")
    endpoint(url, cloud=True)
    return url.rstrip("/"), key


def get_metadata(url, key, path):
    request = urllib.request.Request(url + path, headers={"api-key": key})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            document = json.load(response)
        if document.get("status") != "ok" or not isinstance(document.get("result"), dict):
            raise ValueError("Unexpected Qdrant metadata envelope")
    except urllib.error.HTTPError as error:
        return {"available": False, "http_status": error.code}
    except (ValueError, OSError):
        return {"available": False, "error": "Metadata unavailable within sample window"}
    return {
        "available": True, "duration_ms": (time.monotonic() - started) * 1000,
        "result": document["result"],
    }


def resources(url, key):
    response = get_metadata(url, key, "/telemetry?details_level=1")
    if not response["available"]:
        return response
    result = response["result"]
    memory = result.get("memory", {})
    return {
        "available": True, "duration_ms": response["duration_ms"],
        "qdrant_version": result.get("app", {}).get("version"),
        "cpu_cores_used": result.get("app", {}).get("system", {}).get("cpu_cores_used"),
        "memory": {name: memory.get(name) for name in MEMORY_FIELDS},
    }


def collection_health(url, key):
    health = {}
    for collection in COLLECTIONS:
        response = get_metadata(url, key, "/collections/" + collection)
        if not response["available"]:
            health[collection] = response
            continue
        data = response["result"]
        optimizer = data.get("optimizer_status")
        status = "ok" if optimizer == "ok" else "not_ok"
        health[collection] = {
            name: status if name == "optimizer_status" else data.get(name)
            for name in HEALTH_FIELDS
        }
    return health


def sample_stage(url, key, start):
    samples = []
    for index in range(7):
        planned = start + dt.timedelta(seconds=index * 10)
        delay = planned.timestamp() - time.time()
        if delay > 0:
            time.sleep(delay)
        actual = dt.datetime.now(dt.timezone.utc)
        sample = {
            "planned_at_utc": planned.isoformat(), "actual_at_utc": actual.isoformat(),
            "actual_offset_seconds": (actual - start).total_seconds(),
            "resources": resources(url, key),
        }
        if index in (0, 6):
            sample["collection_health"] = collection_health(url, key)
        samples.append(sample)
        print(json.dumps({"sample_index": index, **sample}), flush=True)
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-start", required=True)
    parser.add_argument("--filename", default="qdrant-vector64-resources.json")
    args = parser.parse_args()
    start = utc_time(args.stage_start)
    if not -10 <= time.time() - start.timestamp() <= 15:
        raise ValueError("Sampler must start within 15 seconds of the stage notice")
    if Path(args.filename).name != args.filename or not args.filename.endswith(".json"):
        raise ValueError("Report filename must be a plain JSON filename")
    url, key = credentials()
    document = {
        "read_only": True, "mutation_requests": 0, "vector_searches": 0,
        "credentials_and_endpoint_redacted": True, "planned_cadence_seconds": 10,
        "stage_start_utc": start.isoformat(), "stage_duration_seconds": 60,
        "advertised_cpu_capacity_vcpu": 0.5,
        "cpu_measurement": "Qdrant process cores averaged over roughly the last 2 seconds",
        "cpu_source": (
            "https://github.com/qdrant/qdrant/blob/v1.19.1/"
            "src/common/telemetry_ops/app_telemetry.rs"
        ),
        "samples": sample_stage(url, key, start),
        "limitations": [
            "CPU values cover roughly 2 seconds per sample, leaving gaps between samples.",
            "Actual sample times and request durations can drift from planned targets.",
            "Advertised 0.5 vCPU is not independently verified as an enforced limit.",
            "CPU saturation requires aligned latency and throughput evidence.",
            "Memory bytes describe allocator/process telemetry, not account RAM quota.",
        ],
    }
    target = REPORT_DIR / args.filename
    atomic_json(target, document)
    print(json.dumps({"report": str(target), "samples": len(document["samples"])}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
