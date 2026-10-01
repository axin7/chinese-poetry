"""Start and inspect only the isolated, loopback-only RN diagnostic services."""

import argparse
import http.client
import json
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
COLLECTION = "poetry_sentences_20260921_v1"
QDRANT = "poetry-rn-benchmark-qdrant"
API = "poetry-rn-benchmark-api"


def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return {"code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def state(name):
    result = command(["docker", "inspect", "--format", "{{json .State}}", name])
    return json.loads(result["stdout"]) if not result["code"] else result


def request(port, path, payload=None):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        client.request("GET" if payload is None else "POST", path,
                       None if payload is None else json.dumps(payload),
                       {"Content-Type": "application/json"})
        response = client.getresponse()
        result = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        return result
    finally:
        client.close()


def qdrant_command(args):
    return ["docker", "run", "-d", "--name", QDRANT, "--restart=no", "--cpus=1.75",
            f"--memory={args.memory}m", f"--memory-swap={args.total_memory}m",
            "-p", "127.0.0.1:16333:6333", "-p", "127.0.0.1:16334:6334",
            "--mount", f"type=bind,src={ROOT / 'storage'},dst=/qdrant/storage",
            "--env", "QDRANT__TELEMETRY_DISABLED=true", "qdrant/qdrant:v1.15.5"]


def api_command():
    cmd = ["docker", "run", "-d", "--name", API, "--restart=no", "--network=host",
           "--cpus=0.5", "--memory=96m", "--memory-swap=96m",
           "--env-file", str(ROOT / "private.env"), "--entrypoint", "/benchmark/vector-api"]
    for source, target in [("vector-api", "/benchmark/vector-api"),
                           ("chinese_poetry.db", "/data/chinese_poetry.db"),
                           ("datas.json", "/data/datas.json")]:
        cmd += ["--mount", f"type=bind,src={ROOT / source},dst={target},readonly"]
    return cmd + ["qdrant/qdrant:v1.15.5"]


def ready(kind):
    if kind == "api":
        payload = request(18000, "/health")
        return payload.get("status") == "ok", payload
    payload = request(16333, f"/collections/{COLLECTION}")["result"]
    if payload["status"] != "green":
        return False, payload
    count = request(16333, f"/collections/{COLLECTION}/points/count", {"exact": True})
    payload["exact_count"] = count["result"]["count"]
    return payload["exact_count"] == 1717558, payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["qdrant", "api"])
    parser.add_argument("--memory", type=int, default=1100)
    parser.add_argument("--total-memory", type=int, default=2124)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    destination = ROOT / f"{args.label}-startup.json"
    if destination.exists():
        raise RuntimeError("Preserving existing startup evidence; choose a new label")
    name = QDRANT if args.kind == "qdrant" else API
    cmd = qdrant_command(args) if args.kind == "qdrant" else api_command()
    result = {"unix_time": time.time(), "command": cmd, "launch": command(cmd)}
    result.update(ready=False, observations=[])
    started = time.monotonic()
    while not result["launch"]["code"] and time.monotonic() - started < 180:
        current = state(name)
        result["observations"].append({"elapsed": time.monotonic() - started,
                                       "state": current})
        if not current.get("Running"):
            break
        try:
            result["ready"], result["health"] = ready(args.kind)
            if result["ready"]:
                break
        except Exception as error:
            result["last_error"] = type(error).__name__ + ": " + str(error)
        time.sleep(3)
    result.update(elapsed_seconds=time.monotonic() - started, state=state(name))
    result["logs"] = command(["docker", "logs", "--tail", "50", name])
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("ready", "elapsed_seconds", "state")}),
          flush=True)
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
