"""Sustained uncached C4/C16 stages plus bounded C32/C64 waves."""

import concurrent.futures
import http.client
import itertools
import json
import threading
import time

import benchmark as bench


TEMPLATES = [
    "Which classical verse describes {subject} {context}?",
    "I would like a traditional poem expressing {subject} {context}.",
    "Show me a poetic passage evoking {subject} {context} in an ancient landscape.",
    "Find evocative ancient verses on {subject} {context}, emphasizing human feeling.",
]
QUERIES = [
    template.format(subject=subject, context=context)
    for template, subject, context in itertools.product(
        TEMPLATES, bench.SUBJECTS, bench.CONTEXTS,
    )
]


def run_stage(concurrency, task_iter, duration, wave=False):
    before = len(bench.ROWS)
    started = time.monotonic()
    deadline = started + duration
    if wave:
        barrier = threading.Barrier(concurrency)
        tasks = [next(task_iter) for _ in range(concurrency)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            jobs = [pool.submit(send_wave, barrier, concurrency, task) for task in tasks]
            for job in jobs:
                job.result()
    else:
        tasks = iter(itertools.islice(task_iter, 600))
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            jobs = [pool.submit(bench.worker, deadline, concurrency, tasks)
                    for _ in range(concurrency)]
            for job in jobs:
                job.result()
    summary = bench.summarize(bench.ROWS[before:], started, time.monotonic(), concurrency)
    summary["mode"] = "single synchronized wave" if wave else "closed-loop sustained"
    summary["admission_seconds"] = duration if not wave else None
    summary["stage_started_s"] = started - bench.START
    print(json.dumps(summary), flush=True)
    return summary


def send_wave(barrier, concurrency, task):
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=12)
    index, query = task
    barrier.wait(timeout=10)
    try:
        return bench.request_one(connection, query, concurrency, index)
    finally:
        connection.close()


def save(data):
    data["raw_requests"] = sorted(bench.ROWS, key=lambda row: row["started_s"])
    data["docker_stats"] = bench.STATS
    (bench.ROOT / "extended.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def main():
    data = {"metadata": bench.metadata(), "stages": []}
    data["metadata"].update(
        method="uncached text; sustained C4/C16 then synchronized C32/C64 waves",
        stage_admission_seconds=25, stage_request_cap=600,
        planned_concurrency=[4, 16, 32, 64],
    )
    tasks = iter(enumerate(QUERIES, 390))
    done = threading.Event()
    monitor = threading.Thread(target=bench.stats_worker, args=(done,), daemon=True)
    monitor.start()
    try:
        for concurrency, duration, wave in [(4, 25, False), (16, 25, False),
                                            (32, 12, True), (64, 12, True)]:
            if bench.STOP.is_set():
                break
            summary = run_stage(concurrency, tasks, duration, wave)
            data["stages"].append(summary)
            save(data)
            if summary["errors"]:
                data["escalation_stopped"] = "Error observed; do not increase load"
                break
            time.sleep(2)
    finally:
        done.set()
        monitor.join(timeout=20)
        data["ended_utc"] = bench.utc_now()
        data["api_health_after"] = bench.json_get("/health")
        save(data)


if __name__ == "__main__":
    main()
