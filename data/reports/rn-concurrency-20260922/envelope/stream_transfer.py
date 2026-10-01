"""Generate immutable gzip-1 chunks and upload through ssh-skill only."""

import concurrent.futures
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEST = ROOT / "transfer-fast"
REMOTE = "/opt/poetry-rn-benchmark-20260922/archive-parts"
SKILL = "/Users/axin/.codex/skills/ssh-skill/scripts"


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def upload_file(path):
    started = time.monotonic()
    args = [
        "uv", "run", "--no-project", "--with", "paramiko", "python",
        f"{SKILL}/ssh_upload.py", "rn", str(path), f"{REMOTE}/{path.name}", "--no-progress",
    ]
    attempts = []
    for attempt in range(3):
        result = subprocess.run(args, capture_output=True, text=True, timeout=600,
                                env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        attempts.append({"exit": result.returncode, "stdout": result.stdout,
                         "stderr": result.stderr})
        if result.returncode == 0:
            break
        time.sleep(2 + attempt * 3)
    return {
        "name": path.name, "bytes": path.stat().st_size, "sha256": digest(path),
        "seconds": time.monotonic() - started, "success": result.returncode == 0,
        "attempts": attempts,
    }


def start_pipeline():
    partial = ROOT / "transfer/storage.tar.gz"
    if partial.exists():
        retained = partial.with_suffix(".gz.level6-partial")
        if retained.exists():
            raise RuntimeError("Partial-archive retention path already exists")
        partial.rename(retained)
    DEST.mkdir(exist_ok=False)
    log = (DEST / "archive-errors.log").open("wb")
    tar = subprocess.Popen([
        "tar", "--no-xattrs", "-cf", "-", "-C", str(ROOT / "storage"), "."
    ], stdout=subprocess.PIPE, stderr=log, env={**os.environ, "COPYFILE_DISABLE": "1"})
    gzip = subprocess.Popen(["gzip", "-1"], stdin=tar.stdout,
                            stdout=subprocess.PIPE, stderr=log)
    tar.stdout.close()
    split = subprocess.Popen(["split", "-b", "64m", "-a", "4", "-", str(DEST / "part-")],
                             stdin=gzip.stdout, stderr=log)
    gzip.stdout.close()
    return [tar, gzip, split], log


def save_status(started, results, submitted, complete):
    success = [item for item in results if item["success"]]
    status = {
        "at": datetime.now(timezone.utc).isoformat(), "elapsed": time.monotonic() - started,
        "compression_complete": complete, "pieces_submitted": len(submitted),
        "pieces_finished": len(results), "pieces_successful": len(success),
        "bytes_uploaded": sum(item["bytes"] for item in success),
        "archive_bytes_so_far": sum(path.stat().st_size for path in DEST.glob("part-*")),
    }
    (DEST / "status.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status), flush=True)


def pump_uploads(processes, started):
    submitted, results, pending = set(), [], {}
    next_report = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        while True:
            complete = processes[-1].poll() is not None
            paths = sorted(DEST.glob("part-*"))
            ready = paths if complete else paths[:-1]
            for path in ready:
                if path.name not in submitted:
                    pending[pool.submit(upload_file, path)] = path.name
                    submitted.add(path.name)
            for future in list(pending):
                if future.done():
                    results.append(future.result())
                    del pending[future]
                    (DEST / "upload-results.json").write_text(json.dumps(results, indent=2))
            if time.monotonic() >= next_report:
                save_status(started, results, submitted, complete)
                next_report = time.monotonic() + 30
            if complete and not pending:
                break
            time.sleep(2)
    return sorted(results, key=lambda item: item["name"])


def finish_manifest(results, started, process_codes):
    archive_hash = hashlib.sha256()
    for result in results:
        with (DEST / result["name"]).open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                archive_hash.update(block)
    manifest = {
        "at": datetime.now(timezone.utc).isoformat(), "seconds": time.monotonic() - started,
        "archive_bytes": sum(item["bytes"] for item in results),
        "archive_sha256": archive_hash.hexdigest(), "process_exit_codes": process_codes,
        "success": all(item["success"] for item in results) and process_codes == [0, 0, 0],
        "pieces": results,
    }
    path = DEST / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    checksums = DEST / "checksums.sha256"
    checksums.write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in results))
    extra = [upload_file(path), upload_file(checksums)]
    (DEST / "metadata-upload.json").write_text(json.dumps(extra, indent=2) + "\n")
    print(json.dumps({key: value for key, value in manifest.items() if key != "pieces"}),
          flush=True)


def main():
    started = time.monotonic()
    processes, log = start_pipeline()
    results = pump_uploads(processes, started)
    codes = [process.wait() for process in processes]
    log.close()
    finish_manifest(results, started, codes)


if __name__ == "__main__":
    main()
