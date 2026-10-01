"""A 20-second, 4.5-request/second uncached local API smoke test."""

import concurrent.futures
import http.client
import json
import threading
import time

import benchmark as bench


LOCAL = threading.local()
ACTIVE = 0
PEAK = 0


def send(index, query, scheduled):
    global ACTIVE, PEAK
    if not hasattr(LOCAL, "connection"):
        LOCAL.connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=12)
    with bench.LOCK:
        ACTIVE += 1
        PEAK = max(ACTIVE, PEAK)
    try:
        row = bench.request_one(LOCAL.connection, query, 8, index)
        row["scheduled_s"] = scheduled - bench.START
        row["generator_delay_s"] = row["started_s"] - row["scheduled_s"]
        return row
    finally:
        with bench.LOCK:
            ACTIVE -= 1


def main():
    metadata = bench.metadata()
    metadata.update(method="fixed arrival rate 4.5 requests/second; uncached text",
                    stage_request_cap=90, planned_concurrency=[8])
    started = time.monotonic()
    done = threading.Event()
    monitor = threading.Thread(target=bench.stats_worker, args=(done,), daemon=True)
    monitor.start()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = []
        for offset, query in enumerate(bench.QUERIES[300:390]):
            scheduled = started + offset / 4.5
            time.sleep(max(0, scheduled - time.monotonic()))
            if bench.STOP.is_set():
                break
            jobs.append(pool.submit(send, 300 + offset, query, scheduled))
        for job in jobs:
            job.result()
    stopped = time.monotonic()
    done.set()
    monitor.join(timeout=20)
    data = {"metadata": metadata, "offered_rps": 4.5, "admission_seconds": 20,
            "peak_in_flight": PEAK, "summary": bench.summarize(bench.ROWS, started, stopped, 8),
            "raw_requests": bench.ROWS, "docker_stats": bench.STATS,
            "api_health_after": bench.json_get("/health"), "ended_utc": bench.utc_now()}
    details = []
    for row in bench.ROWS[::30]:
        if not row["success"]:
            continue
        payload = bench.json_get(row["response"]["poem"]["detail_url"])
        assert payload["original"] and payload["translation"]
        assert len(payload["original"]) == len(payload["translation"])
        details.append(payload)
    data["full_work_checks"] = details
    (bench.ROOT / "steady.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({"summary": data["summary"], "peak_in_flight": PEAK,
                      "full_work_checks": len(details)}), flush=True)


if __name__ == "__main__":
    main()
