"""Create a retained compressed archive and 64 MiB native-upload pieces."""

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEST = ROOT / "transfer"


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def main():
    free = shutil.disk_usage(ROOT).free
    if free < 22 * 1024 ** 3:
        raise RuntimeError("Insufficient space for retained archive plus pieces")
    DEST.mkdir(exist_ok=False)
    archive = DEST / "storage.tar.gz"
    started = time.monotonic()
    subprocess.run([
        "tar", "--no-xattrs", "-czf", str(archive), "-C", str(ROOT / "storage"), "."
    ], env={**os.environ, "COPYFILE_DISABLE": "1"}, check=True)
    compressed_seconds = time.monotonic() - started
    subprocess.run([
        "split", "-b", "64m", "-a", "4", str(archive), str(DEST / "part-")
    ], check=True)
    pieces = [
        {"name": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}
        for path in sorted(DEST.glob("part-*"))
    ]
    manifest = {
        "archive": str(archive), "archive_bytes": archive.stat().st_size,
        "archive_sha256": digest(archive), "compression_seconds": compressed_seconds,
        "total_seconds": time.monotonic() - started, "free_before_bytes": free,
        "free_after_bytes": shutil.disk_usage(ROOT).free, "pieces": pieces,
    }
    (DEST / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({key: value for key, value in manifest.items() if key != "pieces"}),
          flush=True)
    print(json.dumps({"piece_count": len(pieces)}), flush=True)


if __name__ == "__main__":
    main()
