"""Check exact deployed scope and collect resource facts without reading secrets."""

import http.client
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path("/opt/poetry-tang-20260922")
COLLECTION = "poetry_tang_20260922_v1"
COUNTS = {"yudingquantangshi": 125500, "tangshisanbaishou": 1531}


def request(path, payload=None):
    client = http.client.HTTPConnection("127.0.0.1", 17333, timeout=30)
    try:
        client.request("POST" if payload is not None else "GET", path,
                       json.dumps(payload) if payload is not None else None,
                       {"Content-Type": "application/json"})
        response = client.getresponse()
        result = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(f"Qdrant status {response.status}")
        return result["result"]
    finally:
        client.close()


def check_scope():
    path = f"/collections/{COLLECTION}"
    info = request(path)
    counts = {}
    for dataset, expected in COUNTS.items():
        payload = {"exact": True, "filter": {"must": [
            {"key": "dataset", "match": {"value": dataset}},
            {"key": "generation", "match": {"value": "poetry-20260921-v1"}},
        ]}}
        counts[dataset] = request(path + "/points/count", payload)["count"]
        if counts[dataset] != expected:
            raise RuntimeError("Dataset count differs")
    if info["points_count"] != sum(COUNTS.values()) or info["status"] != "green":
        raise RuntimeError("Collection is incomplete or unhealthy")
    if info["indexed_vectors_count"] != sum(COUNTS.values()):
        raise RuntimeError("Indexing is incomplete")
    return {"counts": counts, "collection": info}


def check_sqlite():
    database = sqlite3.connect(f"file:{ROOT / 'corpus/chinese_poetry.db'}?mode=ro", uri=True)
    tables = {row[0] for row in database.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if tables != set(COUNTS):
        raise RuntimeError("Unexpected SQLite tables")
    integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError("SQLite integrity check failed")
    rows = {table: database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in sorted(tables)}
    database.close()
    return {"integrity": integrity, "rows": rows}


def resource_facts():
    result = {}
    for service in ("qdrant", "vector-api"):
        name = f"poetry-tang-{service}-1"
        container = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
        result[service] = {"state": container["State"], "mounts": container["Mounts"],
                           "restart_count": container["RestartCount"]}
        pid = container["State"]["Pid"]
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                group = Path("/sys/fs/cgroup") / line[3:].lstrip("/")
                result[service]["cgroup"] = {file: (group / file).read_text().strip()
                    for file in ("memory.current", "memory.swap.current", "memory.events",
                                 "memory.stat", "cpu.stat", "io.stat") if (group / file).exists()}
    result["meminfo"] = Path("/proc/meminfo").read_text()
    result["disk_usage"] = subprocess.check_output(
        ["du", "-sh", str(ROOT / "storage"), str(ROOT / "snapshots"), str(ROOT / "corpus")],
        text=True)
    return result


def main():
    name = sys.argv[1]
    if not name.replace("-", "").isalnum():
        raise ValueError("Invalid report name")
    report = {"scope": check_scope(), "sqlite": check_sqlite(), "resources": resource_facts()}
    with (ROOT / "evidence" / (name + ".json")).open("x") as handle:
        json.dump(report, handle, indent=2)
    print(json.dumps({"counts": report["scope"]["counts"], "sqlite": report["sqlite"],
                      "disk_usage": report["resources"]["disk_usage"], "success": True}))


if __name__ == "__main__":
    main()
