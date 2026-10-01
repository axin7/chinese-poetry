"""Check Cloud API sentence/detail contracts, optionally against the local API."""

import argparse
import datetime
import http.client
import json
import math
import pathlib
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

GENERATION = "poetry-20260921-v1"
COLLECTION = "poetry_tang_20260922_v1"
DATASETS = ("yudingquantangshi", "tangshisanbaishou")
PROFILE = "sf-bge-m3-1024-v1"
ROOT = pathlib.Path("/opt/poetry-tang-20260922")
QUERIES = (
    "\u60f3\u627e\u6625\u5929\u82b1\u5f00\u7684\u5510\u8bd7",
    "\u671b\u6708\u601d\u5ff5\u5bb6\u4eba",
    "\u670b\u53cb\u9001\u522b\u4f9d\u4f9d\u4e0d\u820d",
    "\u8fb9\u585e\u5c06\u58eb\u601d\u4e61",
    "\u5c71\u4e2d\u9690\u5c45\u7684\u751f\u6d3b",
    "\u519c\u6c11\u52b3\u4f5c\u751f\u6d3b\u4e0d\u6613",
)


class OriginConnection(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(
            raw, server_hostname=self.host
        )


def response_row(response, started):
    content = response.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("API response exceeds size limit")
    try:
        data = json.loads(content)
    except ValueError:
        data = {"non_json": True}
    return {
        "status": response.status,
        "body": data,
        "generation": response.headers.get("X-Poetry-Generation"),
        "score": response.headers.get("X-Poetry-Score"),
        "seconds": round(time.monotonic() - started, 4),
    }


def request(base, path, payload=None, token=None, origin=False):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "poetry-cloud-migration-check/1.0",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None if payload is None else json.dumps(payload).encode()
    started = time.monotonic()
    if origin:
        connection = OriginConnection(
            urllib.parse.urlsplit(base).hostname, 443, timeout=12
        )
        try:
            connection.request("GET" if body is None else "POST", path, body, headers)
            with connection.getresponse() as response:
                return response_row(response, started)
        finally:
            connection.close()
    req = urllib.request.Request(base + path, body, headers)
    try:
        response = urllib.request.urlopen(req, timeout=12)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response_row(response, started)


def stored_samples():
    for dataset in DATASETS:
        body = {
            "limit": 128,
            "with_payload": True,
            "with_vector": True,
            "filter": {"must": [{"key": "dataset", "match": {"value": dataset}}]},
        }
        response = request(
            "http://127.0.0.1:17333",
            "/collections/" + COLLECTION + "/points/scroll",
            body,
        )
        if response["status"] != 200:
            raise ValueError("Source vector sampling failed")
        points = response["body"]["result"]["points"]
        yield from points[::16][:8]


def detail_path(work_id, generation=GENERATION, prefix=""):
    return (
        prefix
        + "/poems/"
        + urllib.parse.quote(work_id, safe="")
        + "?generation="
        + urllib.parse.quote(generation)
    )


def check_result(base, result, allowed, prefix="", token=None, origin=False):
    body = result["body"]
    if result["status"] != 200 or set(body) != {"id", "original"}:
        return {"valid": False, "error": "Search status or response contract"}
    dataset = body["id"].partition(":")[0]
    if dataset not in allowed or not isinstance(body["original"], str):
        return {"valid": False, "error": "Search locator or sentence"}
    score = result["score"]
    if (
        result["generation"] != GENERATION
        or score is None
        or not math.isfinite(float(score))
    ):
        return {"valid": False, "error": "Search metadata"}
    detail = request(
        base, detail_path(body["id"], prefix=prefix), token=token, origin=origin
    )
    value = detail["body"]
    valid = (
        detail["status"] == 200
        and value.get("id") == body["id"]
        and value.get("dataset") == dataset
        and bool(value.get("title"))
        and isinstance(value.get("original"), list)
        and body["original"] in value["original"]
        and isinstance(value.get("translation"), list)
    )
    return {"valid": valid, "detail": value}


def exact_self_evidence(point, payload, candidate, detail):
    query_filter = {"must": [{"key": "generation", "match": {"value": GENERATION}}]}
    tables = payload.get("filters", {}).get("tables", [])
    if tables:
        query_filter["must"].append({"key": "dataset", "match": {"any": tables}})
    oracle = request(
        "http://127.0.0.1:17333",
        "/collections/" + COLLECTION + "/points/query",
        {
            "query": payload["vector"],
            "filter": query_filter,
            "limit": 1,
            "with_payload": True,
            "with_vector": False,
            "params": {"exact": True, "quantization": {"ignore": True}},
        },
    )
    hits = oracle["body"].get("result", {}).get("points", [])
    hit = hits[0] if len(hits) == 1 else {}
    locator = point["payload"]
    index = locator["normalized_index"]
    originals = detail.get("original", [])
    expected = originals[index] if index < len(originals) else None
    matches = (
        oracle["status"] == 200
        and hit.get("id") == point["id"]
        and abs(hit.get("score", 0) - 1) <= 1e-5
        and candidate["body"].get("id") == locator["work_id"]
        and expected is not None
        and candidate["body"].get("original") == expected
    )
    return {
        "query_point_id": point["id"],
        "exact_hit_id": hit.get("id"),
        "exact_score": hit.get("score"),
        "expected_original": expected,
        "candidate_matches_exact_self": matches,
    }


def compare_search(args, payload, label, allowed, point=None):
    result = request(args.candidate_url, "/search", payload)
    checked = check_result(args.candidate_url, result, allowed)
    row = {"label": label, "candidate": result, "valid": checked["valid"]}
    if not args.baseline_url or not checked["valid"]:
        return row
    baseline = request(args.baseline_url, "/search", payload)
    baseline_checked = check_result(args.baseline_url, baseline, allowed)
    same_hit = baseline["body"] == result["body"]
    baseline_detail = request(args.baseline_url, detail_path(result["body"]["id"]))
    same_detail = baseline_detail["status"] == 200 and (
        baseline_detail["body"] == checked["detail"]
    )
    row.update(
        baseline=baseline,
        same_hit=same_hit,
        same_detail=same_detail,
        baseline_valid=baseline_checked["valid"],
    )
    if baseline_checked["valid"]:
        row["score_delta"] = abs(float(baseline["score"]) - float(result["score"]))
    recovered = False
    if not same_hit and point is not None and baseline_checked["valid"] and same_detail:
        evidence = exact_self_evidence(point, payload, result, checked["detail"])
        row["exact_self_evidence"] = evidence
        recovered = evidence["candidate_matches_exact_self"]
    row["source_approximation_miss"] = recovered
    row["valid"] = (
        row["valid"]
        and baseline_checked["valid"]
        and same_detail
        and (same_hit or recovered)
    )
    return row


def search_checks(args):
    rows = []
    for point in stored_samples():
        dataset = point["payload"]["dataset"]
        for filtered in (False, True):
            payload = {"vector": point["vector"], "embedding_profile": PROFILE}
            if filtered:
                payload["filters"] = {"tables": [dataset]}
            label = "stored:" + point["id"] + (":filtered" if filtered else ":all")
            rows.append(
                compare_search(
                    args, payload, label, (dataset,) if filtered else DATASETS, point
                )
            )
    for index, query in enumerate(QUERIES):
        dataset = DATASETS[index % 2]
        rows.append(
            compare_search(
                args,
                {
                    "query": query,
                    "filters": {"tables": [dataset]},
                },
                "text:" + str(index),
                (dataset,),
            )
        )
    return rows


def negative_checks(args):
    rows = []
    cases = (
        (
            "bad_dimension",
            "/search",
            {"vector": [1], "embedding_profile": PROFILE},
            422,
        ),
        (
            "bad_profile",
            "/search",
            {"query": QUERIES[0], "embedding_profile": "other"},
            422,
        ),
        (
            "excluded_dataset",
            "/search",
            {"query": QUERIES[0], "filters": {"tables": ["songci"]}},
            404,
        ),
        ("excluded_detail", "/poems/songci%3A1", None, 404),
        ("stale_generation", detail_path("tangshisanbaishou:1", "old-v1"), None, 404),
    )
    for label, path, payload, expected in cases:
        result = request(args.candidate_url, path, payload)
        rows.append(
            {"label": label, "result": result, "valid": result["status"] == expected}
        )
    health = request(args.candidate_url, "/health")
    rows.append(
        {
            "label": "health",
            "result": health,
            "valid": (
                health["status"] == 200
                and health["body"] == {"status": "ok", "generation": GENERATION}
            ),
        }
    )
    return rows


def public_checks(args):
    if not args.public_url:
        return []
    token = (ROOT / "client-token").read_text().strip()
    body = {"query": QUERIES[0], "filters": {"tables": [DATASETS[1]]}}
    denied = request(args.public_url, "/poetry/search", body, origin=args.public_origin)
    wrong = request(
        args.public_url,
        "/poetry/search",
        body,
        token="invalid-token",
        origin=args.public_origin,
    )
    result = request(
        args.public_url, "/poetry/search", body, token=token, origin=args.public_origin
    )
    check = check_result(
        args.public_url, result, (DATASETS[1],), "/poetry", token, args.public_origin
    )
    return [
        {
            "label": "public_missing_auth",
            "result": denied,
            "valid": denied["status"] == 401,
        },
        {
            "label": "public_wrong_auth",
            "result": wrong,
            "valid": wrong["status"] == 401,
        },
        {"label": "public_search_detail", "result": result, "valid": check["valid"]},
    ]


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-url", default="http://127.0.0.1:18081")
    parser.add_argument("--baseline-url")
    parser.add_argument("--public-url")
    parser.add_argument(
        "--public-origin",
        action="store_true",
        help="Connect to RN loopback443, retaining public TLS hostname",
    )
    parser.add_argument("--report", type=pathlib.Path, required=True)
    args = parser.parse_args()
    for value in (args.candidate_url, args.baseline_url):
        if value is None:
            continue
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path
        ):
            parser.error("API URLs must be loopback HTTP origins")
    if args.public_url and args.public_url != "https://rn-proxy-test.anyveo.com":
        parser.error("Unexpected public API origin")
    if args.public_origin and not args.public_url:
        parser.error("public-origin requires public-url")
    if args.report.exists():
        parser.error("Report exists; choose a new path")
    return args


def main():
    args = arguments()
    rows = search_checks(args) + negative_checks(args) + public_checks(args)
    report = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "candidate_url": args.candidate_url,
        "baseline_url": args.baseline_url,
        "public_url": args.public_url,
        "public_origin": args.public_origin,
        "checks": len(rows),
        "passed": sum(row["valid"] for row in rows),
        "rows": rows,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("checks", "passed")}))
    raise SystemExit(0 if report["checks"] == report["passed"] else 1)


if __name__ == "__main__":
    main()
