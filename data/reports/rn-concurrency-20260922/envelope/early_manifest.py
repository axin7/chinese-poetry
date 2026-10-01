"""Hash the completed immutable gzip stream and upload its verification manifests."""

import hashlib
import json
from pathlib import Path

from stream_transfer import upload_file


def main():
    root = Path(__file__).resolve().parent / "transfer-fast"
    aggregate = hashlib.sha256()
    pieces = []
    for path in sorted(root.glob("part-*")):
        piece_hash = hashlib.sha256()
        with path.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                aggregate.update(block)
                piece_hash.update(block)
        pieces.append({"name": path.name, "bytes": path.stat().st_size,
                       "sha256": piece_hash.hexdigest()})
    manifest = {"archive_bytes": sum(item["bytes"] for item in pieces),
                "archive_sha256": aggregate.hexdigest(), "pieces": pieces}
    manifest_path = root / "archive_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    checksums = root / "checksums.sha256"
    checksums.write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in pieces))
    results = [upload_file(checksums), upload_file(manifest_path)]
    (root / "early-manifest-upload.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps({key: value for key, value in manifest.items() if key != "pieces"}))
    print(json.dumps({"uploads": results}), flush=True)


if __name__ == "__main__":
    main()
