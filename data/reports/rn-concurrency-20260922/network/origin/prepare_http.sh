set -eu
test ! -e /opt/1panel/www/conf.d/rn-proxy-test.anyveo.com.conf
test ! -e /opt/1panel/www/sites/rn-proxy-test.anyveo.com
mkdir -p /opt/1panel/www/sites/rn-proxy-test.anyveo.com/.well-known/acme-challenge
chmod 755 /opt/1panel/www/sites/rn-proxy-test.anyveo.com
mkdir -p /opt/poetry-rn-benchmark-20260922/certbot
date -u +%Y-%m-%dT%H:%M:%SZ
