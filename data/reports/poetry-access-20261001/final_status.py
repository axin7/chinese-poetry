import http.client
import json
import sys

from common import paths, write_private
from inspect_origin import inspect
from verify_origin import GENERATION, HOST, request, wait_health


def verify(alias):
    wait_health(18080)
    connection = http.client.HTTPConnection("127.0.0.1", 18080, timeout=12)
    status, _, health = request(connection, "GET", "/health")
    connection.close()
    current = inspect(alias)
    released = json.loads((paths(alias)[1] / "released.json").read_text())
    checks = [
        {"name": "health", "status": status, "generation": health.get("generation"),
         "pass": status == 200 and health.get("generation") == GENERATION},
        {"name": "release_files_unchanged",
         "pass": current["sha256"] == released["sha256"]},
        {"name": "only_loopback_production_listener", "listeners": current["listeners"],
         "pass": len(current["listeners"]) == 2
         and "127.0.0.1:18080" in current["listeners"][1]},
        {"name": "cloud_tls_preserved",
         "pass": current["cloud_tls"] and current["cloud_configured"]},
    ]
    if alias == "rn":
        checks.append({"name": "unrelated_containers_unchanged",
                       "pass": current["containers"] == released["containers"]})
    else:
        connection = http.client.HTTPSConnection(HOST, timeout=15)
        status, _, marker = request(connection, "GET", "/rn-proxy-check")
        connection.close()
        checks.append({"name": "active_public_origin", "status": status,
                       "origin": marker.get("origin"),
                       "pass": status == 200 and marker.get("origin") == "bwg"})
    result = {"alias": alias, "checks": checks,
              "pass": all(check["pass"] for check in checks)}
    write_private(paths(alias)[1] / "verification-final.json", json.dumps(result))
    return result


if __name__ == "__main__":
    result = verify(sys.argv[1])
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["pass"] else 1)
