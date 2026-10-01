"""Copy two immutable datasets and their existing vectors into a new collection."""

import argparse
import hashlib
import http.client
import json
import sqlite3
import struct
import time
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[2]
CORPUS = PROJECT / "data/corpus/poetry-20260921-v1"
SOURCE = "poetry_sentences_20260921_v1"
TARGET = "poetry_tang_20260922_v1"
GENERATION = "poetry-20260921-v1"
COUNTS = {"yudingquantangshi": 125500, "tangshisanbaishou": 1531}
CLIENT = http.client.HTTPConnection("127.0.0.1", 6333, timeout=120)


def request(method, path, payload=None):
    data = None if payload is None else json.dumps(payload, separators=(",", ":"))
    headers = {"Content-Type": "application/json"}
    try:
        CLIENT.request(method, path, data, headers)
        response = CLIENT.getresponse()
    except http.client.RemoteDisconnected:
        CLIENT.close()
        if method != "GET":
            raise
        CLIENT.request(method, path, data, headers)
        response = CLIENT.getresponse()
    body = json.loads(response.read())
    if response.status >= 300:
        raise RuntimeError(f"Qdrant {response.status}: {body}")
    return body["result"]


def save(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare_corpus():
    destination = ROOT / "corpus"
    destination.mkdir(exist_ok=False)
    source = sqlite3.connect(f"file:{CORPUS / 'chinese_poetry.db'}?mode=ro", uri=True)
    target = sqlite3.connect(destination / "chinese_poetry.db")
    manifest = {}
    for name in COUNTS:
        schema = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()[0]
        target.execute(schema)
        rows = source.execute(f'SELECT * FROM "{name}" ORDER BY id').fetchall()
        target.executemany(
            f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in rows[0])})', rows
        )
        copied = target.execute(f'SELECT * FROM "{name}" ORDER BY id').fetchall()
        if rows != copied:
            raise RuntimeError(f"SQLite rows differ for {name}")
        manifest[name] = {"rows": len(rows), "rows_equal": True}
    target.commit()
    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")
    target.close()
    source.close()
    config = json.loads((CORPUS / "datas.json").read_text())
    config["datasets"] = {name: config["datasets"][name] for name in COUNTS}
    (destination / "datas.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
    save("corpus-verification.json", manifest)


def create_collection():
    existing = request("GET", "/collections")["collections"]
    if any(item["name"] == TARGET for item in existing):
        raise RuntimeError("Target exists; refusing to overwrite an existing collection")
    config = request("GET", f"/collections/{SOURCE}")["config"]
    payload = {
        "vectors": config["params"]["vectors"], "shard_number": 1,
        "on_disk_payload": True, "hnsw_config": config["hnsw_config"],
        "quantization_config": config["quantization_config"],
        "optimizers_config": {"indexing_threshold": 0, "default_segment_number": 2},
    }
    request("PUT", f"/collections/{TARGET}", payload)
    for field in ("dataset", "generation"):
        request("PUT", f"/collections/{TARGET}/index?wait=true",
                {"field_name": field, "field_schema": "keyword"})


def pages(collection, filtered=False):
    offset = None
    while True:
        payload = {"limit": 256, "with_vector": True, "with_payload": True}
        if offset is not None:
            payload["offset"] = offset
        if filtered:
            payload["filter"] = {"must": [
                {"key": "dataset", "match": {"any": list(COUNTS)}},
                {"key": "generation", "match": {"value": GENERATION}},
            ]}
        result = request("POST", f"/collections/{collection}/points/scroll", payload)
        yield result["points"]
        offset = result.get("next_page_offset")
        if offset is None:
            break


def fingerprint(digest, point):
    if len(point["vector"]) != 1024:
        raise RuntimeError("Unexpected vector dimension")
    payload = point["payload"]
    if payload["dataset"] not in COUNTS or payload["generation"] != GENERATION:
        raise RuntimeError("Unexpected dataset or generation")
    digest.update(str(point["id"]).encode())
    digest.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    digest.update(struct.pack("<1024f", *point["vector"]))


def copy_vectors():
    digest = hashlib.sha256()
    counts = Counter()
    started = time.monotonic()
    for points in pages(SOURCE, filtered=True):
        for point in points:
            fingerprint(digest, point)
            counts[point["payload"]["dataset"]] += 1
        request("PUT", f"/collections/{TARGET}/points?wait=true", {"points": points})
        if sum(counts.values()) % 8192 == 0:
            print(json.dumps({"copied": sum(counts.values()),
                              "seconds": round(time.monotonic() - started, 1)}), flush=True)
    if dict(counts) != COUNTS:
        raise RuntimeError(f"Wrong subset counts: {counts}")
    return digest.hexdigest(), dict(counts)


def verify_vectors(expected):
    digest = hashlib.sha256()
    count = 0
    for points in pages(TARGET):
        for point in points:
            fingerprint(digest, point)
            count += 1
    if digest.hexdigest() != expected or count != sum(COUNTS.values()):
        raise RuntimeError("Destination vector/payload/ID fingerprint differs")
    return {"count": count, "sha256": digest.hexdigest(), "identical_to_source": True}


def finish_index():
    request("PATCH", f"/collections/{TARGET}",
            {"optimizers_config": {"indexing_threshold": 1000}})
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        info = request("GET", f"/collections/{TARGET}")
        summary = {key: info.get(key) for key in
                   ("status", "points_count", "indexed_vectors_count", "segments_count")}
        print(json.dumps(summary), flush=True)
        if info["status"] == "green" and info["indexed_vectors_count"] == sum(COUNTS.values()):
            save("collection-ready.json", info)
            return
        time.sleep(10)
    raise RuntimeError("Index did not become fully ready")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    ROOT.mkdir(exist_ok=True)
    if args.finalize:
        verification = json.loads((ROOT / "vector-verification.json").read_text())
        counts = COUNTS
    else:
        create_collection()
        prepare_corpus()
        digest, counts = copy_vectors()
        verification = verify_vectors(digest)
        save("vector-verification.json", verification)
    finish_index()
    snapshot = request("POST", f"/collections/{TARGET}/snapshots?wait=true")
    manifest = {"source_collection": SOURCE, "collection": TARGET,
                "generation": GENERATION, "datasets": counts,
                "vectors": verification, "snapshot": snapshot, "embedding_calls": 0}
    save("subset-manifest.json", manifest)
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    main()
