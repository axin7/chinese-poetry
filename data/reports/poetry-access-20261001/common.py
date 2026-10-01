import hashlib
import os
import shlex
import subprocess
from pathlib import Path


RELEASE = "access-20261001T024128Z"
ROOTS = {"rn": Path("/opt/poetry-tang-20260922"), "bwg": Path("/opt/poetry-tang")}
POLICY = {
    "HTTP_CONCURRENCY": "64", "SEARCH_REQUESTS_PER_SECOND": "2", "SEARCH_BURST": "4",
    "DETAIL_REQUESTS_PER_SECOND": "10", "DETAIL_BURST": "20", "DETAIL_CONCURRENCY": "8",
    "DETAIL_CACHE_BYTES": "8388608", "DETAIL_CACHE_TTL": "3600",
    "EMBEDDING_REQUESTS_PER_MINUTE": "120",
}
PUBLIC_KEYS = set(POLICY) | {
    "HTTP_ADDR", "HTTP_PRIVATE_CONTAINER", "SEARCH_CONCURRENCY", "EMBEDDING_CONCURRENCY",
    "CORPUS_GENERATION", "COLLECTION_NAME", "EMBEDDING_PROFILE", "RERANK_ENABLED",
}


def run(arguments, timeout=60):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command failed: {arguments[0]} exit={result.returncode}")
    return result.stdout


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            result.update(chunk)
    return result.hexdigest()


def env_values(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = shlex.split(line, comments=False)
        if len(parts) != 1 or "=" not in parts[0]:
            raise RuntimeError("unsupported environment file syntax")
        key, value = parts[0].split("=", 1)
        values[key] = value
    return values


def paths(alias):
    root = ROOTS[alias]
    config = root / "compose.cloud.yaml" if alias == "rn" else Path("/etc/poetry/vector-api.env")
    binary = root / "bin" / ("vector-api-cloud" if alias == "rn" else "vector-api")
    token = root / "client-token" if alias == "rn" else Path("/etc/poetry/client-token")
    return root, root / RELEASE, config, binary, token


def compose(alias, config=None, project=None):
    root, _, original, _, _ = paths(alias)
    arguments = ["docker", "compose", "-f", str(config or original)]
    if project:
        arguments += ["-p", project]
    arguments += ["--env-file", str(root / ".env"), "--env-file", str(root / ".env.cloud")]
    return arguments


def write_private(path, content):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as target:
        target.write(content)


def policy_for(environment):
    result = dict(POLICY)
    result["EMBEDDING_REQUESTS_PER_MINUTE"] = str(min(
        120, int(environment.get("EMBEDDING_REQUESTS_PER_MINUTE", "120"))
    ))
    if int(result["EMBEDDING_REQUESTS_PER_MINUTE"]) <= 0:
        raise RuntimeError("invalid existing embedding budget")
    return result
