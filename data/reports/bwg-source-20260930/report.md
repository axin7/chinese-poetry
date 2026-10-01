# RN To BWG Source Deployment

Completed on 2026-09-30. The poetry service now runs on BWG (`104.225.151.64`), built
from source on Ubuntu 22.04 amd64 with Go 1.25.5. No Docker is installed or required.
RN remains running as a rollback origin.

## Active Deployment

- API: `poetry-api.service`, enabled, user `poetry`, loopback `127.0.0.1:18080`.
- Application and source: `/opt/poetry-tang/bin/vector-api`, `/opt/poetry-tang/source`.
- Read-only corpus: `/opt/poetry-tang/corpus`.
- Environment/client token/private proxy matcher: `/etc/poetry`, root-only mode 0600 files.
- Native Caddy 2.10.2 site: `/etc/caddy/sites/poetry-tang-20260930.conf`.
- Qdrant Cloud: unchanged collection `poetry_tang_20260922_v1`, 127,031 indexed points.
- Generation/profile: `poetry-20260921-v1`, `sf-bge-m3-1024-v1`.

The existing API paths and client token are preserved. The systemd service limits
memory to 192 MiB and CPU to 50%, starts on boot, and retries failed startups.
An explicit restart passed startup readiness and health checks. Final automatic
restart count was zero; observed API memory was approximately 8-11 MiB.

## DNS And TLS

Cloudflare A record `rn-proxy-test.anyveo.com` changed from `192.129.135.19` to
`104.225.151.64`. Proxy stays enabled and TTL stays automatic. Filtered DNS evidence
shows `Showing 1-1 of 1`; no other record for this hostname was changed.

`evidence/cloudflare-cutover.json` records the actual ego-browser page evidence and
a real TypeSafe API result: HTTP 200, `jev-1.13.0`, `pass`, confidence 1.
The public origin marker returns `{"origin":"bwg"}`. Public vector test responses
all include `X-Origin-ID: bwg-poetry-network-20260930`.

Caddy successfully completed production HTTP-01 validation and issued a new
Let's Encrypt YE2 certificate, valid until **2026-12-29 11:57:55 UTC**. Its certificate
manager logged renewal scheduling; renewal is automatic. The imported RN certificate
and manual proxy configuration remain under `/etc/poetry` for recovery.

Caddy's old configuration disabled its admin endpoint, and this version does not
implement SIGUSR1 reload. Its main configuration was backed up, a private Unix
admin endpoint was configured under `/run/poetry-caddy-admin` (directory mode 0700),
and a systemd RuntimeDirectory preserves this across restarts. A necessary restart
activated the change; graceful `systemctl reload caddy` now succeeds. A first socket
location under read-only `/etc` was corrected and service restored before DNS cutover.

## Verification

| Evidence | Result |
| --- | --- |
| `rn-baseline.json` | 22/22 API checks before migration |
| `bwg-loopback-final.json` | 22/22; same text hits/details as RN |
| `bwg-origin-final.json` | 22/22 HTTPS origin checks with the new certificate |
| `public-api-final.json` | 22/22 authenticated/public contracts |
| `bwg-gateway.json` | 5/5: detail auth, private routes, 64 KiB body limit |
| `bwg-vectors.json` | 32/32 fixed-vector parity checks; maximum score delta 0 |
| `public-vectors.json` | 32/32 public vector checks; 96/96 cached burst requests |
| `cloudflare-cutover.json` | DNS page evidence and TypeSafe pass |

Evidence files are in `evidence/`. Tests cover Chinese search, matching full details,
generation/profile/dimension failures, dataset exclusion, unsupported methods, bearer
authentication, and TLS hostname verification. Bursts use 4, 8, and 16 workers with
32 requests each; these are cached smoke tests, not a sustained capacity guarantee.

One independently generated text query had a score delta of 0.00258635 while returning
the same hit and full details. This is retained in the initial report; text comparisons
record score drift, and identical stored-vector comparisons enforce a 1e-5 tolerance.
Both final fixed-vector comparisons and public behavior passed.

The existing `st.anyveo.com` origin still returns its baseline 404 at `/`, CLIProxyAPI
returns 200, and Caddy/CLIProxyAPI/OneAV/sing-box units are active. Existing site files
retain their original hashes. Old `al.fengxin.me` and `vl.fengxin.me` configurations
already lack DNS records and produce unrelated ACME errors; they were not modified.
Existing Cloudflare Python urllib User-Agent restrictions were not changed.

## Integrity And Rollback

SQLite SHA-256: `c95b12576cf24cf9f67bcfad8c7d4aff098ad137abb11919fcea2b935324c799`.
Dataset config: `fad238022fdb8364ac8b0555f682321458efaf75caaa4680af837aaaad90d644`.
Native binary: `b39c464f55c096210a6ffb82d817c96d2f0f5619d2ab71b27caa01617c6b1213`.

To roll back, restore only this Cloudflare A record to `192.129.135.19`, preserving
proxy and TTL. Verify the RN marker, authenticated search, and poem details. RN's
API, source Qdrant, SQLite, and credentials remain available; unrelated RN services
were not changed. Deployment procedures are in `deploy/bwg/README.md`.
