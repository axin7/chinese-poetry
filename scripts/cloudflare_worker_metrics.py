"""Collect read-only Cloudflare analytics for the Qdrant Worker experiment."""

import argparse
import datetime as dt
import json
from pathlib import Path
import tomllib
import urllib.error
import urllib.request


ACCOUNT_ID = "41c7cfec58dfb5e692cf7588a4e4c556"
WORKER_NAME = "poetry-rust-lab-20260930"
WORKER_NAMES = (WORKER_NAME, "poetry-qdrant-gateway-20260930")
API = "https://api.cloudflare.com/client/v4/graphql"
REPORT_DIR = Path(__file__).resolve().parents[1] / "data/reports/worker-qdrant-20260930"
QUERY = """query WorkerMetrics(
  $accountTag: string!, $scriptName: string!,
  $start: string!, $end: string!, $date: string!
) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      interval: workersInvocationsAdaptive(limit: 20, filter: {
        scriptName: $scriptName, datetime_geq: $start, datetime_leq: $end
      }) {
        dimensions { status }
        sum { requests errors }
        quantiles { cpuTimeP50 cpuTimeP95 cpuTimeP99 }
      }
      accountDaily: workersInvocationsAdaptive(limit: 10, filter: {date: $date}) {
        sum { requests errors }
      }
      scriptDaily: workersInvocationsAdaptive(limit: 10, filter: {
        date: $date, scriptName: $scriptName
      }) {
        sum { requests errors }
      }
    }
  }
}"""


def utc_time(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta():
        raise ValueError("Benchmark boundaries must explicitly use UTC")
    return parsed


def get_token():
    path = Path.home() / "Library/Preferences/.wrangler/config/default.toml"
    with path.open("rb") as handle:
        credential = tomllib.load(handle)
    token = credential.get("oauth_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Private Wrangler OAuth credential is unavailable")
    return token


def fetch_analytics(variables):
    request = urllib.request.Request(
        API,
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={"Authorization": "Bearer " + get_token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
            status = response.status
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Cloudflare GraphQL rejected request: HTTP {error.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("Cloudflare GraphQL connection failed") from None
    if result.get("errors"):
        raise RuntimeError("Cloudflare GraphQL returned errors; report was not written")
    accounts = result.get("data", {}).get("viewer", {}).get("accounts", [])
    if len(accounts) != 1:
        raise RuntimeError("Cloudflare GraphQL returned an unexpected account result")
    return status, result, accounts[0]


def totals(records):
    if not records:
        return {"available": False, "requests_estimated": None, "errors_estimated": None}
    return {
        "available": True,
        "requests_estimated": sum(record["sum"]["requests"] for record in records),
        "errors_estimated": sum(record["sum"]["errors"] for record in records),
    }


def interval_metrics(records):
    statuses = []
    for record in records:
        quantiles = record["quantiles"]
        cpu = {
            name: quantiles[field] / 1000 if quantiles.get(field) is not None else None
            for name, field in (
                ("p50", "cpuTimeP50"), ("p95", "cpuTimeP95"), ("p99", "cpuTimeP99")
            )
        }
        statuses.append({
            "status": record["dimensions"]["status"],
            "requests_estimated": record["sum"]["requests"],
            "runtime_errors_estimated": record["sum"]["errors"],
            "cpu_ms": cpu,
        })
    observed = [record["status"] for record in statuses]
    return {
        **totals(records), "statuses": statuses,
        "exceeded_cpu_status_observed": "exceededCpu" in observed if records else None,
        "exceeded_memory_status_observed": "exceededMemory" in observed if records else None,
    }


def report(status, response, account, variables):
    daily = totals(account["accountDaily"])
    requests = daily["requests_estimated"]
    next_day = dt.date.fromisoformat(variables["date"]) + dt.timedelta(days=1)
    return {
        "provider": "Cloudflare", "account_id": ACCOUNT_ID,
        "worker": variables["scriptName"],
        "api": API, "read_only": True, "http_status": status,
        "collected_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "requested_interval_utc": {"start": variables["start"], "end": variables["end"]},
        "interval_bounds": "inclusive datetime_geq and datetime_leq",
        "cpu_units": {"official": "microseconds", "derived": "milliseconds", "divisor": 1000},
        "query": QUERY, "variables": variables, "official_response": response,
        "interval": interval_metrics(account["interval"]),
        "daily_quota": {
            "utc_date": variables["date"], "free_requests": 100000, **daily,
            "count_basis": "workersInvocationsAdaptive account invocation estimates",
            "billable_external_request_equivalence_verified": False,
            "remaining_requests_estimated": max(0, 100000 - requests)
            if requests is not None else None,
            "script_daily": totals(account["scriptDaily"]),
            "next_reset_utc": f"{next_day.isoformat()}T00:00:00Z",
        },
        "limitations": [
            "Adaptive sampling makes counts and CPU quantiles estimates.",
            "Analytics ingestion can lag, especially for requests near the interval end.",
            "Application HTTP 503 responses can have success status and zero runtime errors.",
            "CPU quantiles are per invocation status and must not be averaged across statuses.",
            "No observed exceededCpu status does not prove compliance with the Free CPU limit.",
            "Daily quota is shared by the account and can change after collection.",
            "Account invocations may include internal Service Binding calls.",
            "Remaining requests are conservative analytics headroom, not a billing counter.",
            "Empty analytics records mean unavailable evidence, not confirmed zero usage.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Inclusive benchmark start in UTC")
    parser.add_argument("--end", required=True, help="Inclusive benchmark end in UTC")
    parser.add_argument("--filename", default="cloudflare-metrics.json")
    parser.add_argument("--worker", choices=WORKER_NAMES, default=WORKER_NAME)
    args = parser.parse_args()
    start, end = utc_time(args.start), utc_time(args.end)
    if start >= end:
        raise ValueError("Benchmark end must be later than its start")
    if Path(args.filename).name != args.filename or not args.filename.endswith(".json"):
        raise ValueError("Report filename must be a plain JSON filename")
    variables = {
        "accountTag": ACCOUNT_ID, "scriptName": args.worker,
        "start": args.start, "end": args.end,
        "date": dt.datetime.now(dt.timezone.utc).date().isoformat(),
    }
    status, response, account = fetch_analytics(variables)
    document = report(status, response, account, variables)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / args.filename
    target.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"report": str(target), "interval": document["interval"],
        "daily_quota": document["daily_quota"]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
