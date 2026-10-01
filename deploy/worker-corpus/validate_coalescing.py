"""Check identical cold requests against the actual Worker runtime."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import http.client
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bwg"))
from validate_api import DATASETS, response_row, search_valid


def cold_request(base, token, query, barrier):
    parsed = urlsplit(base)
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=12)
    try:
        connection.connect()
        barrier.wait(timeout=15)
        started = time.monotonic()
        connection.request("POST", parsed.path.rstrip("/") + "/search",
                           json.dumps({"query": query}), headers={
                               "Content-Type": "application/json",
                               "Authorization": "Bearer " + token,
                               "User-Agent": "poetry-api-deployment-validator/1.0",
                           })
        with connection.getresponse() as response:
            result = response_row(response, started)
            result["coalesced"] = response.headers.get("X-Poetry-Coalesced")
            return result
    except (OSError, http.client.HTTPException, threading.BrokenBarrierError) as error:
        return {"status": None, "error": type(error).__name__}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if urlsplit(args.base_url).scheme != "https" or not 2 <= args.concurrency <= 64:
        parser.error("require HTTPS and concurrency 2..64")
    if args.report.exists():
        parser.error("report exists")
    token = args.token_file.read_text().strip()
    query = "\u671b\u6708\u601d\u4e61 " + uuid.uuid4().hex
    barrier = threading.Barrier(args.concurrency)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(cold_request, args.base_url, token, query, barrier)
                   for _ in range(args.concurrency)]
        rows = [future.result() for future in futures]
    passed = sum(search_valid(row, DATASETS) for row in rows)
    joined = sum(row.get("coalesced") == "JOIN" for row in rows)
    bodies = {json.dumps(row.get("body"), sort_keys=True) for row in rows}
    report = {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "requests": len(rows), "passed": passed, "joined": joined,
              "same_response": len(bodies) == 1, "rows": rows,
              "success": passed == len(rows) and len(bodies) == 1 and joined > 0,
              "scope": "Cold synchronized burst; coalescing is local to each isolate"}
    with args.report.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({key: report[key] for key in
                      ("requests", "passed", "joined", "same_response", "success")}))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
