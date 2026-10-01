"""Check identical-vector API parity and bounded cached concurrency during deployment."""

import argparse
import concurrent.futures
import datetime
import ipaddress
import json
import math
import pathlib
import time
import urllib.parse

from validate_api import (
    DATASETS, GENERATION, PROFILE, candidate_request, detail_path, detail_valid,
    redact, request, search_valid, validated_url,
)

COLLECTION = "poetry_tang_20260922_v1"
SCORE_TOLERANCE = 1e-5
LOCATOR_FIELDS = ("dataset", "source_row_id", "raw_index", "normalized_index",
                  "work_id", "generation")


def sample_valid(point, dataset):
    if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
        return False
    vector, payload = point.get("vector"), point["payload"]
    if not isinstance(vector, list) or len(vector) != 1024:
        return False
    if not all(type(value) in (int, float) and math.isfinite(value) for value in vector):
        return False
    return (any(value != 0 for value in vector) and point.get("id") is not None
            and payload.get("dataset") == dataset and payload.get("generation") == GENERATION
            and isinstance(payload.get("work_id"), str)
            and payload["work_id"].startswith(dataset + ":"))


def stored_samples(args):
    samples = []
    for dataset in DATASETS:
        payload = {
            "limit": 8, "with_payload": True, "with_vector": True,
            "filter": {"must": [
                {"key": "dataset", "match": {"value": dataset}},
                {"key": "generation", "match": {"value": GENERATION}},
            ]},
        }
        response = request(args.source_url, "/collections/" + COLLECTION + "/points/scroll",
                           payload)
        body = response.get("body")
        result = body.get("result") if isinstance(body, dict) else None
        points = result.get("points") if isinstance(result, dict) else None
        if response["status"] != 200 or not isinstance(points, list) or len(points) != 8:
            raise ValueError("Source must return eight stored points for each dataset")
        if (not all(sample_valid(point, dataset) for point in points)
                or len({str(point["id"]) for point in points}) != 8):
            raise ValueError("Source points have invalid vectors or locator payloads")
        samples.extend(points)
    return samples


def fixtures(samples):
    result = []
    for point in samples:
        dataset = point["payload"]["dataset"]
        for filtered in (False, True):
            payload = {"vector": point["vector"], "embedding_profile": PROFILE}
            if filtered:
                payload["filters"] = {"tables": [dataset]}
            result.append({"label": str(point["id"]) + (":filtered" if filtered else ":all"),
                           "point_id": str(point["id"]), "payload": payload,
                           "allowed": (dataset,) if filtered else DATASETS})
    return result


def strict_baseline(args, row, fixture):
    baseline = request(args.baseline_url, "/search", fixture["payload"])
    row["baseline"] = baseline
    if not search_valid(baseline, fixture["allowed"]):
        row["valid"] = False
        return
    detail = request(args.baseline_url, detail_path(row["result"]["body"]["id"]))
    row["baseline_detail"] = detail
    row["same_hit"] = baseline["body"] == row["result"]["body"]
    row["same_detail"] = detail["status"] == 200 and detail["body"] == row["detail"]["body"]
    row["score_delta"] = abs(float(baseline["score"]) - float(row["result"]["score"]))
    row["valid"] = (row["valid"] and row["same_hit"] and row["same_detail"]
                    and row["score_delta"] <= SCORE_TOLERANCE)


def vector_checks(args, token, cases):
    rows = []
    for fixture in cases:
        result = candidate_request(args, "/search", fixture["payload"], token=token)
        row = {"label": fixture["label"], "query_point_id": fixture["point_id"],
               "result": result, "valid": search_valid(result, fixture["allowed"])}
        if row["valid"]:
            detail = candidate_request(args, detail_path(result["body"]["id"]), token=token)
            row.update(detail=detail, valid=detail_valid(detail, result["body"]))
            if args.baseline_url and row["valid"]:
                strict_baseline(args, row, fixture)
        rows.append(row)
    return rows


def burst_one(args, token, fixture, reference):
    result = candidate_request(args, "/search", fixture["payload"], token=token)
    valid = search_valid(result, fixture["allowed"])
    row = {"label": fixture["label"], "result": result, "valid": valid}
    if valid:
        row["same_hit"] = result["body"] == reference["result"]["body"]
        row["score_delta"] = abs(float(result["score"]) - float(reference["result"]["score"]))
        row["valid"] = row["same_hit"] and row["score_delta"] <= SCORE_TOLERANCE
    return row


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 4)


def concurrency_stage(args, token, cases, references, workers):
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(burst_one, args, token, fixture, reference)
                for fixture, reference in zip(cases, references)]
        rows = [job.result() for job in jobs]
    elapsed = time.monotonic() - started
    latencies = [row["result"]["seconds"] for row in rows if row["valid"]]
    return {"workers": workers, "requests": len(rows), "passed": len(latencies),
            "valid": all(row["valid"] for row in rows), "wall_seconds": round(elapsed, 4),
            "observed_success_qps": round(len(latencies) / elapsed, 4),
            "p50_seconds": percentile(latencies, 0.5),
            "p95_seconds": percentile(latencies, 0.95), "rows": rows}


def concurrency_checks(args, token, cases, references):
    stages = []
    if not all(row["valid"] for row in references):
        return stages
    for workers in (4, 8, 16):
        stage = concurrency_stage(args, token, cases, references, workers)
        stages.append(stage)
        if not stage["valid"]:
            break
    return stages


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18181")
    parser.add_argument("--baseline-url")
    parser.add_argument("--source-url", default="http://127.0.0.1:18133")
    parser.add_argument("--origin-ip")
    parser.add_argument("--token-file", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.base_url = validated_url(parser, args.base_url)
    args.source_url = validated_url(parser, args.source_url, loopback=True)
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


def main():
    args = arguments()
    token = args.token_file.read_text(encoding="utf-8").strip() if args.token_file else None
    if args.token_file and (not token or any(character.isspace() for character in token)):
        raise SystemExit("Token file must contain one nonempty token")
    report = {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "base_url": args.base_url, "baseline_url": args.baseline_url,
        "source_url": args.source_url, "origin_ip": args.origin_ip,
        "authenticated": bool(token), "collection": COLLECTION, "generation": GENERATION,
        "embedding_profile": PROFILE, "score_tolerance": SCORE_TOLERANCE,
        "scope": "Identical stored vectors; no embedding requests; bursts are not an SLA",
    }
    try:
        samples = stored_samples(args)
        cases = fixtures(samples)
        rows = vector_checks(args, token, cases)
        stages = concurrency_checks(args, token, cases, rows)
        report.update(samples=[{"point_id": str(point["id"]),
                                "locator": {field: point["payload"].get(field)
                                            for field in LOCATOR_FIELDS}}
                               for point in samples], rows=rows, stages=stages,
                      checks=len(rows), passed=sum(row["valid"] for row in rows))
        report["success"] = (all(row["valid"] for row in rows) and len(stages) == 3
                             and all(stage["valid"] for stage in stages))
    except (ValueError, TypeError, KeyError) as error:
        report.update(success=False, error=str(error), checks=0, passed=0)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x", encoding="utf-8") as handle:
        json.dump(redact(report, token), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({key: report[key] for key in ("success", "checks", "passed")}))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
