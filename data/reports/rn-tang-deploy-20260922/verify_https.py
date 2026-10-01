"""Check TLS, authentication, retrieval and detail via origin and Cloudflare."""

import http.client
import json
import socket
import ssl
import sys
from pathlib import Path


ROOT = Path("/opt/poetry-tang-20260922")
DOMAIN = "rn-proxy-test.anyveo.com"


class OriginConnection(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(("127.0.0.1", 443), timeout=self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=DOMAIN)


def request(client, method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    payload = json.dumps(body, ensure_ascii=False).encode() if body else None
    client.request(method, path, payload, headers)
    response = client.getresponse()
    content = response.read()
    row = {"status": response.status, "content_type": response.getheader("Content-Type"),
           "server": response.getheader("Server"), "cf_ray": response.getheader("CF-Ray")}
    try:
        row["payload"] = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        row["payload"] = None
    return row


def checks(kind, token):
    connection = OriginConnection if kind == "origin" else http.client.HTTPSConnection
    client = connection(DOMAIN, timeout=20)
    body = {"query": "想找写春天花开、溪水清澈的唐诗。"}
    result = {"route": kind}
    try:
        for name, credential in (("missing_token", None), ("wrong_token", "invalid"),
                                 ("valid_token", token)):
            result[name] = request(client, "POST", "/poetry/search", credential, body)
        search = result["valid_token"]
        if search["status"] == 200:
            path = "/poetry" + search["payload"]["poem"]["detail_url"]
            result["detail"] = request(client, "GET", path, token)
        result["network_check"] = request(client, "GET", "/rn-proxy-check?nonce=tang-deploy")
        result["success"] = (result["missing_token"]["status"] == 401
                             and result["wrong_token"]["status"] == 401
                             and search["status"] == 200
                             and result.get("detail", {}).get("status") == 200
                             and result["network_check"]["status"] == 200)
    except (OSError, http.client.HTTPException) as error:
        result.update(success=False, error=type(error).__name__)
    finally:
        client.close()
    return result


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "https"
    if not name.replace("-", "").isalnum():
        raise ValueError("Invalid report name")
    token = (ROOT / "client-token").read_text().strip()
    rows = [checks(kind, token) for kind in ("origin", "cloudflare")]
    with (ROOT / "evidence" / (name + ".json")).open("x") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)
    for row in rows:
        summary = {key: value.get("status") if isinstance(value, dict) else value
                   for key, value in row.items()}
        print(json.dumps(summary))
    if not rows[0]["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
