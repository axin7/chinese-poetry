set -eu
certbot_root=/opt/poetry-rn-benchmark-20260922/certbot
cert_target=/opt/1panel/apps/openresty/openresty/conf/ssl/rn-proxy-test.anyveo.com
"$certbot_root/bin/certbot" renew --non-interactive \
    --cert-name rn-proxy-test.anyveo.com \
    --config-dir "$certbot_root/config" \
    --work-dir "$certbot_root/work" \
    --logs-dir "$certbot_root/logs" --no-directory-hooks
install -m 644 "$certbot_root/config/live/rn-proxy-test.anyveo.com/fullchain.pem" \
    "$cert_target/fullchain.pem"
install -m 600 "$certbot_root/config/live/rn-proxy-test.anyveo.com/privkey.pem" \
    "$cert_target/privkey.pem"
docker exec 1Panel-openresty-i7Mo nginx -t
docker exec 1Panel-openresty-i7Mo nginx -s reload
