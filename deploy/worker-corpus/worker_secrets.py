"""Copy authorized upstream credentials into an isolated Cloudflare Worker."""

import argparse
import io
import json
import os
import pathlib
import shlex
import subprocess
import sys
import urllib.parse

from dotenv import dotenv_values


def remote_read(alias, path):
    script = pathlib.Path.home() / ".claude/skills/ssh-skill/scripts/ssh_execute.py"
    result = subprocess.run(
        [sys.executable, str(script), alias, "cat " + shlex.quote(path), "--timeout", "30"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("Private SSH credential read failed")
    value = json.loads(result.stdout)
    if not value.get("success"):
        raise RuntimeError("Private SSH credential read failed")
    return value["stdout"]


def credentials(args):
    local = dotenv_values(args.local_env)
    remote = dotenv_values(stream=io.StringIO(remote_read(args.alias, args.remote_env)))
    token = remote_read(args.alias, args.remote_token).strip()
    endpoint = local.get("QDRANT_CLUSTER_ENDPOINT") or remote.get("QDRANT_CLUSTER_ENDPOINT")
    parsed = urllib.parse.urlsplit(endpoint or "")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise RuntimeError("Qdrant must have a credential-free HTTPS endpoint")
    port = 6333 if parsed.port == 6334 else parsed.port
    host = parsed.hostname + (":" + str(port) if port else "")
    values = {
        "CLIENT_TOKEN": token,
        "QDRANT_REST_URL": urllib.parse.urlunsplit(("https", host, parsed.path.rstrip("/"),
                                                   "", "")),
        "QDRANT_API_KEY": local.get("QDRANT_API_KEY") or remote.get("QDRANT_API_KEY"),
        "SILICONFLOW_API_KEY": remote.get("SILICONFLOW_API_KEY") or remote.get("POETRY_API_KEY"),
    }
    if len(token) != 64 or not token.isascii() or not token.isalnum():
        raise RuntimeError("Unexpected client token format")
    if any(not value for value in values.values()):
        raise RuntimeError("Required private credential is missing")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alias", default="rn")
    parser.add_argument("--local-env", default=".env")
    parser.add_argument("--remote-env", default="/opt/poetry-tang-20260922/.env")
    parser.add_argument("--remote-token", default="/opt/poetry-tang-20260922/client-token")
    parser.add_argument("--config", required=True)
    parser.add_argument("--wrangler-dir", required=True)
    args = parser.parse_args()
    values = credentials(args)
    command = ["pnpm", "--dir", args.wrangler_dir, "exec", "wrangler", "secret", "bulk",
               "--config", str(pathlib.Path(args.config).resolve())]
    environment = dict(os.environ, WRANGLER_SEND_METRICS="false")
    result = subprocess.run(command, input=json.dumps(values), text=True, env=environment,
                            capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Cloudflare secret deployment failed; credential output suppressed")
    print(json.dumps({"secrets_deployed": sorted(values), "values_logged": False}))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
