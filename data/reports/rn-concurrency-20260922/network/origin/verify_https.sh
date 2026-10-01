set -eu
curl --noproxy '*' -fsS -D - \
    --resolve rn-proxy-test.anyveo.com:443:192.129.135.19 \
    'https://rn-proxy-test.anyveo.com/rn-proxy-check?nonce=trusted-origin-20260922' \
    -w '\nTLS verify result: %{ssl_verify_result}; HTTP: %{http_code}; IP: %{remote_ip}\n'
curl --noproxy '*' -sS -o /dev/null -w 'Default path HTTP status: %{http_code}\n' \
    --resolve rn-proxy-test.anyveo.com:443:192.129.135.19 \
    'https://rn-proxy-test.anyveo.com/'
sha256sum /opt/1panel/www/conf.d/code.fengxin.me.conf \
    /opt/1panel/apps/openresty/openresty/conf/nginx.conf
