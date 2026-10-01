"""Validate Worker contracts over a reused HTTP/2 connection; never a capacity benchmark."""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import pathlib
import sys
import threading
import time
import urllib.parse
import uuid
from collections import Counter

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bwg"))
import validate_api as api
import validate_vectors as vectors
import validate_lab as lab

FIELDS = {"generation": "X-Poetry-Generation", "score": "X-Poetry-Score",
          "origin_id": "X-Origin-ID", "cache": "X-Poetry-Cache",
          "cf_ray": "CF-Ray", "coalesced": "X-Poetry-Coalesced",
          "placement": "CF-Placement"}


def origin(base):
    parsed = urllib.parse.urlsplit(base)
    return parsed.scheme + "://" + parsed.netloc


def failure_layer(row):
    if row.get("status") is None:
        return row.get("failure_layer", "client_transport")
    if row["status"] == 503 and isinstance(row.get("body"), dict):
        detail = str(row["body"].get("detail", ""))
        if "vector store" in detail or "embedding" in detail:
            return "worker_upstream"
        if detail == "request timed out":
            return "worker_deadline"
    return "http_response" if row["status"] >= 400 else None


def decoded(response, content, started):
    try:
        body = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        body = None
    row = {"status": response.status_code, "body": body,
           "content_type": response.headers.get("Content-Type", ""),
           "allow": response.headers.get("Allow", ""), "http_version": response.http_version,
           "body_bytes": len(content), "seconds": round(time.monotonic() - started, 4)}
    row.update({key: response.headers.get(header) for key, header in FIELDS.items()})
    row["colo"] = row["cf_ray"].rsplit("-", 1)[-1] if row["cf_ray"] else None
    row["failure_layer"] = failure_layer(row)
    return row


class Transport:
    def __init__(self, args):
        bases = (args.base_url, args.baseline_url, args.source_url)
        self.public = origin(args.base_url)
        self.clients = {origin(base): httpx.Client(
            http2=origin(base) == self.public, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(12),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1,
                                keepalive_expiry=None),
        ) for base in bases}
        self.rows = []
        self.lock = threading.Lock()

    def close(self):
        for client in self.clients.values():
            client.close()

    def remember(self, base, path, method, row):
        detail = row.get("body", {}).get("detail") if isinstance(row.get("body"), dict) else None
        summary = {key: row.get(key) for key in (
            "status", "error", "seconds", "http_version", "colo", "cf_ray", "cache",
            "coalesced", "placement", "failure_layer",
        )}
        summary.update(origin=origin(base), path=path, method=method, detail=detail)
        with self.lock:
            self.rows.append(summary)

    def request(self, base, path, payload=None, method=None, token=None, origin_ip=None):
        started = time.monotonic()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "poetry-api-deployment-validator/1.0"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = payload if isinstance(payload, bytes) else None
        if payload is not None and body is None:
            body = json.dumps(payload, ensure_ascii=False).encode()
        method = method or ("GET" if body is None else "POST")
        try:
            if origin_ip or origin(base) not in self.clients:
                raise ValueError("Only configured origins allowed; origin override unsupported")
            with self.clients[origin(base)].stream(method, base + path, content=body,
                                                   headers=headers) as response:
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > api.MAX_RESPONSE:
                        raise ValueError("API response exceeds size limit")
                row = decoded(response, content, started)
        except Exception as error:
            row = {"status": None, "body": None, "error": type(error).__name__,
                   "seconds": round(time.monotonic() - started, 4),
                   "failure_layer": "client_transport"}
        self.remember(base, path, method, row)
        return row


def install_transport(transport):
    originals = (api.request, vectors.request, lab.request, lab.persistent_pair)
    api.request = transport.request
    vectors.request = transport.request
    lab.request = transport.request
    lab.persistent_pair = lambda base, path, token: (
        transport.request(base, path, token=token),
        transport.request(base, path, token=token),
    )
    return originals


def restore_transport(originals):
    api.request, vectors.request, lab.request, lab.persistent_pair = originals


def section(rows, expected):
    return {"checks": len(rows), "expected_checks": expected,
            "passed": sum(bool(row.get("valid")) for row in rows), "rows": rows,
            "success": len(rows) == expected and all(row.get("valid") for row in rows)}


def api_checks(args, token):
    rows = api.auth_checks(args, token) + api.search_checks(args, token)
    rows += api.negative_checks(args, token) + api.concurrency_checks(args, token)
    return section(rows, 22)


def vector_checks(args, token):
    samples = vectors.stored_samples(args)
    rows = vectors.vector_checks(args, token, vectors.fixtures(samples))
    result = section(rows, 32)
    result.update(score_tolerance=vectors.SCORE_TOLERANCE,
                  samples=[{"point_id": str(point["id"]),
                            "locator": {key: point["payload"].get(key)
                                        for key in vectors.LOCATOR_FIELDS}} for point in samples],
                  baseline_store="RN native API verified to use current Qdrant Cloud collection")
    return result


def boundary_checks(args, token):
    rows = lab.detail_checks(args.base_url, token, args.baseline_url)
    rows += lab.boundary_checks(args.base_url, token)
    return section(rows, 9)


def cold_one(args, token, query, barrier):
    try:
        barrier.wait(timeout=15)
    except threading.BrokenBarrierError:
        return {"status": None, "body": None, "error": "BrokenBarrierError",
                "failure_layer": "client_synchronization", "seconds": 0}
    return api.request(args.base_url, "/search", {"query": query}, token=token)


def cold_details_valid(args, rows):
    details = {}
    for row in rows:
        work_id = row["body"]["id"]
        if work_id not in details:
            details[work_id] = api.request(args.baseline_url, api.detail_path(work_id))
        if not api.detail_valid(details[work_id], row["body"]):
            return False
    return bool(details)


def cold_checks(args, token):
    query = "\u671b\u6708\u601d\u4e61 " + uuid.uuid4().hex
    barrier = threading.Barrier(32)
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        jobs = [pool.submit(cold_one, args, token, query, barrier) for _ in range(32)]
        rows = [job.result() for job in jobs]
    valid = [row for row in rows if api.search_valid(row, api.DATASETS)]
    bodies = {json.dumps(row.get("body"), sort_keys=True) for row in rows}
    scores = [float(row["score"]) for row in valid]
    joined = sum(row.get("coalesced") == "JOIN" for row in rows)
    score_delta = max(scores) - min(scores) if scores else None
    mode_valid = (joined > 0 if args.coalescing_mode == "isolate"
                  else all(row.get("coalesced") is None for row in rows))
    details_valid = cold_details_valid(args, valid)
    response_valid = (len(bodies) == 1 and score_delta is not None
                      and score_delta <= vectors.SCORE_TOLERANCE
                      if args.coalescing_mode == "isolate" else details_valid)
    success = (len(valid) == 32 and response_valid and mode_valid
               and all(row.get("http_version") == "HTTP/2" for row in rows))
    verification = "passed" if success else "response_mismatch"
    if any(row.get("status") is None or row.get("http_version") != "HTTP/2" for row in rows):
        verification = "inconclusive_client_transport"
    elif len(valid) == 32 and not mode_valid:
        verification = "no_join_observed"
    return {"requests": len(rows), "passed": len(valid), "joined": joined,
            "leaders": sum(row.get("coalesced") == "LEADER" for row in rows),
            "cache_hits": sum(row.get("cache") == "HIT" for row in rows),
            "same_response": len(bodies) == 1, "max_score_delta": score_delta,
            "details_valid": details_valid,
            "seconds": round(time.monotonic() - started, 4), "success": success, "rows": rows,
            "verification_state": verification,
            "mode": args.coalescing_mode,
            "scope": "32 synchronized same-query cold requests; runtime correctness, not capacity",
            "query_sha256": hashlib.sha256(query.encode()).hexdigest()}


def safe_section(operation):
    try:
        return operation()
    except Exception as error:
        return {"success": False, "error": type(error).__name__, "checks": 0,
                "passed": 0, "rows": [], "failure_layer": "validation_harness"}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="HTTPS Worker URL including /poetry")
    parser.add_argument("--baseline-url", default="http://127.0.0.1:18080")
    parser.add_argument("--source-url", default="http://127.0.0.1:17333")
    parser.add_argument("--token-file", type=pathlib.Path, required=True)
    parser.add_argument("--coalescing-mode", choices=("independent", "isolate"),
                        default="independent")
    parser.add_argument("--report", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.base_url = api.validated_url(parser, args.base_url)
    args.baseline_url = api.validated_url(parser, args.baseline_url, loopback=True)
    args.source_url = api.validated_url(parser, args.source_url, loopback=True)
    args.origin_ip = None
    if urllib.parse.urlsplit(args.base_url).scheme != "https" or args.report.exists():
        parser.error("Worker URL must use HTTPS, and report path must be new")
    return args


def finalize(report, transport, path, token):
    public = [row for row in transport.rows if row["origin"] == transport.public]
    report["transport"] = {
        "http2_required": True, "public_requests": len(public),
        "protocols": dict(Counter(row.get("http_version") or "unknown" for row in public)),
        "colos": dict(Counter(row.get("colo") or "unknown" for row in public)),
        "placements": dict(Counter(row.get("placement") or "absent" for row in public)),
        "client_errors": sum(row["status"] is None for row in public),
        "rows": transport.rows,
    }
    sections = ("api", "vectors", "boundaries", "coalescing")
    report["success"] = (all(report[name]["success"] for name in sections)
                         and bool(public) and all(row.get("http_version") == "HTTP/2"
                                                  for row in public))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as output:
        json.dump(api.redact(report, token), output, indent=2)
        output.write("\n")
    print(json.dumps({"success": report["success"],
                      "sections": {name: {key: report[name].get(key) for key in
                                           ("checks", "passed", "joined", "success")}
                                   for name in sections}}))
    return 0 if report["success"] else 1


def main():
    args = arguments()
    token = args.token_file.read_text().strip()
    if not token or any(character.isspace() for character in token):
        raise SystemExit("Token file must contain one nonempty token")
    transport = Transport(args)
    originals = install_transport(transport)
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "base_url": args.base_url, "baseline_url": args.baseline_url,
              "source_url": args.source_url,
              "scope": "HTTP/2 contracts, vector parity, document bounds and cold requests"}
    try:
        report["api"] = safe_section(lambda: api_checks(args, token))
        report["vectors"] = safe_section(lambda: vector_checks(args, token))
        report["boundaries"] = safe_section(lambda: boundary_checks(args, token))
        report["coalescing"] = safe_section(lambda: cold_checks(args, token))
    finally:
        restore_transport(originals)
        transport.close()
    return finalize(report, transport, args.report, token)


if __name__ == "__main__":
    raise SystemExit(main())
