"""Read-only snapshot and optimization evidence; never mutate Cloud storage."""

from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "qdrant_api"))
from support import PATH, atomic_json, cloud_client


def inspect(client, path):
    try:
        return {"success": True, "result": client.request("GET", path, missing=True)}
    except (ValueError, RuntimeError, OSError) as error:
        return {"success": False, "error": str(error)}


def main():
    client = cloud_client()
    try:
        document = {"at_utc": datetime.now(timezone.utc).isoformat(),
                    "read_only": True, "mutation_requests": 0,
                    "collection": inspect(client, PATH),
                    "collection_snapshots": inspect(client, PATH + "/snapshots"),
                    "full_snapshots": inspect(client, "/snapshots"),
                    "optimizations": inspect(client, PATH + "/optimizations"),
                    "telemetry": inspect(client, "/telemetry?details_level=3")}
        path = Path(__file__).with_name("snapshot-inspection.json")
        atomic_json(path, document)
        for name in ("collection_snapshots", "full_snapshots", "optimizations"):
            print(name, document[name])
        info = document["collection"].get("result") or {}
        print("collection", {key: info.get(key) for key in (
            "status", "optimizer_status", "points_count", "indexed_vectors_count", "segments_count")})
        print("evidence", path)
    finally:
        client.close()


if __name__ == "__main__":
    main()
