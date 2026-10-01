"""Summarize already-recorded host observations during Chinese query stages."""

import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def stage(concurrency, workload, samples):
    requests = [row for row in workload["raw_requests"] if row["concurrency"] == concurrency]
    start = min(epoch(row["started_utc"]) for row in requests)
    end = max(epoch(row["started_utc"]) + row["latency_s"] for row in requests)
    selected = [row for row in samples if start <= row["unix_time"] <= end]
    first, last = selected[0], selected[-1]
    cpu = [list(map(int, row["proc_stat"].splitlines()[0].split()[1:9]))
           for row in (first, last)]
    delta = [after - before for before, after in zip(cpu[0], cpu[1])]
    qdrant = [row["containers"]["poetry-rn-benchmark-qdrant"] for row in selected]
    return {"concurrency": concurrency, "samples": len(selected),
            "observed_seconds": last["unix_time"] - first["unix_time"],
            "iowait_percent": 100 * delta[4] / sum(delta),
            "qdrant_ram_max_mib": max(int(row["memory.current"]) for row in qdrant) / 2**20,
            "qdrant_swap_max_mib": max(int(row["memory.swap.current"])
                                       for row in qdrant) / 2**20,
            "host_mem_available_min_mib": min(row["meminfo_kib"]["MemAvailable"]
                                               for row in selected) / 1024,
            "host_swap_free_min_mib": min(row["meminfo_kib"]["SwapFree"]
                                           for row in selected) / 1024}


def main():
    results = []
    for filename, observer, levels in [
        ("cn-c2-c4.json", "cn-run-host.jsonl", (2, 4)),
        ("cn-c8.json", "cn8-host.jsonl", (8,)),
    ]:
        workload = json.loads((ROOT / filename).read_text())
        lines = (ROOT / "evidence" / observer).read_text().splitlines()
        samples = [json.loads(line) for line in lines]
        results.extend(stage(level, workload, samples) for level in levels)
    (ROOT / "chinese-resource-metrics.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
