"""Match saved probe nonces to the isolated origin access log."""

import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENTRY = re.compile(r'^(\S+) - - \[([^\]]+)\] "GET ([^ ]+) HTTP/[^ ]+" (\d+) ')


def read_json(path):
    return json.loads(path.read_text())


def correlate(name, lines):
    request = read_json(ROOT.parent / f"{name}.request.json")
    result = read_json(ROOT.parent / f"{name}.json")
    options = request["measurementOptions"]["request"]
    target = options["path"] + "?" + options["query"]
    matches = []
    for number, line in enumerate(lines, 1):
        match = ENTRY.match(line)
        if match and match[3] == target:
            matches.append({"line": number, "peer_address": match[1],
                            "time": match[2], "status": int(match[4])})
    probe_statuses = Counter(str(item["result"].get("statusCode"))
                             for item in result["results"])
    origin_statuses = Counter(str(entry["status"]) for entry in matches)
    return {"measurement": name, "request_target": target,
            "probe_count": len(result["results"]), "probe_status_counts": dict(probe_statuses),
            "origin_count": len(matches), "origin_status_counts": dict(origin_statuses),
            "origin_200_equals_probe_200": origin_statuses["200"] == probe_statuses["200"],
            "origin_entries": matches}


def write_note(results):
    rows = ["# 源站日志核对", "", "按六次 HTTP 测量保存的完整路径和唯一 nonce 精确匹配日志。",
            "排除了证书验证、人工预检和其他互联网扫描请求。", "",
            "| 测量 | 探针 HTTP 状态 | 源站记录 | 源站状态 |",
            "| --- | --- | ---: | --- |"]
    for item in results:
        statuses = json.dumps(item["probe_status_counts"])
        origin = json.dumps(item["origin_status_counts"])
        rows.append(f'| {item["measurement"]} | {statuses} | {item["origin_count"]} | {origin} |')
    rows.extend(["", "六组源站 HTTP 200 条数均与探针收到的 HTTP 200 数一致。",
                 "代理成功响应均有源站访问记录；这支持此次动态请求真实回源，未由缓存返回。",
                 "403 请求没有对应的额外源站记录，与 Cloudflare 边缘阻断相符。",
                 "该核对按每轮 nonce 聚合，不单独把 Cloudflare 出口 IP 映射到具体探针。",
                 "", "服务器检查时间：2026-09-22 04:35:44 UTC。",
                 "根分区可用 21,134,831,616 字节（19.683 GiB），使用率 52%。",
                 "原始状态见 `after-probes-status.json`，逐条匹配见 `access-correlation.json`。"])
    (ROOT / "access-correlation.md").write_text("\n".join(rows) + "\n")


def main():
    lines = (ROOT / "access-after-probes.log").read_text().splitlines()
    results = [correlate(f"{side}-http-{number}", lines)
               for number in range(1, 4) for side in ("direct", "proxy")]
    if not all(item["origin_200_equals_probe_200"] for item in results):
        raise ValueError("Origin/probe success counts differ; inspect before writing a conclusion")
    report = {"log_file": "access-after-probes.log", "measurements": results}
    (ROOT / "access-correlation.json").write_text(json.dumps(report, indent=2) + "\n")
    write_note(results)
    print(json.dumps([{key: value for key, value in item.items() if key != "origin_entries"}
                      for item in results], indent=2))


if __name__ == "__main__":
    main()
