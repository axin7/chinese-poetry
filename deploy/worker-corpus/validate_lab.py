"""Validate edge cache, authentication and largest documents against the native API."""

import argparse
import datetime
import hashlib
import http.client
import json
import pathlib
import sys
import time
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bwg"))
from validate_api import GENERATION, detail_path, request, response_row


def fingerprint(result):
    body = json.dumps(result.get("body"), ensure_ascii=False, sort_keys=True)
    return {key: result.get(key) for key in (
        "status", "origin_id", "cache", "cf_ray", "seconds", "generation",
    )} | {"body_sha256": hashlib.sha256(body.encode()).hexdigest()}


def detail_checks(base, token, baseline):
    rows = []
    for work_id in ("yudingquantangshi:39029", "yudingquantangshi:38694",
                    "yudingquantangshi:6890"):
        path = detail_path(work_id)
        first, second = persistent_pair(base, path, token)
        expected = request(baseline, path)
        valid = (first["status"] == second["status"] == expected["status"] == 200
                 and first["body"] == second["body"] == expected["body"]
                 and second.get("cache") == "HIT")
        rows.append({"label": "large_detail:" + work_id, "valid": valid,
                     "first": fingerprint(first), "second": fingerprint(second),
                     "baseline": fingerprint(expected)})
    return rows


def persistent_pair(base, path, token):
    parsed = urllib.parse.urlsplit(base)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=12)
    rows = []
    try:
        for _ in range(2):
            started = time.monotonic()
            connection.request("GET", parsed.path.rstrip("/") + path, headers={
                "Authorization": "Bearer " + token,
                "User-Agent": "poetry-api-deployment-validator/1.0",
            })
            with connection.getresponse() as response:
                rows.append(response_row(response, started))
    finally:
        connection.close()
    return rows


def boundary_checks(base, token):
    rows = []
    cases = (
        ("health", "/health", None, token, 200),
        ("cached_detail_without_auth", detail_path("yudingquantangshi:39029"),
         None, None, 401),
        ("oversized_body", "/search", b'{"query":"' + b"a" * 70000 + b'"}', token, 413),
        ("oversized_query", "/search", {"query": "a" * 513}, token, 422),
        ("null_query", "/search", {"query": None}, token, 422),
        ("null_vector", "/search", {"vector": None}, token, 422),
    )
    for label, path, payload, credential, status in cases:
        result = request(base, path, payload, token=credential)
        valid = result["status"] == status
        if label == "health":
            valid = valid and result["body"] == {"status": "ok", "generation": GENERATION}
        rows.append({"label": label, "valid": valid, "expected_status": status,
                     "result": fingerprint(result)})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--baseline-url", default="http://127.0.0.1:18080")
    parser.add_argument("--token-file", required=True, type=pathlib.Path)
    parser.add_argument("--report", required=True, type=pathlib.Path)
    args = parser.parse_args()
    if not args.base_url.startswith("https://"):
        parser.error("credentials require HTTPS")
    token = args.token_file.read_text().strip()
    rows = detail_checks(args.base_url, token, args.baseline_url)
    rows += boundary_checks(args.base_url, token)
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "checks": len(rows), "passed": sum(row["valid"] for row in rows), "rows": rows}
    with args.report.open("x") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print(json.dumps({key: report[key] for key in ("checks", "passed")}))
    return 0 if report["checks"] == report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
