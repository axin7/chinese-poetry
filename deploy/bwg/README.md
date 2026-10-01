# Native BWG Deployment

This deployment builds the poetry API from source on Debian/Ubuntu and runs it with
systemd and the existing native Caddy. Qdrant remains in Cloud. No Docker is required.

The Go API uses the [admission policy](../rate-policy.md). Deploy its matching environment
template with the new binary; preserve the mandatory Caddy token check and loopback API binding.
The policy was deployed to active BWG and rollback RN on 2026-10-01; see the
[release evidence](../../data/reports/poetry-access-20261001/report.md).

## Staging

Create a root-only staging directory on the verified BWG host with this layout:

```text
stage/
  source/                  go.mod, go.sum, cmd/vector-api, internal, deploy/bwg
  corpus/                  chinese_poetry.db, datas.json from RN
  .env                     RN SiliconFlow credential input
  .env.cloud               RN Qdrant Cloud credential input
  client-token             Existing client token from RN
  tls/fullchain.pem        Existing hostname certificate from RN
  tls/privkey.pem           Existing private key from RN
```

Transfer private files using ssh-skill without printing their content. Confirm the
SQLite SHA-256 matches the migration report before running:

```sh
bash /absolute/stage/source/deploy/bwg/install.sh /absolute/stage
```

The installer refuses occupied deployment paths, service identities, API ports,
and incompatible web servers. Inspect any refusal before adapting the deployment.
It installs only the API and leaves Caddy proxy activation to a separate script.
Installation errors may leave newly installed poetry files for inspection.

Runtime files: `/opt/poetry-tang`, `/etc/poetry`, `poetry-api.service`, and
`/etc/caddy/sites/poetry-tang-20260930.conf`. The API listens on `127.0.0.1:18080`.
The systemd manager reads the mandatory root-only environment file. Caddy performs
bearer authentication before forwarding requests to the API.

Activate the proxy with the transferred certificate before DNS cutover:

```sh
bash /absolute/stage/source/deploy/bwg/configure-proxy.sh /absolute/stage
```

The existing Caddy 2.10.2 has its admin endpoint disabled and does not support signal
reload. The script backs up its main configuration, enables a Unix admin socket in
`/run/poetry-caddy-admin`, and adds a systemd RuntimeDirectory with mode 0700. A first
restart activates this change; subsequent changes use graceful reload. Other sites
retain their existing configuration. No public admin port is opened.

## Validation And Cutover

Use the local uv environment and ssh-skill loopback tunnels for private API checks:

```sh
uv run python deploy/bwg/validate_api.py \
  --base-url http://127.0.0.1:LOCAL_BWG_TUNNEL_PORT \
  --baseline-url http://127.0.0.1:LOCAL_RN_TUNNEL_PORT \
  --report /absolute/new-loopback-report.json
```

Before DNS changes, verify native service state, Caddy configuration, data hashes, and HTTPS
at BWG's verified public IP with the existing hostname and certificate:

```sh
uv run python deploy/bwg/validate_api.py \
  --base-url https://rn-proxy-test.anyveo.com/poetry \
  --origin-ip VERIFIED_BWG_PUBLIC_IP \
  --token-file /absolute/private-client-token \
  --report /absolute/new-origin-report.json
```

After passing origin validation, change only the existing Cloudflare A record for
`rn-proxy-test.anyveo.com` from `192.129.135.19` to BWG. Keep proxy enabled and TTL
automatic; inspect all records for the exact hostname. Repeat public validation
without `--origin-ip`, and confirm `/rn-proxy-check` returns `{"origin":"bwg"}`.
Verify restart recovery and service enablement. Keep RN running for rollback.

After cutover, enable Caddy's automatic certificate issuance and renewal:

```sh
bash /absolute/stage/source/deploy/bwg/configure-proxy.sh /absolute/stage auto
```

Verify a new certificate is issued, its served hostname and validity are correct,
and public/origin API tests still pass. The imported certificate and manual site
configuration remain under `/etc/poetry` for recovery.

## Rollback

Restore the Cloudflare A record to `192.129.135.19`, keeping proxy and TTL settings.
Confirm the RN origin marker, authenticated public search, and poem details. Preserve
BWG files and reports for diagnosis; avoid stopping unrelated host services.
