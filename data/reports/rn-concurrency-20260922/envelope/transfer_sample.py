"""Measure one skill-mediated native upload of a new 64 MiB matrix sample."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / (
    "storage/collections/poetry_sentences_20260921_v1/0/segments/"
    "94e3dda3-1e0d-433b-a581-d07b06b8a2d3/vector_storage/matrix.dat"
)
SAMPLE = ROOT / "native-transfer-sample.bin"
REMOTE = "/opt/poetry-rn-benchmark-20260922/native-transfer-sample.bin"


def main():
    size = 64 * 1024 * 1024
    with SOURCE.open("rb") as source, SAMPLE.open("xb") as target:
        data = source.read(size)
        if len(data) != size:
            raise RuntimeError("Matrix sample source was unexpectedly short")
        target.write(data)
    args = [
        "uv", "run", "--no-project", "--with", "paramiko", "python",
        "/Users/axin/.codex/skills/ssh-skill/scripts/ssh_upload.py",
        "rn", str(SAMPLE), REMOTE, "--no-progress",
    ]
    started = time.monotonic()
    result = subprocess.run(
        args, capture_output=True, text=True,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"}, timeout=240,
    )
    elapsed = time.monotonic() - started
    report = {
        "at": datetime.now(timezone.utc).isoformat(), "source": str(SOURCE),
        "sample": str(SAMPLE), "destination": REMOTE, "size_bytes": size,
        "elapsed_seconds": elapsed, "mib_per_second": 64 / elapsed,
        "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
    }
    (ROOT / "native-transfer-sample-result.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
