import copy
import json
import os
import shutil
import sys
from pathlib import Path

from common import RELEASE, compose, digest, env_values, paths, policy_for, run, write_private
from inspect_origin import inspect


def patched_environment(source, policy, address):
    replacements = dict(policy, HTTP_ADDR=address)
    lines = []
    for line in source.splitlines():
        key = line.partition("=")[0]
        lines.append(f"{key}={replacements.pop(key)}" if key in replacements else line)
    lines.extend(f"{key}={value}" for key, value in replacements.items())
    return "\n".join(lines) + "\n"


def prepare_backup(alias, checksum):
    root, release, config, binary, _ = paths(alias)
    staged = release / "vector-api-linux-amd64"
    if digest(staged) != checksum:
        raise RuntimeError("uploaded binary checksum mismatch")
    baseline = inspect(alias)
    if not all(baseline[key] for key in (
        "cloud_tls", "cloud_configured", "provider_credentials_present"
    )):
        raise RuntimeError("Cloud deployment prerequisites failed")
    backup = release / "backup"
    backup.mkdir(mode=0o700)
    for path in [config, binary]:
        shutil.copy2(path, backup / path.name)
    os.chmod(backup / config.name, 0o600)
    write_private(release / "baseline.json", json.dumps(baseline))
    candidate_binary = root / "bin" / f"vector-api-{RELEASE}"
    if candidate_binary.exists():
        raise RuntimeError("candidate binary path already exists")
    shutil.copy2(staged, candidate_binary)
    os.chmod(candidate_binary, 0o755)
    return baseline, candidate_binary


def stage_rn(candidate_binary):
    import yaml

    root, release, config, _, _ = paths("rn")
    original = yaml.safe_load(config.read_text())
    effective = json.loads(run(compose("rn") + ["config", "--format", "json"]))
    service = effective["services"]["vector-api"]
    if any(item.get("host_ip") != "127.0.0.1" for item in service["ports"]):
        raise RuntimeError("existing Docker API port is not loopback")
    policy = dict(policy_for(service["environment"]), HTTP_PRIVATE_CONTAINER="true")
    original["services"]["vector-api"]["environment"].update(policy)
    write_private(release / "new-compose.yaml", yaml.safe_dump(original, sort_keys=False))
    candidate = copy.deepcopy(service)
    candidate["environment"].update(policy)
    candidate["container_name"] = f"poetry-{RELEASE}-candidate"
    candidate["ports"] = [{"target": 8000, "published": "18081", "host_ip": "127.0.0.1"}]
    candidate.pop("depends_on", None)
    candidate.pop("networks", None)
    mounts = [item for item in candidate["volumes"]
              if item["target"] == "/usr/local/bin/vector-api"]
    if len(mounts) != 1:
        raise RuntimeError("unexpected binary mount contract")
    mounts[0]["source"] = str(candidate_binary)
    specification = {"name": candidate_project(), "services": {"vector-api": candidate}}
    write_private(release / "candidate.json", json.dumps(specification))
    run(compose("rn", release / "candidate.json", candidate_project()) +
        ["config", "--quiet"])
    run(compose("rn", release / "candidate.json", candidate_project()) +
        ["up", "-d", "--no-deps", "--pull", "never", "vector-api"])


def candidate_project():
    return f"poetry-{RELEASE}-candidate".lower()


def stage_bwg(candidate_binary):
    _, release, config, _, _ = paths("bwg")
    environment = env_values(config)
    if environment.get("HTTP_ADDR") != "127.0.0.1:18080":
        raise RuntimeError("native API is not configured for loopback")
    source = config.read_text()
    policy = policy_for(environment)
    write_private(release / "new-api.env", patched_environment(source, policy, "127.0.0.1:18080"))
    write_private(release / "candidate.env", patched_environment(source, policy, "127.0.0.1:18081"))
    run(["systemd-run", f"--unit={candidate_project()}", "--collect",
         "--property=User=poetry", "--property=Group=poetry",
         f"--property=EnvironmentFile={release / 'candidate.env'}",
         "--property=WorkingDirectory=/opt/poetry-tang",
         "--property=MemoryMax=192M", "--property=CPUQuota=50%",
         "--property=NoNewPrivileges=true", str(candidate_binary)])


def stop_candidate(alias):
    _, release, _, _, _ = paths(alias)
    if alias == "rn":
        run(compose(alias, release / "candidate.json", candidate_project()) +
            ["down", "--timeout", "15"])
    else:
        run(["systemctl", "stop", candidate_project()])


def replace_file(source, target):
    original = target.stat()
    temporary = target.with_name(target.name + f".{RELEASE}")
    if temporary.exists():
        raise RuntimeError("atomic replacement path already exists")
    shutil.copy2(source, temporary)
    os.chown(temporary, original.st_uid, original.st_gid)
    os.chmod(temporary, original.st_mode & 0o777)
    os.replace(temporary, target)


def restart(alias):
    if alias == "rn":
        run(compose(alias) + ["config", "--quiet"])
        run(compose(alias) + ["up", "-d", "--no-deps", "--force-recreate",
                             "--pull", "never", "vector-api"])
    else:
        run(["systemctl", "restart", "poetry-api.service"])


def switch(alias):
    from verify_origin import wait_health

    _, release, config, binary, _ = paths(alias)
    baseline = json.loads((release / "baseline.json").read_text())
    verified = json.loads((release / "verification-candidate.json").read_text())
    if not verified["pass"]:
        raise RuntimeError("candidate verification did not pass")
    for path in [config, binary]:
        if digest(path) != baseline["sha256"][str(path)]:
            raise RuntimeError("production file changed since staging")
    stop_candidate(alias)
    new_config = release / ("new-compose.yaml" if alias == "rn" else "new-api.env")
    try:
        replace_file(release / "vector-api-linux-amd64", binary)
        replace_file(new_config, config)
        restart(alias)
        wait_health(18080)
    except Exception:
        replace_file(release / "backup" / binary.name, binary)
        replace_file(release / "backup" / config.name, config)
        restart(alias)
        wait_health(18080)
        raise RuntimeError("release failed; original deployment restored") from None
    result = inspect(alias)
    write_private(release / "released.json", json.dumps(result))
    return result


def main():
    action, alias = sys.argv[1:3]
    if action == "stage":
        baseline, binary = prepare_backup(alias, sys.argv[3])
        stage_rn(binary) if alias == "rn" else stage_bwg(binary)
        result = {"alias": alias, "candidate": candidate_project(), "baseline": baseline,
                  "backup": str(paths(alias)[1] / "backup"), "sha256": digest(binary)}
    elif action == "switch":
        result = switch(alias)
    else:
        raise RuntimeError("unsupported release action")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
