import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PREVIOUS = ROOT.parents[1] / "rn-20260922/raw/icmp1.json"
API = "https://api.globalping.io/v1"
ORIGIN = "192.129.135.19"
SELECTED = [
    0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 14, 15, 16, 17, 20, 22,
    23, 25, 27, 29, 30, 31, 33, 35, 36, 38, 42, 44, 47, 48, 49,
]


def request(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        API + path, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Globalping HTTP {error.code}: {message}") from error


def locations():
    previous = json.loads(PREVIOUS.read_text())["results"]
    result = []
    for index in SELECTED:
        probe = previous[index]["probe"]
        result.append({"country": probe["country"], "city": probe["city"],
                       "asn": probe["asn"], "limit": 1})
    return result


def build_payload(args):
    if args.protocol == "HTTPS":
        kind = "http"
        options = {"protocol": "HTTPS", "port": 443, "request": {
            "host": args.hostname, "method": "GET", "path": args.path,
            "query": f"nonce={args.name}-{time.time_ns()}",
            "headers": {"Cache-Control": "no-cache", "Accept": "application/json"},
        }}
    else:
        kind = "ping"
        options = {"protocol": args.protocol, "packets": 16}
        if args.protocol == "TCP":
            options["port"] = 443
    payload = {"type": kind, "target": args.hostname if args.proxy else ORIGIN,
               "timeout": 15 if kind == "http" else 21,
               "locations": args.reuse or locations(), "measurementOptions": options}
    if args.reuse:
        payload["limit"] = len(SELECTED)
    return payload


def run(args):
    payload = build_payload(args)
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return
    destination = ROOT / f"{args.name}.json"
    if destination.exists():
        raise RuntimeError("Result already exists; choose a new measurement name")
    (ROOT / f"{args.name}.request.json").write_text(json.dumps(payload, indent=2))
    created = request("/measurements", payload)
    print(json.dumps(created), flush=True)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = request("/measurements/" + created["id"])
        destination.write_text(json.dumps(result, indent=2))
        if result["status"] == "finished":
            print(json.dumps({"name": args.name, "status": "finished",
                              "probes": result["probesCount"]}), flush=True)
            return
        time.sleep(4)
    raise RuntimeError("Measurement pending; resume by the saved ID")


parser = argparse.ArgumentParser()
parser.add_argument("name")
parser.add_argument("hostname")
parser.add_argument("--protocol", choices=["ICMP", "TCP", "HTTPS"], default="HTTPS")
parser.add_argument("--path", default="/rn-proxy-check")
parser.add_argument("--proxy", action="store_true")
parser.add_argument("--reuse")
parser.add_argument("--dry-run", action="store_true")
run(parser.parse_args())
