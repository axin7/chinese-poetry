"""Prepare or explicitly apply reversible Qdrant-only Worker API payload fields."""

import argparse
import json
from pathlib import Path
import sys

from apply import apply, inspect_plan, verify
from prepare import prepare
from support import checkpoint_lock, cloud_client


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "preflight", "apply", "verify"))
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--canonical-db", type=Path)
    parser.add_argument("--canonical-report", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "prepare" and (args.canonical_db is None or args.canonical_report is None):
        parser.error("prepare requires --canonical-db and --canonical-report")
    if args.action == "apply" and (not args.apply or args.checkpoint is None):
        parser.error("apply requires --apply and a new --checkpoint")
    if args.checkpoint is not None and args.checkpoint.resolve() in {
            (args.plan_dir / name).resolve() for name in ("manifest.jsonl", "plan.json",
                                                        "rollback.json", "verification.json")}:
        parser.error("checkpoint cannot replace plan artifacts")
    return args


def main():
    args, client = options(), cloud_client()
    try:
        if args.action == "prepare":
            result = prepare(client, args.canonical_db, args.canonical_report,
                             args.plan_dir, log=True)
        elif args.action == "verify":
            result = verify(client, args.plan_dir, log=True)
        elif args.action == "preflight":
            result = inspect_plan(client, args.plan_dir, args.checkpoint, log=True)
        else:
            with checkpoint_lock(args.checkpoint):
                result = apply(client, args.plan_dir, args.checkpoint, args.apply, log=True)
        visible = {key: value for key, value in result.items()
                   if key not in {"target_endpoint", "collection_config"}}
        print(json.dumps(visible, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(json.dumps({"success": False, "error": str(error)}), file=sys.stderr)
        sys.exit(1)
