"""Validate sample full works, then stop only the two temporary test services."""

import http.client
import json
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
NAMES = ["poetry-rn-benchmark-api", "poetry-rn-benchmark-qdrant"]


def command(args, timeout=40):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def get(port, path):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=12)
    try:
        client.request("GET", path)
        response = client.getresponse()
        payload = json.loads(response.read())
        return {"status": response.status, "payload": payload}
    finally:
        client.close()


def full_work_checks():
    result = []
    for filename in ("api-benchmark.json", "cn-c2-c4.json"):
        data = json.loads((ROOT / filename).read_text())
        rows = [row for row in data["raw_requests"] if row["success"]]
        if not rows:
            continue
        for index in sorted({0, len(rows) // 2, len(rows) - 1}):
            work = rows[index]["response"]["poem"]
            detail = get(18000, work["detail_url"])
            payload = detail["payload"]
            valid = detail["status"] == 200 and bool(payload.get("original"))
            valid = valid and bool(payload.get("translation"))
            valid = valid and len(payload["original"]) == len(payload["translation"])
            result.append({"source": filename, "work_id": work["id"],
                           "valid": valid, "detail": detail})
    return result


def main():
    destination = ROOT / "finish.json"
    if destination.exists():
        raise RuntimeError("Preserving previous final evidence")
    result = {"unix_time": time.time(), "stops": {}}
    try:
        result["api_health_before_stop"] = get(18000, "/health")
        result["collection_before_stop"] = get(
            16333, "/collections/poetry_sentences_20260921_v1"
        )
        result["full_work_checks"] = full_work_checks()
    except Exception as error:
        result["validation_error"] = str(error)
    finally:
        for name in NAMES:
            result["stops"][name] = command(["docker", "stop", "--time", "30", name])
    result["states"] = {
        name: command(["docker", "inspect", "--format", "{{json .State}}", name])
        for name in NAMES
    }
    result["container_statuses"] = command(
        ["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}}"]
    )
    result["retained_bytes"] = command(["du", "-sb", str(ROOT)])
    result["disk_usage"] = dict(zip(("total", "used", "free"), shutil.disk_usage(ROOT)))
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"validated_full_works": len(result.get("full_work_checks", [])),
                      "all_valid": all(row["valid"] for row in result.get("full_work_checks", [])),
                      "validation_error": result.get("validation_error"),
                      "stopped": {name: row["code"] == 0 for name, row in result["stops"].items()},
                      "free_bytes": result["disk_usage"]["free"]}), flush=True)


if __name__ == "__main__":
    main()
