#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077

TARGET=/opt/poetry-tang
CONFIG=/etc/poetry
UNIT=/etc/systemd/system/poetry-api.service
TOOLCHAIN=/opt/poetry-toolchains/go1.25.5
GO_SHA256=9e9b755d63b36acf30c12a9a3fc379243714c1c6d3dd72861da637f336ebb35b
PHASE=preflight
WORK=

fail() {
    printf 'Installation stopped during %s: %s\n' "$PHASE" "$1" >&2
    exit 1
}

cleanup() {
    if [[ -n "$WORK" && "$WORK" == /var/tmp/poetry-install.* ]]; then
        rm -rf -- "$WORK"
    fi
}

trim() {
    local value=$1
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

literal_value() {
    local value
    value=$(trim "$1")
    if [[ "${value:0:1}" == "'" || "${value:0:1}" == '"' ]]; then
        [[ ${#value} -ge 2 && "${value: -1}" == "${value:0:1}" ]] || return 1
        value=${value:1:${#value}-2}
    fi
    [[ -n "$value" && "$value" =~ ^[A-Za-z0-9._:/-]+$ ]] || return 1
    printf '%s' "$value"
}

load_credentials() {
    local file line key value previous
    SILICONFLOW_API_KEY=
    QDRANT_API_KEY=
    QDRANT_CLUSTER_ENDPOINT=
    for file in "$STAGE/.env" "$STAGE/.env.cloud"; do
        while IFS= read -r line || [[ -n "$line" ]]; do
            [[ "$line" =~ ^[[:space:]]*([A-Z_][A-Z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
            key=${BASH_REMATCH[1]}
            case "$key" in
                SILICONFLOW_API_KEY|QDRANT_API_KEY|QDRANT_CLUSTER_ENDPOINT) ;;
                *) continue ;;
            esac
            value=$(literal_value "${BASH_REMATCH[2]}") || fail 'Unsupported credential syntax'
            previous=${!key}
            [[ -z "$previous" || "$previous" == "$value" ]] || fail 'Conflicting credentials'
            printf -v "$key" '%s' "$value"
        done < "$file"
    done
    [[ -n "$SILICONFLOW_API_KEY" && -n "$QDRANT_API_KEY" ]] || fail 'Missing provider keys'
    [[ "$SILICONFLOW_API_KEY" =~ ^[A-Za-z0-9._-]+$ ]] || fail 'Invalid provider key'
    [[ "$QDRANT_API_KEY" =~ ^[A-Za-z0-9._-]+$ ]] || fail 'Invalid Qdrant key'
    [[ "$QDRANT_CLUSTER_ENDPOINT" =~ ^https://([A-Za-z0-9.-]+)(:[0-9]{1,5})?/?$ ]] \
        || fail 'Invalid Cloud HTTPS endpoint'
    [[ "${BASH_REMATCH[1]}" == *.cloud.qdrant.io ]] || fail 'Unexpected Cloud endpoint host'
}

check_stage() {
    local name token
    for name in source/go.mod source/go.sum source/deploy/bwg/poetry-api.service \
        source/deploy/bwg/vector-api.env.example corpus/chinese_poetry.db \
        corpus/datas.json .env .env.cloud client-token; do
        [[ -f "$STAGE/$name" && ! -L "$STAGE/$name" && -s "$STAGE/$name" ]] \
            || fail "Missing or unsafe staged file: $name"
    done
    [[ -d "$STAGE/source/cmd/vector-api" && -d "$STAGE/source/internal" ]] \
        || fail 'Missing application source directories'
    load_credentials
    token=$(trim "$(< "$STAGE/client-token")")
    [[ "$token" =~ ^[A-Za-z0-9._~-]+$ ]] || fail 'Invalid client token'
}

check_host() {
    [[ $EUID -eq 0 ]] || fail 'Run as root'
    [[ -f /etc/os-release ]] || fail 'Missing operating system identification'
    . /etc/os-release
    [[ "$ID" == debian || "$ID" == ubuntu ]] || fail 'Only Debian and Ubuntu are supported'
    [[ -d /run/systemd/system ]] || fail 'Native systemd is required'
    local path
    for path in "$TARGET" "$CONFIG" "$UNIT"; do
        [[ ! -e "$path" && ! -L "$path" ]] \
            || fail 'Deployment paths already exist; inspect them before installing'
    done
    [[ $(systemctl show -p LoadState --value poetry-api.service) == not-found ]] \
        || fail 'A poetry-api systemd unit already exists'
    if getent passwd poetry >/dev/null || getent group poetry >/dev/null; then
        fail 'The poetry service account already exists'
    fi
    command -v ss >/dev/null || fail 'The ss network inspection command is required'
    [[ -z $(ss -H -ltn '( sport = :18080 )') ]] || fail 'Loopback API port 18080 is occupied'
    local listeners port
    listeners=$(ss -H -ltnp '( sport = :80 or sport = :443 )')
    if [[ -z "$listeners" ]]; then
        fail 'The existing Caddy listener is required'
    fi
    if printf '%s\n' "$listeners" | grep -v '"caddy"' >/dev/null; then
        fail 'A different process owns port 80 or 443'
    fi
    for port in 80 443; do
        [[ -n $(ss -H -ltn "( sport = :$port )") ]] || fail 'A required Caddy port is absent'
    done
    [[ $(systemctl show -p LoadState --value caddy.service) == loaded ]] \
        || fail 'The existing Caddy systemd unit is required'
    systemctl is-active --quiet caddy.service || fail 'The existing Caddy service is not active'
}

install_packages() {
    PHASE=packages
    local packages=() command_name package_name
    for pair in curl:curl openssl:openssl; do
        command_name=${pair%%:*}
        package_name=${pair#*:}
        command -v "$command_name" >/dev/null || packages+=("$package_name")
    done
    if [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
        packages+=(ca-certificates)
    fi
    if [[ ${#packages[@]} -gt 0 ]]; then
        DEBIAN_FRONTEND=noninteractive apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
    fi
}

supported_go() {
    local candidate=$1 version
    [[ -x "$candidate" ]] || return 1
    version=$("$candidate" version)
    [[ "$version" =~ go([0-9]+)\.([0-9]+) ]] || return 1
    (( BASH_REMATCH[1] > 1 || (BASH_REMATCH[1] == 1 && BASH_REMATCH[2] >= 25) ))
}

install_go() {
    PHASE=go-toolchain
    local candidate
    for candidate in "$(command -v go || true)" /usr/local/go/bin/go "$TOOLCHAIN/bin/go"; do
        if supported_go "$candidate"; then
            GO_BIN=$candidate
            return
        fi
    done
    [[ $(uname -m) == x86_64 ]] || fail 'Pinned Go archive requires an amd64 server'
    [[ ! -e "$TOOLCHAIN" && ! -L "$TOOLCHAIN" ]] \
        || fail 'The dedicated Go toolchain path already exists'
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --retry 3 --connect-timeout 10 --max-time 600 \
        https://go.dev/dl/go1.25.5.linux-amd64.tar.gz -o "$WORK/go.tar.gz"
    printf '%s  %s\n' "$GO_SHA256" "$WORK/go.tar.gz" | sha256sum --check --status \
        || fail 'Official Go archive checksum mismatch'
    tar -xzf "$WORK/go.tar.gz" -C "$WORK"
    rm -- "$WORK/go.tar.gz"
    install -d -m 0755 /opt/poetry-toolchains
    mv "$WORK/go" "$TOOLCHAIN"
    GO_BIN=$TOOLCHAIN/bin/go
    supported_go "$GO_BIN" || fail 'Installed Go toolchain failed its version check'
}

build_api() {
    PHASE=source-build
    printf 'Building the API from source with one compiler worker.\n'
    (
        cd "$STAGE/source"
        mkdir -m 0700 "$WORK/go-tmp"
        export CGO_ENABLED=0 GOMAXPROCS=1 GOMEMLIMIT=192MiB GOGC=50 GOTOOLCHAIN=local
        export GOPATH="$WORK/gopath" GOCACHE="$WORK/go-cache" GOTMPDIR="$WORK/go-tmp"
        "$GO_BIN" mod verify
        "$GO_BIN" build -p 1 -mod=readonly -trimpath -ldflags='-s -w' \
            -o "$WORK/vector-api" ./cmd/vector-api
    )
    [[ -s "$WORK/vector-api" ]] || fail 'Source build did not produce a binary'
    rm -rf -- "$WORK/gopath" "$WORK/go-cache" "$WORK/go-tmp"
}

write_environment() {
    local line key
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=([A-Za-z0-9._:/-]+)$ ]] \
            || fail 'Invalid nonsecret environment template'
        key=${BASH_REMATCH[1]}
        case "$key" in
            QDRANT_URL|QDRANT_CLUSTER_ENDPOINT|QDRANT_API_KEY|SILICONFLOW_API_KEY) continue ;;
        esac
        printf '%s\n' "$line"
    done < "$STAGE/source/deploy/bwg/vector-api.env.example"
    printf 'SILICONFLOW_API_KEY=%s\n' "$SILICONFLOW_API_KEY"
    printf 'QDRANT_API_KEY=%s\n' "$QDRANT_API_KEY"
    printf 'QDRANT_CLUSTER_ENDPOINT=%s\n' "$QDRANT_CLUSTER_ENDPOINT"
}

install_runtime() {
    PHASE=runtime-install
    install -d -m 0755 "$TARGET" "$TARGET/bin" "$TARGET/corpus" "$TARGET/source"
    install -d -m 0700 "$CONFIG"
    install -m 0755 "$WORK/vector-api" "$TARGET/bin/vector-api"
    install -m 0444 "$STAGE/corpus/chinese_poetry.db" "$TARGET/corpus/chinese_poetry.db"
    install -m 0444 "$STAGE/corpus/datas.json" "$TARGET/corpus/datas.json"
    install -m 0600 "$STAGE/client-token" "$CONFIG/client-token"
    install -m 0644 "$STAGE/source/go.mod" "$STAGE/source/go.sum" "$TARGET/source/"
    cp -a "$STAGE/source/cmd" "$STAGE/source/internal" "$TARGET/source/"
    install -d -m 0755 "$TARGET/source/deploy"
    cp -a "$STAGE/source/deploy/bwg" "$TARGET/source/deploy/"
    chown -R root:root "$TARGET" "$CONFIG"
    write_environment > "$CONFIG/vector-api.env"
    chmod 0600 "$CONFIG/vector-api.env"
    useradd --system --user-group --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin poetry
    install -m 0644 "$STAGE/source/deploy/bwg/poetry-api.service" "$UNIT"
    systemd-analyze verify "$UNIT"
    systemctl daemon-reload
    systemctl enable --now poetry-api.service
}

check_api() {
    PHASE=api-health
    local attempt
    for attempt in {1..30}; do
        if curl --fail --silent --max-time 2 http://127.0.0.1:18080/health >/dev/null; then
            return
        fi
        systemctl is-active --quiet poetry-api.service || fail 'The API systemd unit failed'
        sleep 1
    done
    fail 'The loopback API did not become healthy'
}

main() {
    [[ $# -eq 1 ]] || fail 'Usage: install.sh /absolute/pre-staged-deployment-directory'
    STAGE=$(realpath -- "$1")
    [[ "$STAGE" == /* && -d "$STAGE" ]] || fail 'Invalid staging directory'
    check_host
    check_stage
    WORK=$(mktemp -d /var/tmp/poetry-install.XXXXXX)
    trap cleanup EXIT
    trap 'fail "A command failed; inspect this phase without printing private files"' ERR
    install_packages
    install_go
    build_api
    install_runtime
    check_api
    printf 'Native poetry API installed; Caddy proxy and DNS cutover remain pending.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
