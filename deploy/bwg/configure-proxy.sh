#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077

CONFIG=/etc/poetry
SITE=/etc/caddy/sites/poetry-tang-20260930.conf

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

reload_caddy() {
    local main=/etc/caddy/Caddyfile
    if grep -Eq '^[[:space:]]*admin off[[:space:]]*$' "$main"; then
        [[ -f "$CONFIG/Caddyfile.before-poetry" ]] \
            || cp -a "$main" "$CONFIG/Caddyfile.before-poetry"
        install -d -m 0755 /etc/systemd/system/caddy.service.d
        install -m 0644 "$1/source/deploy/bwg/caddy-poetry.conf" \
            /etc/systemd/system/caddy.service.d/poetry-admin.conf
        install -d -m 0700 /run/poetry-caddy-admin
        sed 's|^[[:space:]]*admin off[[:space:]]*$|  admin unix//run/poetry-caddy-admin/admin.sock|' \
            "$main" > "$CONFIG/Caddyfile.next"
        install -m 0644 "$CONFIG/Caddyfile.next" "$main"
        if ! /usr/local/bin/caddy validate --config "$main" --adapter caddyfile \
            > "$CONFIG/proxy-admin-validation.log" 2>&1; then
            cp -a "$CONFIG/Caddyfile.before-poetry" "$main"
            fail 'Private Caddy admin configuration failed validation'
        fi
        systemctl daemon-reload
        if ! systemctl restart caddy.service; then
            cp -a "$CONFIG/Caddyfile.before-poetry" "$main"
            systemctl restart caddy.service || true
            fail 'Caddy activation failed; its previous main configuration was restored'
        fi
    else
        /usr/local/bin/caddy reload --config "$main" --adapter caddyfile \
            --address unix//run/poetry-caddy-admin/admin.sock \
            > "$CONFIG/proxy-reload.log" 2>&1
    fi
}

main() {
    [[ $EUID -eq 0 && $# -ge 1 && $# -le 2 ]] || fail 'Usage: configure-proxy.sh STAGE [auto]'
    local stage token mode=${2:-manual}
    stage=$(realpath -- "$1")
    [[ "$mode" == manual || "$mode" == auto ]] || fail 'Unknown certificate mode'
    [[ -f "$CONFIG/client-token" && -f "$stage/source/deploy/bwg/poetry.caddy" ]] \
        || fail 'Install the native API and stage the Caddy template first'
    [[ $(systemctl show caddy -p User --value) == root ]] \
        || fail 'The existing Caddy service must be able to read root-only credentials'
    token=$(< "$CONFIG/client-token")
    [[ "$token" =~ ^[A-Za-z0-9._~-]+$ ]] || fail 'Unexpected client token format'
    if [[ "$mode" == manual ]]; then
        if [[ -e "$SITE" || -L "$SITE" ]]; then
            cmp -s "$SITE" "$stage/source/deploy/bwg/poetry.caddy" \
                || fail 'A different poetry proxy site already exists'
        fi
        install -d -m 0700 "$CONFIG/tls"
        install -m 0644 "$stage/tls/fullchain.pem" "$CONFIG/tls/fullchain.pem"
        install -m 0600 "$stage/tls/privkey.pem" "$CONFIG/tls/privkey.pem"
        openssl x509 -in "$CONFIG/tls/fullchain.pem" -checkend 604800 -noout >/dev/null
        openssl x509 -in "$CONFIG/tls/fullchain.pem" -checkhost rn-proxy-test.anyveo.com \
            -noout >/dev/null
        printf '@unauthorized not header Authorization "Bearer %s"\n' "$token" \
            > "$CONFIG/poetry-auth.caddy"
        install -m 0600 "$stage/source/deploy/bwg/poetry.caddy" "$SITE"
    else
        [[ -f "$SITE" ]] || fail 'Poetry proxy site has not been activated'
        [[ -f "$CONFIG/poetry-site-manual.backup" ]] \
            || cp -a "$SITE" "$CONFIG/poetry-site-manual.backup"
        sed '\|^[[:space:]]*tls /etc/poetry/tls/fullchain.pem /etc/poetry/tls/privkey.pem$|d' \
            "$stage/source/deploy/bwg/poetry.caddy" > "$SITE"
    fi
    if ! /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
        > "$CONFIG/proxy-validation.log" 2>&1; then
        if [[ "$mode" == auto ]]; then
            cp -a "$CONFIG/poetry-site-manual.backup" "$SITE"
        else
            rm -- "$SITE"
        fi
        fail 'Caddy validation failed; the active configuration was not reloaded'
    fi
    reload_caddy "$stage"
    printf 'Caddy configuration validated and activated (%s TLS).\n' "$mode"
}

main "$@"
