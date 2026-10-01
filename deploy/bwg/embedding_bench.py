"""Bounded keep-alive embedding comparison; credentials and raw vectors are never reported."""

import argparse
import concurrent.futures
import datetime
import http.client
import json
import math
import pathlib
import ssl
import threading
import time
import urllib.parse
import uuid
from collections import Counter

MODELS = ("BAAI/bge-m3", "Pro/BAAI/bge-m3")
QUERIES = (
    "\u671b\u6708\u601d\u5ff5\u5bb6\u4eba",
    "\u6625\u5929\u5c71\u91ce\u91cc\u7684\u82b1\u548c\u9e1f",
    "\u4e0e\u670b\u53cb\u79bb\u522b\u65f6\u7684\u60b2\u4f24",
    "\u8fb9\u585e\u5c06\u58eb\u5b88\u536b\u5bb6\u56fd",
    "\u96e8\u540e\u7684\u5c71\u5c45\u548c\u79cb\u591c",
    "\u767b\u697c\u8fdc\u671b\u58ee\u4e3d\u6cb3\u5c71",
    "\u72ec\u81ea\u6f02\u6cca\u5728\u5916\u601d\u5ff5\u6545\u4e61",
    "\u6e05\u98ce\u660e\u6708\u4e0b\u7684\u6c5f\u4e0a\u821f\u884c",
)


def environment(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 4)


def vector_valid(vector):
    return (isinstance(vector, list) and len(vector) == 1024
            and all(type(value) in (int, float) and math.isfinite(value) for value in vector)
            and any(value != 0 for value in vector))


def connection(base, timeout):
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or parsed.hostname != "api.siliconflow.cn":
        raise ValueError("Embedding endpoint must be the verified SiliconFlow HTTPS origin")
    return http.client.HTTPSConnection(parsed.hostname, timeout=timeout,
                                       context=ssl.create_default_context())


def limit_reason(body):
    error = body.get("error")
    nested = error.get("message", "") if isinstance(error, dict) else ""
    message = " ".join(str(value) for value in (body.get("message"), body.get("data"), nested))
    categories = {"rpm": "request_rate", "tpm": "token_rate", "concurrent": "concurrency",
                  "overload": "upstream_overload", "rate limit": "rate_limit_unspecified"}
    return next((category for keyword, category in categories.items()
                 if keyword in message.lower()), "unspecified")


def embedding(conn, key, model, text):
    started = time.monotonic()
    row = {"status": 0, "valid": False, "tokens": 0, "timeout": False}
    try:
        payload = json.dumps({"model": model, "input": text, "encoding_format": "float"})
        conn.request("POST", "/v1/embeddings", payload.encode(), {
            "Authorization": "Bearer " + key, "Content-Type": "application/json",
            "User-Agent": "poetry-pro-embedding-benchmark/1.0",
        })
        response = conn.getresponse()
        body = json.loads(response.read())
        row["status"] = response.status
        row["rate_headers"] = {name: value for name, value in response.getheaders()
                               if "ratelimit" in name.lower() or name.lower() == "retry-after"}
        if response.status == 200:
            vectors = body.get("data", [])
            vector = vectors[0].get("embedding") if len(vectors) == 1 else None
            row["valid"] = vector_valid(vector)
            row["tokens"] = int(body.get("usage", {}).get("total_tokens", 0))
            if row["valid"]:
                row["norm"] = math.sqrt(sum(value * value for value in vector))
                row["vector"] = vector
        else:
            row["error_code"] = str(body.get("code", body.get("error", {}).get("code", "")))
            row["limit_reason"] = limit_reason(body)
            if response.status == 429:
                row["utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                message = body.get("message") or body.get("data") or ""
                row["message"] = str(message).replace(key, "[REDACTED]")[:250]
    except Exception as error:
        row["exception"] = type(error).__name__
        row["timeout"] = isinstance(error, TimeoutError)
        conn.close()
    row["seconds"] = round(time.monotonic() - started, 4)
    return row


def public_row(row):
    return {key: value for key, value in row.items() if key != "vector"}


def price(metadata):
    item = metadata["models"][MODELS[1]]
    prices = [float(row["price"]) for row in item["pricing"]
              if row["specification"] == "prompt"]
    if len(prices) != 1 or not 0 < prices[0] <= 1 or item["priceUnit"] != "/ M Tokens":
        raise ValueError("Missing or unexpected official Pro input price")
    return prices[0]


def account(conn, key):
    conn.request("GET", "/v1/user/info", headers={"Authorization": "Bearer " + key})
    response = conn.getresponse()
    body = json.loads(response.read())
    data = body.get("data") or {}
    fields = ("balance", "chargeBalance", "totalBalance", "category")
    return {"status": response.status,
            "account": {key: data[key] for key in fields if key in data},
            "error_code": body.get("code") if response.status != 200 else None}


def qdrant_hit(vector, env):
    endpoint = env.get("QDRANT_CLUSTER_ENDPOINT", "")
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or not env.get("QDRANT_API_KEY"):
        raise ValueError("Cloud Qdrant HTTPS endpoint and key required for read-only parity")
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 6333, timeout=8)
    payload = {"vector": vector, "limit": 1, "with_payload": True,
               "filter": {"must": [
                   {"key": "generation", "match": {"value": "poetry-20260921-v1"}},
               ]}}
    try:
        conn.request("POST", "/collections/poetry_tang_20260922_v1/points/search",
                     json.dumps(payload).encode(), {"api-key": env["QDRANT_API_KEY"],
                                                    "Content-Type": "application/json"})
        response = conn.getresponse()
        body = json.loads(response.read())
        if response.status != 200 or not body.get("result"):
            return {"status": response.status, "valid": False}
        hit = body["result"][0]
        return {"status": response.status, "valid": True, "point_id": str(hit["id"]),
                "work_id": hit["payload"].get("work_id"), "score": hit["score"]}
    except Exception as error:
        return {"status": 0, "valid": False, "exception": type(error).__name__}
    finally:
        conn.close()


def parity(base, key, env, timeout):
    rows, conn = [], connection(base, timeout)
    try:
        for text in QUERIES:
            free = embedding(conn, key, MODELS[0], text)
            pro = embedding(conn, key, MODELS[1], text)
            row = {"query": text, "free": public_row(free), "pro": public_row(pro)}
            if free["valid"] and pro["valid"]:
                a, b = free["vector"], pro["vector"]
                row["cosine"] = sum(x * y for x, y in zip(a, b)) / free["norm"] / pro["norm"]
                row["max_element_delta"] = max(abs(x - y) for x, y in zip(a, b))
                if env:
                    row["free_hit"], row["pro_hit"] = qdrant_hit(a, env), qdrant_hit(b, env)
                    free_id, pro_id = row["free_hit"].get("work_id"), row["pro_hit"].get("work_id")
                    row["same_hit"] = free_id is not None and free_id == pro_id
            rows.append(row)
            if not free["valid"] or not pro["valid"]:
                break
    finally:
        conn.close()
    return rows


def stage_worker(context, model, number):
    conn = connection(context["base"], context["timeout"])
    rows = []
    try:
        while not context["stop"].is_set():
            with context["lock"]:
                if time.monotonic() >= context["deadline"] or context["count"] >= context["max"]:
                    break
                index = context["count"]
                context["count"] += 1
            text = QUERIES[index % len(QUERIES)] + " scenario " + context["nonce"] + str(index)
            row = embedding(conn, context["key"], model, text)
            rows.append(public_row(row))
            if not row["valid"]:
                with context["lock"]:
                    context["errors"] += 1
                    if context["errors"] >= max(3, context["workers"] // 4):
                        context["stop"].set()
    finally:
        conn.close()
    return rows


def stage(base, key, model, workers, seconds, max_requests, timeout):
    started = time.monotonic()
    start_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    context = {"base": base, "key": key, "timeout": timeout, "workers": workers,
               "deadline": started + seconds, "max": max_requests, "count": 0,
               "errors": 0, "nonce": uuid.uuid4().hex[:8],
               "lock": threading.Lock(), "stop": threading.Event()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(stage_worker, context, model, number) for number in range(workers)]
        rows = [row for job in jobs for row in job.result()]
    elapsed = time.monotonic() - started
    successful = [row for row in rows if row["valid"]]
    latencies = [row["seconds"] for row in successful]
    return {"model": model, "workers": workers, "seconds": round(elapsed, 4),
            "start_utc": start_utc,
            "finish_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "requests": len(rows), "success": len(successful),
            "error_percent": round(100 * (len(rows) - len(successful)) / max(1, len(rows)), 3),
            "success_rps": round(len(successful) / elapsed, 3),
            "p50_seconds": percentile(latencies, .5), "p95_seconds": percentile(latencies, .95),
            "p99_seconds": percentile(latencies, .99),
            "tokens": sum(row["tokens"] for row in successful),
            "status_counts": dict(Counter(str(row["status"]) for row in rows)),
            "stopped_early": context["stop"].is_set(), "rows": rows}


def args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=pathlib.Path, required=True)
    parser.add_argument("--qdrant-env", type=pathlib.Path)
    parser.add_argument("--metadata", type=pathlib.Path, required=True)
    parser.add_argument("--report", type=pathlib.Path, required=True)
    parser.add_argument("--seconds", type=float, default=12)
    parser.add_argument("--max-requests", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--concurrency", default="1,4,8,16,32")
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--skip-parity", action="store_true")
    parser.add_argument("--steady", action="store_true")
    parser.add_argument("--cooldown", type=float, default=2)
    result = parser.parse_args()
    result.concurrency = [int(value) for value in result.concurrency.split(",")]
    result.models = result.models.split(",")
    if any(value < 1 or value > 64 for value in result.concurrency):
        parser.error("Concurrency must be between 1 and 64")
    if not 0 < result.seconds <= 60 or not 1 <= result.max_requests <= 2500:
        parser.error("Stage duration <=60 seconds and <=2500 requests required")
    if not 0 <= result.cooldown <= 65:
        parser.error("Cooldown must be between zero and 65 seconds")
    if any(model not in MODELS for model in result.models) or result.report.exists():
        parser.error("Exact known models and a new output path required")
    return result


def finish(report, output, unit_price):
    tokens = sum(row["tokens"] for row in report["stages"] if row["model"] == MODELS[1])
    tokens += sum(row["pro"]["tokens"] for row in report["parity"])
    report["pro_tokens"] = tokens
    report["estimated_pro_cost_cny"] = tokens / 1_000_000 * unit_price
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({key: report[key] for key in ("pro_tokens", "estimated_pro_cost_cny")}))


def main():
    options = args()
    env = environment(options.env)
    key = env.get("SILICONFLOW_API_KEY")
    if not key or any(value.isspace() for value in key):
        raise SystemExit("A nonempty private SiliconFlow API key is required")
    metadata = json.loads(options.metadata.read_text())
    unit_price = price(metadata)
    maximum = options.max_requests * len(options.concurrency) + len(QUERIES)
    upper_cost = maximum * 512 / 1_000_000 * unit_price
    if upper_cost > 1:
        raise SystemExit("Conservative <=512-token input cost ceiling exceeds CNY 1")
    base = "https://api.siliconflow.cn/v1"
    conn = connection(base, options.timeout)
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "endpoint": base, "price_cny_per_million": unit_price,
              "maximum_estimated_cost_cny": upper_cost, "timeout_seconds": options.timeout,
              "workload": "Unique short poetry text; keep-alive; no retries; client closed loop",
              "account_before": account(conn, key), "parity": [], "stages": []}
    qdrant = environment(options.qdrant_env) if options.qdrant_env else None
    if not options.skip_parity:
        report["parity"] = parity(base, key, qdrant, options.timeout)
    pro_available = all(row["pro"]["valid"] for row in report["parity"])
    for model_index, model in enumerate(options.models):
        if model == MODELS[1] and not pro_available:
            continue
        for workers in options.concurrency:
            maximum = (options.max_requests if options.steady
                       else min(options.max_requests, max(30, workers * 12)))
            row = stage(base, key, model, workers, options.seconds,
                        maximum, options.timeout)
            report["stages"].append(row)
            summary = {key: value for key, value in row.items() if key != "rows"}
            print(json.dumps(summary), flush=True)
            if row["stopped_early"] or row["error_percent"] >= 5:
                break
            if model_index < len(options.models) - 1 or workers != options.concurrency[-1]:
                time.sleep(options.cooldown)
    report["account_after"] = account(conn, key)
    conn.close()
    finish(report, options.report, unit_price)


if __name__ == "__main__":
    main()
