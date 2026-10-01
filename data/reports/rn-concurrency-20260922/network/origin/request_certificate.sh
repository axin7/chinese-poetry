set -eu
/opt/poetry-rn-benchmark-20260922/certbot/bin/certbot certonly \
    --non-interactive --agree-tos --register-unsafely-without-email \
    --webroot --webroot-path /opt/1panel/www/sites/rn-proxy-test.anyveo.com \
    --domain rn-proxy-test.anyveo.com --cert-name rn-proxy-test.anyveo.com \
    --config-dir /opt/poetry-rn-benchmark-20260922/certbot/config \
    --work-dir /opt/poetry-rn-benchmark-20260922/certbot/work \
    --logs-dir /opt/poetry-rn-benchmark-20260922/certbot/logs \
    --no-directory-hooks
