"""Run the bounded metadata diagnostic through ssh-skill and save safe evidence."""

from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess


def main():
    root = Path(__file__).resolve().parent
    remote_script = root / "document-latency-remote.py"
    command = "python3 -c " + shlex.quote(remote_script.read_text())
    skill = "/Users/axin/.claude/skills/ssh-skill/scripts/ssh_execute.py"
    operation = ["uv", "run", "--no-project", "--with", "paramiko", "python", skill,
                 "rn", command, "--timeout", "150"]
    result = subprocess.run(operation, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise RuntimeError("Read-only SSH diagnostic failed; private output suppressed")
    envelope = json.loads(result.stdout)
    if not envelope.get("success") or envelope.get("exit_code"):
        raise RuntimeError("Remote diagnostic failed; private output suppressed")
    document = json.loads(envelope["stdout"])
    document["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
    output = root / "document-latency-rn.json"
    with output.open("x") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    print(json.dumps(document, indent=2))


if __name__ == "__main__":
    main()
