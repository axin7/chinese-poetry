"""Copy the existing RN Tang collection to Cloud, then verify before switching traffic."""

import argparse
import json
from pathlib import Path
import sys
import time

from common import atomic_json, checkpoint_lock, clients
from contract import (
    COLLECTION, GENERATION, PATH, TOTAL, config_fingerprint, count, creation_config, inventory,
    scroll, validate_config, validate_counts, validate_indexes, validate_point,
)
from verify import ready, verify


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "migrate", "verify"))
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--batch-delay", type=float, default=0.05)
    parser.add_argument("--index-timeout", type=int, default=3600)
    parser.add_argument("--samples-per-dataset", type=int, default=12)
    parser.add_argument("--allow-normalization-drift", action="store_true")
    result = parser.parse_args()
    if not 64 <= result.batch_size <= 256 or not 0 <= result.batch_delay <= 5:
        parser.error("batch-size must be 64..256 and batch-delay 0..5 seconds")
    if not 60 <= result.index_timeout <= 14400 or not 1 <= result.samples_per_dataset <= 32:
        parser.error("index-timeout must be 60..14400 and samples-per-dataset 1..32")
    if result.checkpoint.resolve() == result.report.resolve():
        parser.error("checkpoint and report must be different files")
    return result


def preflight(source, target):
    info = source.request("GET", PATH)
    validate_config(info)
    validate_indexes(info)
    if not ready(source):
        raise ValueError("Source must be healthy and fully indexed")
    return {"collection": COLLECTION, "generation": GENERATION,
            "counts": validate_counts(source),
            "source_version": source.request("GET", "/")["version"],
            "target_version": target.request("GET", "/")["version"],
            "creation_config": creation_config(info),
            "source_config_sha256": config_fingerprint(info)}


def binding(source, target, plan, baseline):
    return {"source_endpoint": source.url, "target_endpoint": target.url,
            "source_collection": COLLECTION, "target_collection": COLLECTION,
            "generation": GENERATION, "source_config_sha256": plan["source_config_sha256"],
            "baseline": baseline}


def load_state(path, expected, target):
    if path.exists():
        state = json.loads(path.read_text())
        if state.get("binding") != expected or state.get("version") != 1:
            raise ValueError("Checkpoint endpoints, corpus or configuration do not match")
        if type(state.get("copied")) is not int or not 0 <= state["copied"] <= TOTAL:
            raise ValueError("Checkpoint progress is invalid")
        return state
    if target.request("GET", PATH, missing=True) is not None:
        raise ValueError("Target collection exists without this migration checkpoint")
    state = {"version": 1, "binding": expected, "phase": "prepared",
             "copied": 0, "offset": None, "last_id": None}
    atomic_json(path, state)
    return state


def prepare_target(target, plan, state):
    info = target.request("GET", PATH, missing=True)
    if info is None:
        if state["phase"] != "prepared":
            raise ValueError("Previously created target disappeared; refusing an incomplete resume")
        target.request("PUT", PATH, plan["creation_config"])
        info = target.request("GET", PATH)
    validate_config(info)
    if count(target) < state["copied"]:
        raise ValueError("Checkpoint is ahead of the target; investigate missing points")
    for field in ("dataset", "generation"):
        schema = info.get("payload_schema", {}).get(field)
        if schema is not None and schema.get("data_type") != "keyword":
            raise ValueError("Target contains an incompatible payload index")
        if schema is None:
            target.request("PUT", PATH + "/index?wait=true", {
                "field_name": field, "field_schema": "keyword",
            })
    validate_indexes(target.request("GET", PATH))


def copy_batches(source, target, state, args):
    if state["phase"] in {"copied", "ready", "verified"}:
        return
    state["phase"] = "copying"
    atomic_json(args.checkpoint, state)
    while True:
        page = scroll(source, args.batch_size, state["offset"])
        previous = state["last_id"]
        for point in page["points"]:
            validate_point(point)
            if previous is not None and point["id"] <= previous:
                raise ValueError("Source scroll IDs are duplicated or out of order")
            previous = point["id"]
        if not page["points"] and page.get("next_page_offset") is not None:
            raise ValueError("Source scroll made no progress")
        if state["copied"] + len(page["points"]) > TOTAL:
            raise ValueError("Source grew during migration")
        if page["points"]:
            target.request("PUT", PATH + "/points?wait=true", {"points": page["points"]})
        state.update(copied=state["copied"] + len(page["points"]),
                     offset=page.get("next_page_offset"), last_id=previous)
        if state["offset"] is None:
            if state["copied"] != TOTAL:
                raise ValueError("Source ended before the expected point count")
            state["phase"] = "copied"
        atomic_json(args.checkpoint, state)
        if state["copied"] % 4096 < args.batch_size or state["phase"] == "copied":
            print(json.dumps({"phase": state["phase"], "copied": state["copied"]}), flush=True)
        if state["phase"] == "copied":
            return
        time.sleep(args.batch_delay)


def wait_index(target, timeout):
    deadline, next_log = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        if ready(target):
            return
        if time.monotonic() >= next_log:
            info = target.request("GET", PATH)
            print(json.dumps({"phase": "indexing", "status": info["status"],
                              "indexed_vectors_count": info.get("indexed_vectors_count")}),
                  flush=True)
            next_log = time.monotonic() + 60
        time.sleep(10)
    raise RuntimeError("Target indexing did not fully complete within the bounded timeout")


def execute(source, target, args):
    plan = preflight(source, target)
    if args.action == "plan":
        plan["target_collection_exists"] = target.request("GET", PATH, missing=True) is not None
        atomic_json(args.report, {"plan": plan, "model_calls": 0})
        return True
    baseline = inventory(source, args.batch_size)
    expected = binding(source, target, plan, baseline)
    if args.action == "migrate":
        state = load_state(args.checkpoint, expected, target)
        prepare_target(target, plan, state)
        copy_batches(source, target, state, args)
        wait_index(target, args.index_timeout)
        state["phase"] = "ready"
        atomic_json(args.checkpoint, state)
    else:
        if not args.checkpoint.exists():
            raise ValueError("Verification requires the migration checkpoint")
        state = load_state(args.checkpoint, expected, target)
    result = verification_result(source, target, args, baseline)
    atomic_json(args.report, {"plan": plan, "verification": result})
    if result["success"]:
        state["phase"] = "verified"
        atomic_json(args.checkpoint, state)
    return result["success"]


def verification_result(source, target, args, baseline):
    try:
        return verify(source, target, args.batch_size, args.samples_per_dataset,
                      baseline, args.allow_normalization_drift)
    except (ValueError, RuntimeError) as error:
        return {"success": False, "error": str(error), "model_calls": 0}


def main():
    args = options()
    source, target = clients()
    try:
        with checkpoint_lock(args.checkpoint):
            success = execute(source, target, args)
        print(json.dumps({"success": success, "action": args.action, "model_calls": 0}), flush=True)
        return 0 if success else 1
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(json.dumps({"success": False, "error": str(error)}), file=sys.stderr)
        sys.exit(1)
