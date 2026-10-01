import json
import os
import sys

from common import PUBLIC_KEYS, compose, digest, env_values, paths, run


def inspect(alias):
    root, _, config, binary, token = paths(alias)
    files = [binary, config, root / "corpus/chinese_poetry.db", root / "corpus/datas.json"]
    result = {"alias": alias, "root": str(root), "sha256": {str(p): digest(p) for p in files}}
    if alias == "rn":
        effective = json.loads(run(compose(alias) + ["config", "--format", "json"]))
        service = effective["services"]["vector-api"]
        environment = service["environment"]
        result["ports"] = service["ports"]
        result["mounts"] = service["volumes"]
        result["compose_name"] = effective["name"]
        ids = run(["docker", "ps", "-q"]).split()
        containers = json.loads(run(["docker", "inspect", *ids]))
        result["containers"] = [
            {"id": item["Id"], "name": item["Name"], "started": item["State"]["StartedAt"]}
            for item in containers
        ]
    else:
        environment = env_values(config)
        result["service"] = run(["systemctl", "show", "poetry-api.service",
                                  "--property=MainPID,ActiveState,SubState"]).splitlines()
    result["settings"] = {key: environment[key] for key in PUBLIC_KEYS if key in environment}
    result["cloud_tls"] = environment.get("QDRANT_TLS") == "true"
    result["cloud_configured"] = bool(environment.get("QDRANT_CLUSTER_ENDPOINT"))
    result["provider_credentials_present"] = all(environment.get(key) for key in (
        "SILICONFLOW_API_KEY", "QDRANT_API_KEY"
    ))
    result["token_file_mode"] = oct(os.stat(token).st_mode & 0o777)
    result["listeners"] = run(["ss", "-ltnp", "( sport = :18080 or sport = :18081 )"]).splitlines()
    return result


if __name__ == "__main__":
    print(json.dumps(inspect(sys.argv[1]), indent=2))
