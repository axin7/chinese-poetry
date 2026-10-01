"""Record origin setup operations; all remote access goes through ssh-skill."""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SKILL = "/Users/axin/.codex/skills/ssh-skill/scripts"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["exec", "upload", "download"])
    parser.add_argument("evidence_name")
    parser.add_argument("--script")
    parser.add_argument("--command")
    parser.add_argument("--local")
    parser.add_argument("--remote")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    common = ["uv", "run", "--no-project", "--with", "paramiko", "python"]
    if args.operation == "exec":
        command = Path(args.script).read_text() if args.script else args.command
        call = common + [f"{SKILL}/ssh_execute.py", "rn", command,
                         "--timeout", str(args.timeout)]
    else:
        paths = [args.local, args.remote] if args.operation == "upload" else [
            args.remote, args.local
        ]
        call = common + [f"{SKILL}/ssh_{args.operation}.py", "rn", *paths, "--no-progress"]
    result = subprocess.run(call, capture_output=True, text=True,
                            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
                            timeout=args.timeout + 30)
    report = {"at": datetime.now(timezone.utc).isoformat(), "operation": args.operation,
              "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    (ROOT / f"{args.evidence_name}.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
