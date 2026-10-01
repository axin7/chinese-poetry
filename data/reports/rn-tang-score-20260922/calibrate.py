"""Collect small query-score samples without recording credentials."""

import http.client
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/opt/poetry-tang-20260922")
QUERIES = {
    "poetry_intent": [
        "月夜独自思念远方故乡和亲人",
        "春雨滋润大地，草木萌发，感受春天的生机",
        "送别好友，祝愿旅途平安，表达依依不舍",
        "远离官场，在山林田园中过安静闲适的生活",
        "登高望远，胸怀壮志，不畏艰难继续向前",
        "战争使百姓流离失所，思念在战场上的家人",
        "秋风萧瑟，落叶飘零，游子感到孤独",
        "珍惜光阴，不要虚度岁月，勉励认真读书",
        "冬日大雪覆盖山川，一叶孤舟独自垂钓",
        "久别重逢，与老朋友饮酒畅谈往事",
        "人生变迁，繁华易逝，感慨世事无常",
        "黄河长江奔流不息，感叹山河辽阔壮美",
    ],
    "clearly_unrelated": [
        "如何修复 Kubernetes 集群中 kubelet 的证书过期错误",
        "解释 PostgreSQL VACUUM 与 MVCC 的实现机制",
        "请给出 OAuth2 PKCE code_verifier 的 SHA256 计算代码",
        "比较 RTX 5090 和 H100 的显存带宽与 CUDA 核心数量",
        "二甲双胍缓释片的药代动力学参数和临床禁忌症",
        "计算三相异步电动机的额定转矩和转差率",
        "美国上市公司季度财报的递延所得税计算公式",
        "如何通过 CSS grid-template-columns 实现自适应布局",
        "JSON Web Token 的 RS256 签名验证失败怎么办",
        "给出波音 787 发动机控制系统的故障诊断流程",
        "使用 Wireshark 分析 TCP 三次握手重传数据包",
        "编写 Rust 程序解析 Parquet 文件并导出 Arrow RecordBatch",
    ],
}


def query(client, group, text):
    started = time.perf_counter()
    client.request("POST", "/search", json.dumps({"query": text}).encode(),
                   {"Content-Type": "application/json"})
    response = client.getresponse()
    payload = json.loads(response.read())
    result = {"group": group, "query": text, "status": response.status,
              "seconds": round(time.perf_counter() - started, 4)}
    if response.status != 200:
        return result
    score = payload["match"].get("score")
    if not isinstance(score, (int, float)) or not math.isfinite(score):
        raise ValueError("Response score is not a finite number")
    result.update(score=score, work_id=payload["poem"]["id"],
                  title=payload["poem"]["title"], original=payload["match"]["original"],
                  translation=payload["match"]["translation"])
    return result


def summarize(rows):
    summary = {}
    for group in QUERIES:
        values = [row["score"] for row in rows if row["group"] == group and "score" in row]
        summary[group] = {"count": len(values), "minimum": min(values),
                          "median": statistics.median(values), "maximum": max(values)}
    return summary


def main():
    rows = []
    client = http.client.HTTPConnection("127.0.0.1", 18080, timeout=15)
    try:
        for group, queries in QUERIES.items():
            for text in queries:
                rows.append(query(client, group, text))
    finally:
        client.close()
    report = {"at": datetime.now(timezone.utc).isoformat(), "rows": rows,
              "summary": summarize(rows)}
    with (ROOT / "evidence" / "score-calibration-20260922.json").open("x") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"at": report["at"], "summary": report["summary"]}))
    if any(row["status"] != 200 for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
