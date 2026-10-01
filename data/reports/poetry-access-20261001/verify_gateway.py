import concurrent.futures
import http.client
import json
import ssl
import sys
import time

from common import paths, write_private
from verify_origin import HOST, OriginHTTPS, check_rejection, request


def connect(public):
    factory = http.client.HTTPSConnection if public else OriginHTTPS
    return factory(HOST, timeout=15, context=ssl.create_default_context())


def burst_request(public, token):
    connection = connect(public)
    try:
        return request(connection, "POST", "/poetry/search", "invalid JSON", token)
    finally:
        connection.close()


def verify(alias, public):
    token = paths(alias)[4].read_text().strip()
    time.sleep(2.1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: burst_request(public, token), range(6)))
    rejected = [result for result in responses
                if check_rejection(*result, "search_rate_exceeded")]
    checks = [{"name": "https_search_rate", "statuses": [item[0] for item in responses],
               "pass": bool(rejected) and all(item[0] in (422, 429) for item in responses)}]
    if rejected:
        checks[0]["rejection"] = rejected[0][2]
        checks[0]["retry_after_header"] = rejected[0][1].get("Retry-After")
        checks[0]["origin_id"] = rejected[0][1].get("X-Origin-ID")
    connection = connect(public)
    path = "/poetry/poems/tangshisanbaishou:1"
    for label, credential, expected in (("missing", None, 401),
                                         ("wrong", "invalid-release-token", 401),
                                         ("authorized", token, 200)):
        status, _, _ = request(connection, "GET", path, credential=credential)
        checks.append({"name": f"https_detail_{label}", "status": status,
                       "pass": status == expected})
    connection.close()
    result = {"alias": alias, "public": public, "checks": checks,
              "pass": all(check["pass"] for check in checks)}
    suffix = "public-rechecked" if public else "gateway"
    write_private(paths(alias)[1] / f"verification-{suffix}.json", json.dumps(result))
    return result


if __name__ == "__main__":
    result = verify(sys.argv[1], len(sys.argv) > 2 and sys.argv[2] == "public")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["pass"] else 1)
