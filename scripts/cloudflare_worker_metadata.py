"""Verify safe live metadata for the private Qdrant gateway experiment."""

import argparse
import datetime as dt
import json
from pathlib import Path
import urllib.error
import urllib.request

from cloudflare_worker_metrics import ACCOUNT_ID, REPORT_DIR, get_token


BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/workers/scripts"
MAIN = "poetry-rust-lab-20260930"
GATEWAY = "poetry-qdrant-gateway-20260930"
APPROVED_VARS = {
    "API_COLLECTION_NAME", "COLLECTION_NAME", "EMBEDDING_MODEL", "GENERATION",
    "ORIGIN_ID", "QDRANT_GATEWAY_ONLY",
}


def fetch_metadata(worker, suffix):
    request = urllib.request.Request(
        f"{BASE}/{worker}/{suffix}",
        headers={"Authorization": "Bearer " + get_token()},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Metadata request rejected: HTTP {error.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("Cloudflare metadata connection failed") from None
    if not body.get("success") or not isinstance(body.get("result"), dict):
        raise RuntimeError("Cloudflare metadata returned an unexpected result")
    return body["result"]


def bindings(settings):
    records = settings.get("bindings", [])
    names = [{"name": item["name"], "type": item["type"]} for item in records]
    variables = {
        item["name"]: item.get("text") for item in records
        if item["type"] == "plain_text" and item["name"] in APPROVED_VARS
    }
    services = [
        {key: item[key] for key in ("name", "service", "environment") if key in item}
        for item in records if item["type"] == "service"
    ]
    return {
        "bindings": sorted(names, key=lambda item: item["name"]),
        "approved_nonsecret_vars": variables,
        "service_bindings": services,
        "d1_binding_present": any(item["type"] == "d1" for item in records),
    }


def latest_deployment(deployments):
    records = deployments.get("deployments", [])
    if not records:
        raise RuntimeError("No live Worker deployment metadata is available")
    latest = records[0]
    return {
        "id": latest["id"], "created_on": latest.get("created_on"),
        "versions": [
            {key: version[key] for key in ("version_id", "percentage") if key in version}
            for version in latest.get("versions", [])
        ],
    }


def inspect_worker(worker, expected_version):
    settings = fetch_metadata(worker, "settings")
    deployment = latest_deployment(fetch_metadata(worker, "deployments"))
    subdomain = fetch_metadata(worker, "subdomain")
    live_versions = [version["version_id"] for version in deployment["versions"]]
    placement = settings.get("placement") or {"mode": "off"}
    return {
        "worker": worker, **bindings(settings),
        "compatibility_date": settings.get("compatibility_date"),
        "placement": placement, "latest_deployment": deployment,
        "expected_version": expected_version,
        "expected_version_matches": expected_version in live_versions,
        "workers_dev_enabled": subdomain.get("enabled"),
        "preview_urls_enabled": subdomain.get("previews_enabled"),
    }


def checks(main, gateway, origin):
    vars_main = main["approved_nonsecret_vars"]
    vars_gateway = gateway["approved_nonsecret_vars"]
    bound = any(
        item.get("name") == "QDRANT_GATEWAY" and item.get("service") == GATEWAY
        for item in main["service_bindings"]
    )
    return {
        "expected_versions_match": all(item["expected_version_matches"]
            for item in (main, gateway)),
        "no_d1_bindings": not any(item["d1_binding_present"] for item in (main, gateway)),
        "main_origin_matches": vars_main.get("ORIGIN_ID") == origin,
        "main_placement_off": main["placement"].get("mode") == "off",
        "main_gateway_binding_matches": bound,
        "gateway_placement_targeted": gateway["placement"].get("mode") == "targeted",
        "gateway_workers_dev_disabled": gateway["workers_dev_enabled"] is False,
        "gateway_preview_urls_disabled": gateway["preview_urls_enabled"] is False,
        "gateway_marker_matches": vars_gateway.get("QDRANT_GATEWAY_ONLY") == "true",
        "collections_match": all(vars_main.get(key) == vars_gateway.get(key)
            for key in ("COLLECTION_NAME", "API_COLLECTION_NAME")),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-version", required=True)
    parser.add_argument("--gateway-version", required=True)
    parser.add_argument("--origin", default="cf-poetry-rust-qdrant-gateway-20260930")
    parser.add_argument("--filename", default="deployment-gateway-final.json")
    args = parser.parse_args()
    if Path(args.filename).name != args.filename or not args.filename.endswith(".json"):
        raise ValueError("Report filename must be a plain JSON filename")
    main_worker = inspect_worker(MAIN, args.main_version)
    gateway = inspect_worker(GATEWAY, args.gateway_version)
    verification = checks(main_worker, gateway, args.origin)
    document = {
        "collected_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "read_only": True, "source": "Official Cloudflare Workers metadata APIs",
        "workers": [main_worker, gateway], "checks": verification,
        "secret_values_and_qdrant_endpoint_redacted": True,
        "d1_data_reads_or_writes_performed": 0, "production_dns_operations_performed": 0,
        "limitations": [
            "Targeted placement exposes a numeric target, not the original region string.",
            "The numeric target region mapping is not independently verified.",
            "No Worker invocation or external gateway URL probe was performed.",
            "Subdomain flags do not independently prove absence of every custom route.",
        ],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    target = REPORT_DIR / args.filename
    target.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"report": str(target), "checks": verification}, indent=2))
    if not all(verification.values()):
        raise RuntimeError("One or more live metadata checks failed; see the safe report")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
