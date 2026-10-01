"""Credential-safe REST access and durable migration state."""

import contextlib
import fcntl
import http.client
import json
import os
from pathlib import Path
import ssl
import tempfile
import time
from urllib.parse import urlsplit


MAX_RESPONSE = 16 * 1024 * 1024
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


def endpoint(value, cloud=False):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Endpoint must contain only scheme, hostname and optional port")
    if cloud and (parsed.scheme != "https"
                  or not parsed.hostname.endswith(".cloud.qdrant.io")):
        raise ValueError("Target must be an HTTPS Qdrant Cloud endpoint")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = parsed.hostname.lower()
    canonical = f"{parsed.scheme}://{host}:{port}"
    return canonical, parsed.scheme, host, port


class Client:
    def __init__(self, url, key="", timeout=120):
        self.url, scheme, host, port = endpoint(url)
        kwargs = {"timeout": timeout}
        if scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        kind = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        self.connection = kind(host, port, **kwargs)
        self.headers = {"Content-Type": "application/json"}
        if key:
            self.headers["api-key"] = key

    def request(self, method, path, payload=None, missing=False):
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        for attempt in range(6):
            try:
                result, retry = self._attempt(method, path, body, missing)
                if not retry:
                    return result
            except (OSError, http.client.HTTPException):
                self.connection.close()
            if attempt < 5:
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"Qdrant {method} request failed after bounded retries")

    def _attempt(self, method, path, body, missing):
        self.connection.request(method, path, body, self.headers)
        response = self.connection.getresponse()
        content = response.read(MAX_RESPONSE + 1)
        if len(content) > MAX_RESPONSE:
            raise ValueError("Qdrant response exceeds the size limit")
        if response.status == 404 and missing:
            return None, False
        if response.status in RETRY_STATUSES:
            self.connection.close()
            return None, True
        if response.status != 200:
            raise RuntimeError(f"Qdrant {method} request rejected: HTTP {response.status}")
        document = json.loads(content)
        if method == "GET" and path == "/" and isinstance(document.get("version"), str):
            return document, False
        if document.get("status") != "ok" or "result" not in document:
            raise RuntimeError("Invalid Qdrant response envelope")
        return document["result"], False

    def close(self):
        self.connection.close()


def clients():
    target = os.environ.get("QDRANT_CLUSTER_ENDPOINT", "")
    key = os.environ.get("QDRANT_API_KEY", "")
    if not target or not key:
        raise ValueError("QDRANT_CLUSTER_ENDPOINT and QDRANT_API_KEY are required")
    endpoint(target, cloud=True)
    source = os.environ.get("QDRANT_SOURCE_ENDPOINT", "http://127.0.0.1:17333")
    _, scheme, host, _ = endpoint(source)
    if scheme != "http" or host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Source must be a loopback HTTP endpoint on RN")
    if endpoint(source)[0] == endpoint(target)[0]:
        raise ValueError("Source and target endpoints must differ")
    return Client(source), Client(target, key)


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".qdrant-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(document, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def checkpoint_lock(path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as error:
        raise RuntimeError("Another migration or verification holds the checkpoint lock") from error
    finally:
        os.close(descriptor)
