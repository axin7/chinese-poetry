"""Read collection state; optionally restart optimizers with unchanged settings."""

import argparse
import datetime
import json
from pathlib import Path
import time

from support import PATH, atomic_json, cloud_client


def state(client):
    info = client.request("GET", PATH)
    return {"status": info["status"], "optimizer_status": info["optimizer_status"],
            "points_count": info["points_count"],
            "indexed_vectors_count": info["indexed_vectors_count"],
            "segments_count": info["segments_count"],
            "optimizer_config": info["config"]["optimizer_config"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--restart-optimizers", action="store_true")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("report exists")
    client = cloud_client()
    try:
        before = state(client)
        report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "before": before, "restart_requested": args.restart_optimizers,
                  "changes_to_vectors_or_payload": False, "samples": []}
        if args.restart_optimizers:
            threads = before["optimizer_config"]["max_optimization_threads"]
            result = client.request("PATCH", PATH, {
                "optimizers_config": {"max_optimization_threads": threads}})
            report["patch_result"] = result
            for _ in range(12):
                current = state(client)
                report["samples"].append(current)
                if current["status"] == "green" and current["optimizer_status"] == "ok":
                    break
                time.sleep(5)
        else:
            current = before
        report["config_preserved"] = current["optimizer_config"] == before["optimizer_config"]
        report["success"] = (current["status"] == "green"
                             and current["optimizer_status"] == "ok"
                             and current["points_count"] == 127031)
        atomic_json(args.report, report)
        print(json.dumps(report))
        return 0 if report["success"] else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
