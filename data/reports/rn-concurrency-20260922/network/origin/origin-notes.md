# RN test origin

Created 2026-09-22 for `rn-proxy-test.anyveo.com` at `192.129.135.19`.

- HTTP and HTTPS `/rn-proxy-check` return 200 JSON with origin marker
  `rn-poetry-network-20260922`, reflected `nonce` (up to 256 characters), and a live timestamp.
- Responses include `Cache-Control: no-store, max-age=0` and `X-Origin-ID`.
- The default path returns 404; HTTP ACME challenges use an isolated webroot.
- No poetry credentials, database, or index are exposed by this endpoint.
- Both RN and an external client verified HTTPS using `curl --resolve` without `-k`.
- Original `code.fengxin.me.conf` and global `nginx.conf` hashes remain unchanged.

## Server files

| Purpose | Path |
| --- | --- |
| Vhost | `/opt/1panel/www/conf.d/rn-proxy-test.anyveo.com.conf` |
| Webroot and logs | `/opt/1panel/www/sites/rn-proxy-test.anyveo.com` |
| Certbot environment and state | `/opt/poetry-rn-benchmark-20260922/certbot` |
| Published certificate | `/opt/1panel/apps/openresty/openresty/conf/ssl/rn-proxy-test.anyveo.com` |
| Manual renewal script | `/opt/poetry-rn-benchmark-20260922/certbot/renew-rn-test.sh` |

The trusted Let's Encrypt certificate expires at **2026-12-21 03:27:45 UTC**.
Its SHA256 fingerprint is
`13:79:4B:DF:FD:C0:1B:4E:15:69:03:5C:81:FB:6C:F5:3E:76:0F:E6:9D:35:65:32:4E:E4:F1:FD:9B:74:4D:47`.
The private key stays on RN with mode 0600; it was never downloaded.

No automatic renewal job was installed for this temporary test site.
If retaining it, run the following remote command through ssh-skill before expiry:

```sh
sh /opt/poetry-rn-benchmark-20260922/certbot/renew-rn-test.sh
```

HTTP port 80 and the ACME challenge location must remain reachable for renewal.
The script renews when due, copies the renewed certificate, tests Nginx configuration,
and reloads OpenResty. It does not change unrelated vhosts or the zone TLS mode.

## Evidence

- `old-vhost-before.json`: original vhost/global configuration hashes.
- `http-recheck.json`: healthy HTTP and ACME challenge responses.
- `certificate-request.json`, `install-certificate.json`: issuance and public certificate details.
- `activate-https.json`: successful Nginx configuration test and reload.
- `verify-https-final.json`: trusted TLS, origin marker/nonce, 404 default, unchanged hashes.
