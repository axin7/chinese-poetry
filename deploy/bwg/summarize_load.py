"""Join bounded API load measurements with read-only host resource samples."""

import argparse
import datetime
import json
import pathlib


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def resource_summary(samples):
    if not samples:
        return {"available": False}
    if any(not row["api"]["memory_bytes"] or not row["api"]["pids"] for row in samples):
        raise ValueError("API resource evidence is incomplete")
    first, last = samples[0], samples[-1]
    seconds = timestamp(last["utc"]) - timestamp(first["utc"])
    summary = {
        "available": True,
        "samples": len(samples),
        "seconds": seconds,
        "host_available_min_mib": min(row["host_available_kib"] for row in samples) / 1024,
        "swap_in_pages": last["vmstat"]["pswpin"] - first["vmstat"]["pswpin"],
        "swap_out_pages": last["vmstat"]["pswpout"] - first["vmstat"]["pswpout"],
    }
    for name in ("api", "caddy"):
        begin, end = first[name], last[name]
        summary[name] = {
            "memory_peak_mib": max(row[name]["memory_bytes"] for row in samples) / 1048576,
            "cpu_percent_of_one_core": (
                (end["cpu"]["usage_usec"] - begin["cpu"]["usage_usec"]) / seconds / 10000
                if seconds else None
            ),
            "throttled_periods": end["cpu"]["nr_throttled"] - begin["cpu"]["nr_throttled"],
            "oom_kills": end["memory_events"]["oom_kill"] - begin["memory_events"]["oom_kill"],
        }
    return summary


def summarize(path, metrics):
    data = json.loads(path.read_text())
    result = {key: data[key] for key in ("base_url", "workload", "origin_ip", "method")}
    stages = []
    for original in data["stages"]:
        stage = {key: value for key, value in original.items() if key != "rows"}
        started = timestamp(stage["utc"])
        ended = started + stage["wall_seconds"]
        samples = [row for row in metrics if started <= timestamp(row["utc"]) <= ended]
        stage["resources"] = resource_summary(samples)
        stage["zero_observed_errors"] = stage["error_rate"] == 0 and stage["requests"] > 0
        stages.append(stage)
    result["stages"] = stages
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=pathlib.Path, required=True)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("inputs", type=pathlib.Path, nargs="+")
    args = parser.parse_args()
    metrics = [json.loads(line) for line in args.metrics.read_text().splitlines() if line]
    runs = {path.stem: summarize(path, metrics) for path in args.inputs}
    summary = {"resources": resource_summary(metrics), "runs": runs}
    summary["requests"] = sum(
        stage["requests"] for run in runs.values() for stage in run["stages"]
    )
    summary["passed"] = sum(stage["passed"] for run in runs.values() for stage in run["stages"])
    with args.report.open("x") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=True)
        handle.write("\n")
    print(json.dumps({key: summary[key] for key in ("requests", "passed", "resources")}))


if __name__ == "__main__":
    main()
