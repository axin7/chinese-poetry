"""Record bounded RN operations using ssh-skill exclusively."""

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SKILL = Path("/Users/axin/.claude/skills/ssh-skill/scripts")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["exec", "upload", "download"])
    parser.add_argument("name")
    parser.add_argument("--command")
    parser.add_argument("--script")
    parser.add_argument("--local")
    parser.add_argument("--remote")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    evidence = ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    output = evidence / f"{args.name}.json"
    if output.exists():
        raise RuntimeError("Evidence name already exists")
    call = ["uv", "run", "--no-project", "--with", "paramiko", "python"]
    if args.operation == "exec":
        command = Path(args.script).read_text() if args.script else args.command
        call += [str(SKILL / "ssh_execute.py"), "rn", command,
                 "--timeout", str(args.timeout)]
    else:
        paths = [args.local, args.remote] if args.operation == "upload" else [
            args.remote, args.local
        ]
        call += [str(SKILL / f"ssh_{args.operation}.py"), "rn", *paths, "--no-progress"]
    result = subprocess.run(call, capture_output=True, text=True,
                            env={**os.environ, "MSYS_NO_PATHCONV": "1"},
                            timeout=args.timeout + 30)
    report = {"at": datetime.now(timezone.utc).isoformat(), "operation": args.operation,
              "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
