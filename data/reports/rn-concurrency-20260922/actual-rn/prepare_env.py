"""Copy only required provider settings into a private benchmark environment."""

import os
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[3]
KEYS = {"SILICONFLOW_KEY", "SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL",
        "SILICONFLOW_EMBED_MODEL", "EMBEDDING_PROFILE", "VECTOR_DIM",
        "EMBEDDING_TIMEOUT", "EMBEDDING_CONCURRENCY", "QDRANT_HNSW_EF"}


def main():
    selected = {}
    for line in (PROJECT / ".env.vector").read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in KEYS:
            tokens = shlex.split(value, comments=True)
            selected[key.strip()] = tokens[0] if tokens else ""
    if not (selected.get("SILICONFLOW_KEY") or selected.get("SILICONFLOW_API_KEY")):
        raise RuntimeError("Missing provider key in source environment")
    selected.update(HTTP_ADDR="127.0.0.1:18000", QDRANT_URL="http://127.0.0.1:16334",
                    SQLITE_DB_PATH="/data/chinese_poetry.db",
                    DATASETS_CONFIG_PATH="/data/datas.json",
                    COLLECTION_NAME="poetry_sentences_20260921_v1",
                    CORPUS_GENERATION="poetry-20260921-v1", RERANK_ENABLED="false")
    target = ROOT / "private.env"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("".join(f"{key}={value}\n" for key, value in selected.items()))
    print("Prepared private environment; no credentials printed.")
    print({key: value for key, value in selected.items() if "KEY" not in key})


if __name__ == "__main__":
    main()
