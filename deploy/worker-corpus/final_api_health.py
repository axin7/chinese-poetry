"""Verify production and experimental APIs after bounded load testing."""

import argparse
import datetime as dt
import json
from pathlib import Path

import httpx


TARGETS = (
    ("worker", "https://poetry-rust-lab-20260930.963600436.workers.dev/poetry",
     "cf-poetry-rust-qdrant-gateway-20260930"),
    ("production", "https://rn-proxy-test.anyveo.com/poetry", "bwg-poetry-network-20260930"),
)


def check(client, name, base, origin, path, payload):
    try:
        response = client.request("POST" if payload else "GET", base + path, json=payload)
        if name == "production" and path == "/health":
            return {
                "target": name, "path": path, "passed": response.status_code == 404,
                "status": response.status_code, "protocol": response.http_version,
                "expected": "Production health is private; public route returns 404",
            }
        body = response.json()
        valid = response.status_code == 200 and response.headers.get("X-Origin-ID") == origin
        if path == "/health":
            valid = valid and body.get("status") == "ok"
        elif path.startswith("/poems/"):
            valid = valid and body.get("id") == "tangshisanbaishou:1" and bool(body["original"])
        else:
            valid = valid and set(body) == {"id", "original"} and bool(body["original"])
        return {
            "target": name, "path": path, "passed": valid, "status": response.status_code,
            "protocol": response.http_version, "origin": response.headers.get("X-Origin-ID"),
            "cache": response.headers.get("X-Poetry-Cache"), "body": body,
        }
    except (httpx.HTTPError, ValueError, KeyError, AttributeError, TypeError) as error:
        return {"target": name, "path": path, "passed": False, "error": type(error).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    if len(token) != 64 or args.report.exists():
        raise SystemExit("Require an existing private token and a new report path")
    rows = []
    headers = {"Authorization": "Bearer " + token,
               "User-Agent": "poetry-api-deployment-validator/1.0"}
    with httpx.Client(http2=True, timeout=12, follow_redirects=False, headers=headers) as client:
        for name, base, origin in TARGETS:
            for path, payload in (("/health", None), ("/poems/tangshisanbaishou:1", None),
                                  ("/search", {"query": "\u671b\u6708\u601d\u5ff5\u5bb6\u4eba"})):
                rows.append(check(client, name, base, origin, path, payload))
    document = {"collected_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "success": all(row["passed"] for row in rows), "rows": rows}
    with args.report.open("x") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"success": document["success"], "checks": len(rows),
                      "passed": sum(row["passed"] for row in rows)}))
    return 0 if document["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
