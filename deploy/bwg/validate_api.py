"""Validate poetry API contracts through loopback, HTTPS origin, or public DNS."""

import argparse
import concurrent.futures
import datetime
import http.client
import ipaddress
import json
import math
import pathlib
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

GENERATION = "poetry-20260921-v1"
PROFILE = "sf-bge-m3-1024-v1"
DATASETS = ("yudingquantangshi", "tangshisanbaishou")
QUERIES = (
    "\u60f3\u627e\u6625\u5929\u82b1\u5f00\u7684\u5510\u8bd7",
    "\u671b\u6708\u601d\u5ff5\u5bb6\u4eba",
    "\u670b\u53cb\u9001\u522b\u4f9d\u4f9d\u4e0d\u820d",
    "\u8fb9\u585e\u5c06\u58eb\u601d\u4e61",
    "\u5c71\u4e2d\u9690\u5c45\u7684\u751f\u6d3b",
    "\u519c\u6c11\u52b3\u4f5c\u751f\u6d3b\u4e0d\u6613",
)
MAX_RESPONSE = 2 * 1024 * 1024
TIMEOUT = 12


class OriginConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, origin_ip):
        super().__init__(host, port, timeout=TIMEOUT)
        self.origin_ip = origin_ip

    def connect(self):
        raw = socket.create_connection((self.origin_ip, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def response_row(response, started):
    content = response.read(MAX_RESPONSE + 1)
    if len(content) > MAX_RESPONSE:
        raise ValueError("API response exceeds size limit")
    try:
        body = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        body = None
    return {
        "status": response.status,
        "body": body,
        "content_type": response.headers.get("Content-Type", ""),
        "generation": response.headers.get("X-Poetry-Generation"),
        "score": response.headers.get("X-Poetry-Score"),
        "allow": response.headers.get("Allow", ""),
        "origin_id": response.headers.get("X-Origin-ID"),
        "cache": response.headers.get("X-Poetry-Cache"),
        "cf_ray": response.headers.get("CF-Ray"),
        "seconds": round(time.monotonic() - started, 4),
    }


def request(base, path, payload=None, method=None, token=None, origin_ip=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "poetry-api-deployment-validator/1.0",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    body = payload if isinstance(payload, bytes) else None
    if payload is not None and body is None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    method = method or ("GET" if body is None else "POST")
    started = time.monotonic()
    connection = None
    try:
        if origin_ip:
            parsed = urllib.parse.urlsplit(base)
            connection = OriginConnection(parsed.hostname, parsed.port or 443, origin_ip)
            connection.request(method, parsed.path.rstrip("/") + path, body, headers)
            response = connection.getresponse()
        else:
            req = urllib.request.Request(base + path, body, headers, method=method)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            try:
                response = opener.open(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as error:
                response = error
        with response:
            return response_row(response, started)
    except (OSError, http.client.HTTPException, ValueError) as error:
        return {"status": None, "body": None, "error": type(error).__name__,
                "seconds": round(time.monotonic() - started, 4)}
    finally:
        if connection:
            connection.close()


def candidate_request(args, path, payload=None, method=None, token=None):
    return request(args.base_url, path, payload, method, token, args.origin_ip)


def detail_path(work_id, generation=GENERATION):
    return "/poems/" + urllib.parse.quote(work_id, safe="") + "?generation=" + generation


def search_valid(result, allowed):
    body = result.get("body")
    if result["status"] != 200 or not isinstance(body, dict):
        return False
    if set(body) != {"id", "original"} or not isinstance(body["id"], str):
        return False
    dataset, separator, row_id = body["id"].partition(":")
    if dataset not in allowed or not separator or not re.fullmatch(r"[1-9][0-9]*", row_id):
        return False
    if not isinstance(body["original"], str) or not body["original"].strip():
        return False
    try:
        score_valid = math.isfinite(float(result.get("score")))
    except (TypeError, ValueError):
        score_valid = False
    return (result.get("generation") == GENERATION and score_valid
            and result.get("content_type", "").lower().startswith("application/json"))


def strings_valid(values):
    return isinstance(values, list) and all(isinstance(value, str) for value in values)


def detail_valid(result, search_body):
    body = result.get("body")
    fields = {"id", "dataset", "title", "author", "original", "translation", "interpretations"}
    if result["status"] != 200 or not isinstance(body, dict) or set(body) != fields:
        return False
    return (
        body["id"] == search_body["id"]
        and body["dataset"] == search_body["id"].partition(":")[0]
        and isinstance(body["title"], str) and bool(body["title"].strip())
        and (body["author"] is None or isinstance(body["author"], str))
        and all(strings_valid(body[field])
                for field in ("original", "translation", "interpretations"))
        and len(body["original"]) == len(body["translation"])
        and search_body["original"] in body["original"]
        and result.get("content_type", "").lower().startswith("application/json")
    )


def compare_baseline(args, row, payload, allowed):
    baseline = request(args.baseline_url, "/search", payload)
    row["baseline"] = baseline
    if not search_valid(baseline, allowed):
        row["valid"] = False
        return
    detail = request(args.baseline_url, detail_path(row["result"]["body"]["id"]))
    row["baseline_detail"] = detail
    row["same_hit"] = baseline["body"] == row["result"]["body"]
    row["same_detail"] = detail["status"] == 200 and detail["body"] == row["detail"]["body"]
    row["score_delta"] = abs(float(baseline["score"]) - float(row["result"]["score"]))
    # Independent text embeddings can vary; validate_vectors checks identical-input score parity.
    row["valid"] = row["valid"] and row["same_hit"] and row["same_detail"]


def search_checks(args, token):
    rows = []
    for index, query in enumerate(QUERIES):
        dataset = DATASETS[index % 2]
        payload = {"query": query, "filters": {"tables": [dataset]}}
        result = candidate_request(args, "/search", payload, token=token)
        row = {"label": "text:" + str(index), "query": query, "dataset": dataset,
               "result": result, "valid": search_valid(result, (dataset,))}
        if row["valid"]:
            detail = candidate_request(args, detail_path(result["body"]["id"]), token=token)
            row.update(detail=detail, valid=detail_valid(detail, result["body"]))
            if args.baseline_url and row["valid"]:
                compare_baseline(args, row, payload, (dataset,))
        rows.append(row)
    return rows


def negative_cases():
    vector = [1] + [0] * 1023
    return (
        ("empty_query", "/search", {"query": " "}, None, 422),
        ("bad_dimension", "/search", {"vector": [1], "embedding_profile": PROFILE}, None, 422),
        ("missing_profile", "/search", {"vector": vector}, None, 422),
        ("bad_profile", "/search", {"query": QUERIES[0], "embedding_profile": "other"}, None, 422),
        ("unknown_field", "/search", {"query": QUERIES[0], "unexpected": 1}, None, 422),
        ("malformed_json", "/search", b"{", None, 422),
        ("excluded_dataset", "/search",
         {"query": QUERIES[0], "filters": {"tables": ["songci"]}}, None, 404),
        ("excluded_detail", "/poems/songci%3A1", None, None, 404),
        ("stale_generation", detail_path("tangshisanbaishou:1", "old-v1"), None, None, 404),
        ("ambiguous_generation", detail_path("tangshisanbaishou:1")
         + "&generation=" + GENERATION, None, None, 404),
        ("search_get", "/search", None, "GET", 405),
        ("search_put", "/search", {"query": QUERIES[0]}, "PUT", 405),
        ("detail_post", "/poems/tangshisanbaishou%3A1", {}, "POST", 405),
    )


def negative_checks(args, token):
    rows = []
    for label, path, payload, method, expected in negative_cases():
        result = candidate_request(args, path, payload, method, token)
        valid = result["status"] == expected
        if expected != 405:
            body = result.get("body")
            valid = (valid and isinstance(body, dict) and set(body) == {"detail"}
                     and isinstance(body["detail"], str) and bool(body["detail"]))
        else:
            allowed = "GET" if label == "detail_post" else "POST"
            valid = valid and allowed in result.get("allow", "").split(", ")
        rows.append({"label": label, "result": result, "expected_status": expected,
                     "valid": valid})
    return rows


def auth_checks(args, token):
    if not token:
        return []
    payload = {"query": QUERIES[0]}
    rows = []
    for label, credential in (("missing_auth", None), ("wrong_auth", "invalid-token")):
        result = candidate_request(args, "/search", payload, token=credential)
        rows.append({"label": label, "result": result, "valid": result["status"] == 401})
    return rows


def loopback_health(args, label):
    parsed = urllib.parse.urlsplit(args.base_url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.path:
        return []
    result = candidate_request(args, "/health")
    valid = result["status"] == 200 and result["body"] == {
        "status": "ok", "generation": GENERATION,
    }
    return [{"label": label, "result": result, "valid": valid}]


def concurrency_checks(args, token):
    payload = {"query": QUERIES[0], "filters": {"tables": [DATASETS[0]]}}
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(candidate_request, args, "/search", payload, None, token)
                for _ in range(4)]
        results = [job.result() for job in jobs]
    return [{"label": "cached_concurrency", "workers": 2, "requests": 4,
             "seconds": round(time.monotonic() - started, 4), "results": results,
             "valid": all(search_valid(result, (DATASETS[0],)) for result in results)}]


def validated_url(parser, value, loopback=False):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        parser.error("API URL must be an HTTP(S) URL without credentials, query, or fragment")
    if loopback and (parsed.scheme != "http" or parsed.path
                     or parsed.hostname not in ("127.0.0.1", "localhost", "::1")):
        parser.error("Baseline URL must be a loopback HTTP origin")
    return value.rstrip("/")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080",
                        help="Include /poetry for a public route")
    parser.add_argument("--token-file", type=pathlib.Path)
    parser.add_argument("--origin-ip", help="Connect to this IP while keeping HTTPS Host and SNI")
    parser.add_argument("--baseline-url", help="Optional loopback HTTP API for comparison")
    parser.add_argument("--report", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.base_url = validated_url(parser, args.base_url)
    if args.baseline_url:
        args.baseline_url = validated_url(parser, args.baseline_url, loopback=True)
    if args.origin_ip:
        try:
            ipaddress.ip_address(args.origin_ip)
        except ValueError:
            parser.error("origin-ip must be an IPv4 or IPv6 address")
        if urllib.parse.urlsplit(args.base_url).scheme != "https":
            parser.error("origin-ip requires an HTTPS base URL")
    if args.token_file and urllib.parse.urlsplit(args.base_url).scheme != "https":
        parser.error("Authenticated public checks require HTTPS")
    if args.report.exists():
        parser.error("Report exists; choose a new path")
    return args


def redact(value, token):
    if isinstance(value, dict):
        return {key: redact(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, str) and token:
        return value.replace(token, "[REDACTED]")
    return value


def main():
    args = arguments()
    token = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else None
    if args.token_file and (not token or any(character.isspace() for character in token)):
        raise SystemExit("Token file must contain one nonempty token")
    rows = loopback_health(args, "health_before") + auth_checks(args, token)
    rows += search_checks(args, token) + negative_checks(args, token)
    rows += concurrency_checks(args, token) + loopback_health(args, "health_after")
    report = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "base_url": args.base_url, "origin_ip": args.origin_ip,
        "baseline_url": args.baseline_url, "authenticated": bool(token),
        "generation": GENERATION, "embedding_profile": PROFILE,
        "checks": len(rows), "passed": sum(row["valid"] for row in rows), "rows": rows,
        "scope": "API contracts; semantic relevance and sustained capacity are not scored",
        "concurrency": "Four repeated cached queries, two workers; not a capacity benchmark",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as handle:
        json.dump(redact(report, token), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: report[key] for key in ("checks", "passed")}))
    return 0 if report["checks"] == report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
