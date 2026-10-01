"""Prepare and explicitly publish an isolated payload-only Qdrant API collection."""

import argparse
import json
from pathlib import Path
import sys

from documents_plan import prepare
from documents_publish import publish, verify
from support import checkpoint_lock, cloud_client


def options():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "publish", "verify"))
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--canonical-db", type=Path)
    parser.add_argument("--canonical-report", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.action == "plan" and (args.canonical_db is None or args.canonical_report is None):
        parser.error("plan requires --canonical-db and --canonical-report")
    if args.action != "plan" and args.checkpoint is None:
        parser.error("publish/verify require --checkpoint")
    if args.action == "publish" and not args.apply:
        parser.error("publish requires --apply after reviewing the local plan")
    if args.checkpoint is not None and args.checkpoint.resolve() in {
            (args.plan_dir / filename).resolve() for filename in (
                "documents.jsonl", "plan.json", "rollback.json", "verification.json")}:
        parser.error("checkpoint cannot replace document plan artifacts")
    return args


def main():
    args = options()
    if args.action == "plan":
        result = prepare(args.canonical_db, args.canonical_report, args.plan_dir)
    else:
        client = cloud_client()
        try:
            with checkpoint_lock(args.checkpoint):
                operation = publish if args.action == "publish" else verify
                kwargs = {"explicit": args.apply} if args.action == "publish" else {}
                result = operation(client, args.plan_dir, args.checkpoint, log=True, **kwargs)
        finally:
            client.close()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(json.dumps({"success": False, "error": str(error)}), file=sys.stderr)
        sys.exit(1)
