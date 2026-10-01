"""Validate and compare saved Globalping origin/proxy measurements locally."""

import argparse
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs


ROOT = Path(__file__).resolve().parent
ORIGIN_ADDRESS = "192.129.135.19"
ORIGIN_MARKER = "rn-poetry-network-20260922"
CACHED = {"HIT", "STALE", "UPDATING", "REVALIDATED"}
SIDES = ("direct", "proxy")
EXPECTED = [f"{side}-http-{n}" for n in range(1, 4) for side in SIDES]
EXPECTED += [f"{side}-tcp" for side in SIDES]


def read_json(path):
    return json.loads(path.read_text())


def node_key(probe):
    fields = ("country", "city", "asn", "network", "latitude", "longitude")
    return json.dumps([probe.get(key) for key in fields], ensure_ascii=False)


def distribution(values):
    values = sorted(value for value in values if isinstance(value, (int, float)))
    if not values:
        return {"count": 0, "median_ms": None, "p95_ms": None,
                "min_ms": None, "max_ms": None}
    return {"count": len(values), "median_ms": statistics.median(values),
            "p95_ms": values[math.ceil(len(values) * 0.95) - 1],
            "min_ms": values[0], "max_ms": values[-1]}


def normalized_headers(result):
    headers = result.get("headers") or {}
    return {key.lower(): str(value) for key, value in headers.items()}


def validate_http(result, payload, side):
    failures = []
    headers = normalized_headers(result)
    if (result.get("tls") or {}).get("authorized") is not True:
        failures.append("tls_not_authorized")
    if headers.get("x-origin-id") != ORIGIN_MARKER:
        failures.append("origin_header_mismatch")
    try:
        body = json.loads(result.get("rawBody") or "null")
    except (ValueError, TypeError):
        body = None
    query = payload["measurementOptions"]["request"].get("query", "")
    expected = parse_qs(query).get("nonce", [])
    if not isinstance(body, dict):
        failures.append("body_not_json_object")
    else:
        if body.get("origin") != ORIGIN_MARKER:
            failures.append("origin_body_mismatch")
        if len(expected) != 1 or body.get("nonce") != expected[0]:
            failures.append("nonce_mismatch")
        if "timestamp" not in body:
            failures.append("timestamp_missing")
    if "no-store" not in headers.get("cache-control", "").lower():
        failures.append("no_store_header_missing")
    address = result.get("resolvedAddress")
    if side == "proxy":
        if not headers.get("cf-ray"):
            failures.append("cf_ray_missing")
        if not address or address == ORIGIN_ADDRESS:
            failures.append("proxy_address_invalid")
        if headers.get("cf-cache-status", "").upper() in CACHED:
            failures.append("cached_response")
    elif address != ORIGIN_ADDRESS:
        failures.append("direct_address_mismatch")
    if not isinstance((result.get("timings") or {}).get("total"), (int, float)):
        failures.append("total_timing_missing")
    return failures


def http_sample(item, payload, side, name):
    result = item["result"]
    network_ok = result.get("status") == "finished" and result.get("statusCode") == 200
    failures = validate_http(result, payload, side) if network_ok else []
    headers = normalized_headers(result)
    ray = headers.get("cf-ray", "")
    return {"measurement": name, "side": side, "probe": item["probe"],
            "status": result.get("status"), "status_code": result.get("statusCode"),
            "success": network_ok and not failures,
            "network_http_error": not network_ok, "validation_failures": failures,
            "transport_error": result.get("status") != "finished",
            "http_error": result.get("statusCode") not in (None, 200),
            "timings_ms": result.get("timings") or {},
            "resolved_address": result.get("resolvedAddress"),
            "cf_colo": ray.rsplit("-", 1)[-1] if "-" in ray else None,
            "cache_status": headers.get("cf-cache-status"),
            "error": result.get("rawOutput") if not network_ok else None}


def load_measurements(root, partial):
    measurements, samples, tcp = {}, [], []
    missing = [name for name in EXPECTED if not (root / f"{name}.json").exists()]
    if missing and not partial:
        raise ValueError("Missing measurements: " + ", ".join(missing))
    for name in EXPECTED:
        if name in missing:
            continue
        data = read_json(root / f"{name}.json")
        payload = read_json(root / f"{name}.request.json")
        side = name.split("-", 1)[0]
        entries = data.get("results", [])
        keys = [node_key(item["probe"]) for item in entries]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Ambiguous duplicate probe metadata in {name}")
        if data.get("status") != "finished":
            raise ValueError(f"Measurement not finished: {name}")
        if data.get("probesCount") != len(entries):
            raise ValueError(f"Result count does not match probesCount: {name}")
        measurements[name] = {"id": data.get("id"), "created_at": data.get("createdAt"),
                              "updated_at": data.get("updatedAt"), "probes": len(entries),
                              "cohort_reuse": payload.get("locations")}
        if "-http-" in name:
            samples.extend(http_sample(item, payload, side, name) for item in entries)
        else:
            tcp.extend({"side": side, "measurement": name, **item} for item in entries)
    return measurements, samples, tcp, missing


def phase_timings(samples):
    phases = {phase: [] for phase in ("dns", "tcp", "tls", "firstByte", "download")}
    without_dns = []
    for sample in samples:
        timings = sample["timings_ms"]
        dns = timings.get("dns")
        if sample["side"] == "direct" and dns is None:
            dns = 0  # Direct target is a literal IP, so no target DNS lookup occurs.
        for phase in phases:
            phases[phase].append(dns if phase == "dns" else timings.get(phase))
        total = timings.get("total")
        if isinstance(total, (int, float)) and isinstance(dns, (int, float)):
            without_dns.append(total - dns)
    return {"phases": {name: distribution(values) for name, values in phases.items()},
            "total_minus_dns": distribution(without_dns)}


def summarize_http(samples):
    valid = [sample for sample in samples if sample["success"]]
    reasons = Counter(reason for sample in samples for reason in sample["validation_failures"])
    statuses = Counter(str(sample["status_code"]) for sample in samples)
    return {"attempts": len(samples), "successes": len(valid),
            "network_http_errors": sum(sample["network_http_error"] for sample in samples),
            "transport_errors": sum(sample["transport_error"] for sample in samples),
            "http_error_responses": sum(sample["http_error"] for sample in samples),
            "validation_error_responses": sum(bool(s["validation_failures"]) for s in samples),
            "validation_failure_reasons": dict(reasons), "http_status_counts": dict(statuses),
            "successful_latency": distribution(s["timings_ms"].get("total") for s in valid),
            "successful_timing_breakdown": phase_timings(valid),
            "all_observed_duration": distribution(s["timings_ms"].get("total") for s in samples),
            "cf_colos": sorted({s["cf_colo"] for s in samples if s["cf_colo"]}),
            "cache_status_counts": dict(Counter(str(s["cache_status"]) for s in samples))}


def summarize_tcp(entries):
    stats = [item["result"]["stats"] for item in entries if item["result"].get("stats")]
    total = sum(stat.get("total", 0) for stat in stats)
    received = sum(stat.get("rcv", 0) for stat in stats)
    dropped = sum(stat.get("drop", 0) for stat in stats)
    return {"probes": len(entries), "probes_without_stats": len(entries) - len(stats),
            "failed_probe_statuses": sum(i["result"].get("status") != "finished" for i in entries),
            "probes_with_drops": sum(stat.get("drop", 0) > 0 for stat in stats),
            "total_reported_attempts": total, "received": received, "dropped": dropped,
            "reported_failure_percent": 100 * dropped / total if total else None,
            "probe_mean_latency": distribution(stat.get("avg") for stat in stats),
            "successful_connection_latency": distribution(
                timing.get("rtt") for item in entries
                for timing in item["result"].get("timings", [])
            )}


def node_summaries(samples, tcp):
    probes = {node_key(s["probe"]): s["probe"] for s in samples + tcp}
    nodes = []
    for key, probe in sorted(probes.items()):
        node = {"probe": probe}
        for side in SIDES:
            matches = [s for s in samples if s["side"] == side and node_key(s["probe"]) == key]
            connections = [s for s in tcp if s["side"] == side and node_key(s["probe"]) == key]
            node[side] = {"http": summarize_http(matches), "tcp": summarize_tcp(connections)}
        complete = all(node[s]["http"]["attempts"] == 3 for s in SIDES)
        valid = complete and all(node[s]["http"]["successes"] == 3 for s in SIDES)
        node["paired_three_successful_rounds"] = valid
        if valid:
            direct = node["direct"]["http"]["successful_latency"]["median_ms"]
            proxy = node["proxy"]["http"]["successful_latency"]["median_ms"]
            node["proxy_minus_direct_median_ms"] = proxy - direct
            change = 100 * (proxy - direct) / direct if direct else None
            node["proxy_median_change_percent"] = change
        nodes.append(node)
    return nodes


def aggregate(samples, tcp, nodes):
    output = {}
    for region in ("overall", "asia"):
        keep = lambda probe: region == "overall" or probe.get("continent") == "AS"
        group = {side: {"http": summarize_http([s for s in samples if s["side"] == side
                                               and keep(s["probe"])]),
                        "tcp": summarize_tcp([s for s in tcp if s["side"] == side
                                              and keep(s["probe"])])} for side in SIDES}
        pairs = [node for node in nodes if node["paired_three_successful_rounds"]
                 and keep(node["probe"])]
        pair_keys = {node_key(node["probe"]) for node in pairs}
        paired_samples = [s for s in samples if node_key(s["probe"]) in pair_keys]
        deltas = [node["proxy_minus_direct_median_ms"] for node in pairs]
        group["paired_nodes"] = {"count": len(pairs),
                                 "proxy_faster": sum(value < 0 for value in deltas),
                                 "proxy_slower": sum(value > 0 for value in deltas),
                                 "equal": sum(value == 0 for value in deltas),
                                 "proxy_minus_direct_node_median_ms": distribution(deltas)}
        group["paired_http"] = {
            side: summarize_http([s for s in paired_samples if s["side"] == side])
            for side in SIDES
        }
        group["paired_rounds"] = {
            name: summarize_http([s for s in paired_samples if s["measurement"] == name])
            for name in EXPECTED if "-http-" in name
        }
        output[region] = group
    return output


def cohort_comparison(samples, tcp):
    cohorts = {}
    for sample in samples + tcp:
        cohorts.setdefault(sample["measurement"], set()).add(node_key(sample["probe"]))
    baseline = cohorts.get("direct-http-1", set())
    return {"baseline": "direct-http-1", "baseline_count": len(baseline),
            "all_measurements_match": all(keys == baseline for keys in cohorts.values()),
            "measurement_counts": {name: len(keys) for name, keys in cohorts.items()},
            "differences": {name: {"missing": sorted(baseline - keys),
                                    "extra": sorted(keys - baseline)}
                            for name, keys in cohorts.items() if keys != baseline}}


def http_cell(summary):
    latency = summary["successful_latency"]
    prefix = f'{summary["successes"]}/{summary["attempts"]}'
    failures = []
    if summary["transport_errors"]:
        failures.append(f'连接/握手失败 {summary["transport_errors"]} 次')
    for status, count in summary["http_status_counts"].items():
        if status not in ("200", "None"):
            failures.append(f"HTTP {status} ×{count}")
    if failures:
        prefix += "；" + "，".join(failures)
    if not latency["count"]:
        return prefix + "；无成功延迟"
    return (f'{prefix}；{latency["median_ms"]:g} '
            f'[{latency["min_ms"]:g}–{latency["max_ms"]:g}]')


def tcp_cell(summary):
    if not summary["probes"]:
        return "未测"
    total, drops = summary["total_reported_attempts"], summary["dropped"]
    means = summary["probe_mean_latency"]["median_ms"]
    if summary["probes_without_stats"]:
        return f'{drops}/{total}；{summary["probes_without_stats"]} 个探针缺统计'
    return f'{drops}/{total}；均值 {means:g}' if means is not None else f"{drops}/{total}；无成功"


def render_table(nodes, missing):
    lines = ["# RN 直连与 Cloudflare 代理：逐节点原始测量汇总", "",
             "HTTP 单元格：校验成功数/请求数；成功响应总耗时中位数 [最小–最大]，单位 ms。",
             "TCP 单元格：失败连接数/已报告尝试数；成功连接耗时均值，单位 ms。",
             "代理 TCP 仅测到 Cloudflare 边缘；HTTP 校验回源内容、TLS、随机 nonce 与禁用缓存。",
             "HTTP 403 是收到的拒绝响应，不计作网络丢包或连接超时。",
             "3 轮短时样本不代表长期 SLA、国家平均值或物理链路丢包率。", ""]
    if missing:
        lines.extend(["尚缺测量：" + ", ".join(missing), ""])
    lines.extend(["| 国家/城市/ASN | 直连 HTTP | 代理 HTTP | 直连 TCP | 代理 TCP | CF 机房 |",
                  "| --- | --- | --- | --- | --- | --- |"])
    for node in nodes:
        probe = node["probe"]
        label = f'{probe["country"]}/{probe["city"]}/AS{probe["asn"]}'
        direct, proxy = node["direct"], node["proxy"]
        cells = [label, http_cell(direct["http"]), http_cell(proxy["http"]),
                 tcp_cell(direct["tcp"]), tcp_cell(proxy["tcp"]),
                 ", ".join(proxy["http"]["cf_colos"]) or "—"]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def run(args):
    measurements, samples, tcp, missing = load_measurements(args.root, args.allow_partial)
    nodes = node_summaries(samples, tcp)
    rounds = {name: summarize_http([s for s in samples if s["measurement"] == name])
              for name in measurements if "-http-" in name}
    metrics = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "units": "milliseconds", "p95_method": "nearest rank",
               "missing_measurements": missing, "measurements": measurements,
               "cohort_comparison": cohort_comparison(samples, tcp),
               "aggregate": aggregate(samples, tcp, nodes), "rounds": rounds,
               "nodes": nodes, "http_samples": samples,
               "limits": ["Short samples, not SLA or country-representative estimates.",
                          "Successful latency excludes failures; counts retain all attempts.",
                          "Proxy TCP measures the edge, not the origin path.",
                          "Probe matching uses country/city/ASN/network/coordinates metadata.",
                          "Direct literal-IP target has no DNS lookup; DNS null becomes zero.",
                          "First-byte phase includes edge/security/origin work, not origin RTT.",
                          "Missing TCP stats remain unknown, not zero failures."]}
    (args.root / "proxy-metrics.json").write_text(json.dumps(metrics, ensure_ascii=False) + "\n")
    (args.root / "proxy-node-table.md").write_text(render_table(nodes, missing))
    print(json.dumps({"aggregate": metrics["aggregate"], "missing": missing}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--allow-partial", action="store_true")
    run(parser.parse_args())
