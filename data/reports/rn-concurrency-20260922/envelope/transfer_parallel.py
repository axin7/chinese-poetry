"""Bounded four-flow native upload comparison through ssh-skill only."""

import concurrent.futures
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "native-transfer-sample.bin"


def upload(index):
    remote = f"/opt/poetry-rn-benchmark-20260922/native-transfer-sample-{index}.bin"
    args = [
        "uv", "run", "--no-project", "--with", "paramiko", "python",
        "/Users/axin/.codex/skills/ssh-skill/scripts/ssh_upload.py",
        "rn", str(SAMPLE), remote, "--no-progress",
    ]
    started = time.monotonic()
    result = subprocess.run(
        args, capture_output=True, text=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"}, timeout=300,
    )
    elapsed = time.monotonic() - started
    return {
        "index": index, "destination": remote, "elapsed_seconds": elapsed,
        "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
    }


def main():
    if SAMPLE.stat().st_size != 64 * 1024 * 1024:
        raise RuntimeError("Expected existing 64 MiB sample")
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(upload, range(1, 5)))
    elapsed = time.monotonic() - started
    succeeded = sum(result["exit_code"] == 0 for result in results)
    report = {
        "at": datetime.now(timezone.utc).isoformat(), "elapsed_seconds": elapsed,
        "successful_uploads": succeeded, "aggregate_mib_per_second": succeeded * 64 / elapsed,
        "results": results,
    }
    (ROOT / "native-transfer-parallel-result.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
