"""Bounded loopback validation for the translated Tang-only deployment.

Text searches can call the server's configured paid embedding provider. Smoke
sends at most 15 searches; benchmark sends at most 180. The script has no API key,
does not retry requests, and does not measure upstream provider billing. Run a
single benchmark per query offset to avoid warmed exact-query caches on reruns.
"""

import argparse
import ast
import collections
import concurrent.futures
import datetime
import http.client
import json
import pathlib
import random
import threading
import time
import urllib.parse


DATASETS = {"yudingquantangshi", "tangshisanbaishou"}
GENERATION = "poetry-20260921-v1"
TOPICS = [
    "想找写春天花开、溪水清澈的唐诗。",
    "想读秋雨落在空山、远处有钟声的诗句。",
    "寻找旅人客居异乡、望月思念家人的诗句。",
    "有哪些诗描写朋友送别时依依不舍的心情？",
    "请找描写寒夜风雪、旅途艰辛的唐诗。",
    "想读江上行船、两岸青山相对的诗句。",
    "寻找夏夜乘凉、微风吹过荷塘的意境。",
    "请找描写边塞辽阔、将士思乡的诗句。",
    "想看遭遇挫折后仍保持志向的唐诗。",
    "寻找山中隐居、不追逐名利的生活意境。",
    "请找描写农民劳作、生活不易的诗句。",
    "想读朋友重逢、围坐饮酒谈心的唐诗。",
    "寻找清晨鸟鸣、山林逐渐苏醒的景象。",
    "请找写落日余晖照在江水上的诗句。",
    "想读对古城遗迹和历史变迁的感慨。",
    "寻找听琵琶或笛声后触动内心的诗句。",
    "有哪些诗写童年往事和故乡生活？",
    "想找表达独处时内心平静自在的唐诗。",
    "寻找登高远望、心胸开阔的意境。",
    "请找赞叹瀑布奔腾、山河壮丽的诗句。",
    "想读对年华易逝、珍惜眼前时光的感悟。",
    "寻找茶香、竹影与安静庭院的诗意。",
    "请找描写战乱离散、盼望团圆的唐诗。",
    "想看老友远去后回忆往日情谊的诗句。",
]
PREFERENCES = [
    "请优先选择描写具体景物和动作的句子。",
    "请优先选择直接表达人物情感的句子。",
    "希望句子体现动静相映的画面。",
    "希望句子突出声音带来的感受。",
    "请优先选择带有季节变化的描写。",
    "请寻找情绪含蓄、值得回味的表达。",
    "希望诗句呈现人与自然的联系。",
    "请优先选择体现时光流逝的句子。",
    "希望诗句能给失意的人带来宽慰。",
    "请寻找用细小日常事物表达感情的句子。",
    "希望诗句能呈现空间辽阔的感觉。",
    "请优先选择有鲜明色彩和光影的句子。",
]


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "benchmark"), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--generation", default=GENERATION)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--query-source", type=pathlib.Path)
    parser.add_argument("--query-offset", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=15)
    parser.add_argument("--max-requests", type=int, default=180)
    parser.add_argument("--stop-p95", type=float, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.endpoint = urllib.parse.urlsplit(args.base_url)
    endpoint = args.endpoint
    if (endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost")
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path not in ("", "/")):
        parser.error("Only credential-free loopback HTTP endpoints are accepted")
    if not 1 <= args.seconds <= 30 or not 3 <= args.max_requests <= 180:
        parser.error("seconds must be 1..30; max-requests must be 3..180")
    if args.query_offset < 0 or not 0 < args.stop_p95 <= 10:
        parser.error("query-offset must be nonnegative; stop-p95 must be 0..10")
    if args.output.exists() and not args.overwrite and not args.dry_run:
        parser.error("Output exists; choose a new path or explicitly use --overwrite")
    return args


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def query_pool(args):
    topics = TOPICS
    if args.query_source:
        tree = ast.parse(args.query_source.read_text(encoding="utf-8"))
        assignments = [node for node in tree.body if isinstance(node, ast.Assign)]
        values = [node.value for node in assignments
                  if any(isinstance(key, ast.Name) and key.id == "TOPICS"
                         for key in node.targets)]
        if len(values) != 1:
            raise ValueError("Query source must define one literal TOPICS list")
        topics = ast.literal_eval(values[0])
    if not isinstance(topics, list) or not topics or any(
            not isinstance(topic, str) or not topic.strip() for topic in topics):
        raise ValueError("TOPICS must be a nonempty list of nonempty strings")
    queries = [topic + preference for topic in topics for preference in PREFERENCES]
    if len(queries) != len(set(queries)):
        raise ValueError("Query corpus contains duplicates")
    random.Random(20260922).shuffle(queries)
    end = args.query_offset + args.max_requests
    if end > len(queries):
        raise ValueError("Query corpus exhausted; provide a larger --query-source")
    return queries[args.query_offset:end]


def connection(args):
    return http.client.HTTPConnection(args.endpoint.hostname, args.endpoint.port or 80,
                                      timeout=12)


def request(client, method, path, payload=None):
    started = time.monotonic()
    row = {"status": 0, "started_utc": utc_now()}
    try:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload else None
        headers = {"Content-Type": "application/json"} if payload else {}
        client.request(method, path, body, headers)
        response = client.getresponse()
        row["status"] = response.status
        content = response.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("Response exceeds size limit")
        row["payload"] = json.loads(content)
    except (OSError, ValueError, http.client.HTTPException) as error:
        row["error"] = type(error).__name__
        client.close()
    row["latency_s"] = time.monotonic() - started
    return row


def health(args):
    client = connection(args)
    try:
        row = request(client, "GET", "/health")
    finally:
        client.close()
    row["success"] = (row["status"] == 200 and row.get("payload") == {
        "status": "ok", "generation": args.generation})
    return row


def valid_search(payload, generation, allowed):
    if not isinstance(payload, dict):
        return False
    match, poem = payload.get("match"), payload.get("poem")
    if not isinstance(match, dict) or not isinstance(poem, dict):
        return False
    if any(not isinstance(match.get(key), str) or not match[key].strip()
           for key in ("original", "translation")):
        return False
    work_id, url = poem.get("id"), poem.get("detail_url")
    index = match.get("sentence_index")
    if (not isinstance(work_id, str) or not isinstance(url, str)
            or type(index) is not int or index < 0):
        return False
    dataset, separator, row_id = work_id.partition(":")
    parsed = urllib.parse.urlsplit(url)
    return (dataset in allowed and separator == ":" and row_id.isdecimal()
            and int(row_id) > 0 and not parsed.netloc and not parsed.scheme
            and not parsed.fragment and urllib.parse.unquote(parsed.path) == "/poems/" + work_id
            and urllib.parse.parse_qs(parsed.query) == {"generation": [generation]})


def search_one(client, query, generation, filters=None):
    body = {"query": query}
    if filters:
        body["filters"] = {"tables": filters}
    row = request(client, "POST", "/search", body)
    row.update(query=query, filters=filters)
    allowed = set(filters) if filters else DATASETS
    row["success"] = (row["status"] == 200 and
                      valid_search(row.get("payload"), generation, allowed))
    if row["success"]:
        row["dataset"] = row["payload"]["poem"]["id"].partition(":")[0]
    elif "error" not in row:
        row["error"] = "HTTP or search contract validation failed"
    return row


def valid_detail(detail, search):
    if detail["status"] != 200 or not isinstance(detail.get("payload"), dict):
        return False
    payload, result = detail["payload"], search["payload"]
    original, translation = payload.get("original"), payload.get("translation")
    index = result["match"]["sentence_index"]
    return (payload.get("id") == result["poem"]["id"]
            and payload.get("dataset") == search["dataset"]
            and payload.get("title") == result["poem"].get("title")
            and isinstance(original, list) and isinstance(translation, list)
            and len(original) == len(translation) and index < len(original)
            and original[index] == result["match"]["original"]
            and translation[index] == result["match"]["translation"])


def detail_checks(client, search):
    detail_url = search["payload"]["poem"]["detail_url"]
    row = request(client, "GET", detail_url)
    result = {"work_id": search["payload"]["poem"]["id"],
              "status": row["status"], "aligned": valid_detail(row, search)}
    bare_url = urllib.parse.urlsplit(detail_url).path
    wrong = request(client, "GET", bare_url + "?generation=invalid-generation")
    missing = request(client, "GET", bare_url)
    result.update(wrong_generation_status=wrong["status"],
                  missing_generation_status=missing["status"])
    result["success"] = result["aligned"] and wrong["status"] == missing["status"] == 404
    return result


def smoke(args):
    rows, details = [], []
    cases = [(query, None) for query in TOPICS[:12]]
    cases.extend([(TOPICS[12], ["yudingquantangshi"]),
                  (TOPICS[13], ["tangshisanbaishou"])])
    client = connection(args)
    try:
        for query, filters in cases:
            row = search_one(client, query, args.generation, filters)
            rows.append(row)
            if row["success"]:
                details.append(detail_checks(client, row))
        excluded = search_one(client, TOPICS[14], args.generation, ["tangsong"])
        excluded["success"] = (excluded["status"] in (404, 422)
                               and isinstance(excluded.get("payload"), dict)
                               and "detail" in excluded["payload"])
        rows.append(excluded)
    finally:
        client.close()
    return {"success": all(row["success"] for row in rows + details),
            "search_requests": len(rows), "search_results": rows,
            "detail_checks": details,
            "dataset_counts": dict(collections.Counter(row.get("dataset")
                for row in rows if row.get("dataset") in DATASETS)),
            "semantic_relevance": "Not scored; broad queries verify retrieval contracts"}


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = int(position), min(int(position) + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def stage_summary(rows, elapsed, concurrency):
    successful = [row["latency_s"] for row in rows if row["success"]]
    return {"concurrency": concurrency, "requests": len(rows),
            "successes": len(successful), "errors": len(rows) - len(successful),
            "wall_seconds_including_drain": elapsed,
            "success_qps": len(successful) / elapsed,
            "success_p50_s": percentile(successful, 0.5),
            "success_p95_s": percentile(successful, 0.95),
            "all_p95_s": percentile([row["latency_s"] for row in rows], 0.95),
            "status_counts": dict(collections.Counter(str(row["status"]) for row in rows)),
            "error_counts": dict(collections.Counter(row["error"]
                for row in rows if "error" in row)),
            "dataset_counts": dict(collections.Counter(row["dataset"]
                for row in rows if row["success"]))}


def stage_worker(args, tasks, deadline, state):
    client = connection(args)
    try:
        while time.monotonic() < deadline and not state["stop"].is_set():
            with state["lock"]:
                query = next(tasks, None)
            if query is None:
                break
            row = search_one(client, query, args.generation)
            with state["lock"]:
                state["rows"].append(row)
            if not row["success"] or row["latency_s"] >= 10:
                state["stop"].set()
    finally:
        client.close()


def run_stage(args, queries, concurrency):
    started = time.monotonic()
    state = {"rows": [], "lock": threading.Lock(), "stop": threading.Event()}
    tasks, deadline = iter(queries), started + args.seconds
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        jobs = [pool.submit(stage_worker, args, tasks, deadline, state)
                for _ in range(concurrency)]
        for job in jobs:
            job.result()
    result = stage_summary(state["rows"], time.monotonic() - started, concurrency)
    result.update(request_cap=len(queries), admission_target_seconds=args.seconds,
                  stopped_admission_early=state["stop"].is_set())
    return result, state["rows"]


def benchmark(args, queries):
    stages, rows, stopped = [], [], None
    for stage, concurrency in enumerate((4, 8, 16)):
        start, end = stage * len(queries) // 3, (stage + 1) * len(queries) // 3
        result, observations = run_stage(args, queries[start:end], concurrency)
        stages.append(result)
        rows.extend(observations)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["errors"] or result["stopped_admission_early"]:
            stopped = "Request failure or >=10s latency; escalation stopped"
            break
        if result["success_p95_s"] is None or result["success_p95_s"] >= args.stop_p95:
            stopped = "p95 threshold reached; escalation stopped"
            break
    return {"success": stopped is None, "stop_reason": stopped,
            "search_requests": len(rows), "stages": stages, "search_results": rows}


def run(args, queries, plan):
    result = {"started_utc": utc_now(), "plan": plan, "health_before": health(args)}
    if result["health_before"]["success"]:
        result["result"] = smoke(args) if args.mode == "smoke" else benchmark(args, queries)
    else:
        result["result"] = {"success": False, "search_requests": 0,
                            "stop_reason": "Initial health failed; no searches sent"}
    result["health_after"] = health(args)
    result["success"] = result["result"]["success"] and result["health_after"]["success"]
    result["ended_utc"] = utc_now()
    return result


def main():
    args = arguments()
    queries = query_pool(args) if args.mode == "benchmark" else []
    plan = {"mode": args.mode, "base_url": args.base_url, "generation": args.generation,
            "allowed_datasets": sorted(DATASETS), "query_offset": args.query_offset,
            "max_text_searches": 15 if args.mode == "smoke" else len(queries),
            "embedding_provider_calls": "May be paid; not directly measured",
            "cache_policy": "Unique meaningful queries within this run; avoid reusing offsets",
            "scope": "Local full-chain search; excludes public network and LLM generation"}
    print(json.dumps(plan, ensure_ascii=False), flush=True)
    if args.dry_run:
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w" if args.overwrite else "x", encoding="utf-8") as report:
        result = run(args, queries, plan)
        json.dump(result, report, ensure_ascii=False, indent=2)
        report.write("\n")
    print(json.dumps({"success": result["success"], "output": str(args.output),
                      "search_requests": result["result"]["search_requests"]}), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
