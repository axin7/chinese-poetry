"""Install protected routes in the existing test vhost, with rollback on failure."""

import hashlib
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path


ROOT = Path("/opt/poetry-tang-20260922")
VHOST = Path("/opt/1panel/www/conf.d/rn-proxy-test.anyveo.com.conf")
AUTH = Path("/opt/1panel/www/sites/rn-proxy-test.anyveo.com/poetry-auth.conf")
EXPECTED = "9ca6f0eb75b06810916a27c597e9535cb44cda50c66b0a5d4d466a7513e6d341"
CONTAINER = "1Panel-openresty-i7Mo"


def private_write(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(content)


def nginx(*args):
    result = subprocess.run(["docker", "exec", CONTAINER, "nginx", *args],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("OpenResty validation/reload failed; vhost will be restored")


def main():
    original = VHOST.read_bytes()
    if hashlib.sha256(original).hexdigest() != EXPECTED:
        raise RuntimeError("Vhost differs from inspected version")
    backup = ROOT / "backups/rn-proxy-test.anyveo.com.conf"
    if backup.exists() or AUTH.exists() or (ROOT / "client-token").exists():
        raise RuntimeError("Refusing to replace existing backup or credentials")
    body = original.decode()
    marker = "    location / {\n        return 404;\n    }"
    position = body.rfind(marker)
    if position < body.index("listen 443 ssl;"):
        raise RuntimeError("HTTPS insertion point was not found")
    locations = (ROOT / "openresty-locations.conf").read_text()
    updated = body[:position] + locations + "\n" + body[position:]
    token = secrets.token_urlsafe(48)
    private_write(ROOT / "client-token", token + "\n")
    private_write(AUTH, f'set $poetry_tang_token "{token}";\n'
                  'if ($http_authorization != "Bearer $poetry_tang_token") { return 401; }\n')
    shutil.copy2(VHOST, backup)
    try:
        VHOST.write_text(updated)
        nginx("-t")
        nginx("-s", "reload")
    except Exception:
        shutil.copy2(backup, VHOST)
        nginx("-t")
        nginx("-s", "reload")
        raise
    print(json.dumps({"https_routes_installed": True, "auth_mode": "0600",
                      "backup": str(backup), "token_printed": False}))


if __name__ == "__main__":
    main()
