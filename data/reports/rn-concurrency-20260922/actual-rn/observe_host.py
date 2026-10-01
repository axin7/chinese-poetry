"""Record RN host pressure and stop only the isolated test on unsafe pressure."""

import argparse
import json
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONTAINERS = ["poetry-rn-benchmark-qdrant", "poetry-rn-benchmark-api"]


def command(args, timeout=10):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def numbers(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        parts = line.replace(":", "").split()
        if len(parts) > 1 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def cgroups():
    results = {}
    for name in CONTAINERS:
        meta = command(["docker", "inspect", "--format", "{{.Id}} {{.State.Pid}}", name])
        if meta["code"]:
            continue
        identifier, pid = meta["stdout"].strip().split()
        data = {"pid": int(pid)}
        if pid == "0":
            results[name] = data
            continue
        unified = Path(f"/sys/fs/cgroup/system.slice/docker-{identifier}.scope")
        if not unified.exists():
            unified = Path(f"/sys/fs/cgroup/docker/{identifier}")
        files = ["memory.current", "memory.peak", "memory.swap.current",
                 "memory.events", "memory.stat", "cpu.stat", "io.stat"]
        for filename in files:
            path = unified / filename
            if path.exists():
                data[filename] = path.read_text()
        legacy = Path(f"/sys/fs/cgroup/memory/docker/{identifier}")
        for filename in ["memory.usage_in_bytes", "memory.memsw.usage_in_bytes",
                         "memory.max_usage_in_bytes", "memory.failcnt", "memory.stat"]:
            path = legacy / filename
            if path.exists():
                data[filename] = path.read_text()
        results[name] = data
    return results


def sample():
    pressures = {}
    for key in ("cpu", "memory", "io"):
        path = Path("/proc/pressure") / key
        if path.exists():
            pressures[key] = path.read_text()
    return {"unix_time": time.time(), "meminfo_kib": numbers("/proc/meminfo"),
            "vmstat": numbers("/proc/vmstat"), "pressure": pressures,
            "proc_stat": Path("/proc/stat").read_text(),
            "diskstats": Path("/proc/diskstats").read_text(), "containers": cgroups()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    path = ROOT / f"{args.name}-host.jsonl"
    deadline = time.monotonic() + args.seconds
    consecutive = 0
    with path.open("x") as handle:
        while time.monotonic() < deadline:
            row = sample()
            mem = row["meminfo_kib"]
            unsafe = mem["MemAvailable"] < 64 * 1024 or (
                mem["MemAvailable"] < 128 * 1024 and mem["SwapFree"] < 128 * 1024
            )
            consecutive = consecutive + 1 if unsafe else 0
            if consecutive >= 2:
                row["safety_stop"] = [command(["docker", "stop", "--time", "5", name])
                                      for name in reversed(CONTAINERS)]
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            if consecutive >= 2:
                print("Stopped isolated benchmark containers due to host pressure.", flush=True)
                return
            time.sleep(2)
    print(f"Saved host observations to {path.name}", flush=True)


if __name__ == "__main__":
    main()
