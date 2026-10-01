"""Prepare a new release using existing RN credentials without displaying them."""

import json
import os
import shlex
import shutil
from pathlib import Path


ROOT = Path("/opt/poetry-tang-20260922")
PREVIOUS = Path("/opt/poetry-rn-benchmark-20260922")


def main():
    target = ROOT / ".env"
    if target.exists():
        raise RuntimeError("Refusing to overwrite existing credentials")
    values = {}
    for line in (PREVIOUS / "private.env").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        tokens = shlex.split(value, comments=True)
        values[key.strip()] = tokens[0] if tokens else ""
    key = values.get("SILICONFLOW_KEY") or values.get("SILICONFLOW_API_KEY")
    if not key or any(char in key for char in "\n\r'$"):
        raise RuntimeError("Provider key unavailable or unsupported env syntax")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(f"SILICONFLOW_API_KEY='{key}'\n")
    binary = ROOT / "bin/vector-api"
    if binary.exists():
        raise RuntimeError("Refusing to overwrite API binary")
    shutil.copy2(PREVIOUS / "vector-api", binary)
    binary.chmod(0o755)
    print(json.dumps({"credentials_prepared": True, "credentials_mode": "0600",
                      "api_binary_copied": True, "credentials_printed": False}))


if __name__ == "__main__":
    main()
