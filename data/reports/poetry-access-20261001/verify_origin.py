import http.client
import json
import socket
import ssl
import sys
import time

from common import paths, write_private


GENERATION = "poetry-20260921-v1"
HOST = "rn-proxy-test.anyveo.com"


class OriginHTTPS(http.client.HTTPSConnection):
    def connect(self):
        connection = socket.create_connection(("127.0.0.1", 443), self.timeout)
        self.sock = self._context.wrap_socket(connection, server_hostname=self.host)


def request(connection, method, path, payload=None, credential=None):
    headers = {"User-Agent": "DeepFocus-Authorization-Release/1.0"}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    connection.request(method, path, payload, headers)
    response = connection.getresponse()
    body = response.read(1_048_577)
    if len(body) > 1_048_576:
        raise RuntimeError("unexpectedly large API response")
    try:
        decoded = json.loads(body) if body else None
    except ValueError:
        decoded = None
    return response.status, response.headers, decoded


def wait_health(port):
    for _ in range(15):
        try:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=12)
            status, _, body = request(connection, "GET", "/health")
            connection.close()
            if status == 200 and body.get("generation") == GENERATION:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(1)
    raise RuntimeError("API health did not become ready")


def check_rejection(status, headers, body, code):
    return (status == 429 and isinstance(body, dict) and body.get("code") == code
            and body.get("retry_after", 0) > 0
            and headers.get("Retry-After") == str(body["retry_after"]))


def functional_checks(connection):
    checks = []
    status, headers, body = request(connection, "POST", "/search",
                                    json.dumps({"query": "moonlit homesickness"}))
    valid = (status == 200 and set(body or {}) == {"id", "original"}
             and headers.get("X-Poetry-Generation") == GENERATION)
    checks.append({"name": "compact_search", "status": status, "pass": valid,
                   "work_id": body.get("id") if isinstance(body, dict) else None})
    if not valid:
        raise RuntimeError("search contract failed")
    work_id = body["id"]
    status, _, body = request(connection, "GET", f"/poems/{work_id}?generation={GENERATION}")
    valid = (status == 200 and body.get("id") == work_id and isinstance(body.get("original"), list))
    checks.append({"name": "full_detail", "status": status, "pass": valid})
    status, _, _ = request(connection, "GET", f"/poems/{work_id}?generation=old")
    checks.append({"name": "stale_generation", "status": status, "pass": status == 404})
    return checks, work_id


def rate_checks(connection, work_id):
    checks = []
    time.sleep(2.1)
    search = [request(connection, "POST", "/search", "invalid JSON") for _ in range(6)]
    passed = (any(check_rejection(*result, "search_rate_exceeded") for result in search)
              and all(item[0] in (422, 429) for item in search))
    checks.append({"name": "search_rate", "statuses": [item[0] for item in search], "pass": passed})
    details = [request(connection, "GET", f"/poems/{work_id}") for _ in range(24)]
    passed = (any(check_rejection(*result, "detail_rate_exceeded") for result in details)
              and all(item[0] in (200, 429) for item in details))
    checks.append({"name": "cached_detail_rate", "statuses": [item[0] for item in details],
                   "pass": passed})
    return checks


def proxy_checks(alias, work_id):
    _, _, _, _, token_file = paths(alias)
    token = token_file.read_text().strip()
    checks = []
    connection = OriginHTTPS(HOST, timeout=15, context=ssl.create_default_context())
    for label, credential in (("missing", None), ("wrong", "invalid-release-token")):
        for path in ("/poetry/search", f"/poetry/poems/{work_id}"):
            method = "POST" if path.endswith("search") else "GET"
            payload = "invalid JSON" if method == "POST" else None
            status, _, _ = request(connection, method, path, payload, credential)
            checks.append({"name": f"proxy_{label}_{method}", "status": status,
                           "pass": status == 401})
    status, _, body = request(connection, "GET", f"/poetry/poems/{work_id}", credential=token)
    checks.append({"name": "proxy_authorized_detail", "status": status,
                   "pass": status == 200 and body.get("id") == work_id})
    connection.close()
    return checks


def main():
    alias, phase = sys.argv[1:3]
    port = 18081 if phase == "candidate" else 18080
    wait_health(port)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    checks, work_id = functional_checks(connection)
    if phase == "production":
        checks += proxy_checks(alias, work_id)
    checks += rate_checks(connection, work_id)
    connection.close()
    wait_health(port)
    result = {"alias": alias, "phase": phase, "checks": checks,
              "pass": all(x["pass"] for x in checks)}
    write_private(paths(alias)[1] / f"verification-{phase}.json", json.dumps(result))
    print(json.dumps(result, indent=2))
    if not result["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
