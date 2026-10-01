set -eu
cert_source=/opt/poetry-rn-benchmark-20260922/certbot/config/live/rn-proxy-test.anyveo.com
cert_target=/opt/1panel/apps/openresty/openresty/conf/ssl/rn-proxy-test.anyveo.com
test -s "$cert_source/fullchain.pem"
test -s "$cert_source/privkey.pem"
install -d -m 700 "$cert_target"
install -m 644 "$cert_source/fullchain.pem" "$cert_target/fullchain.pem"
install -m 600 "$cert_source/privkey.pem" "$cert_target/privkey.pem"
openssl x509 -in "$cert_target/fullchain.pem" -noout -subject -issuer -dates -fingerprint -sha256
