set -eu
UV_CACHE_DIR=/opt/poetry-rn-benchmark-20260922/certbot/cache \
UV_PYTHON_INSTALL_DIR=/opt/poetry-rn-benchmark-20260922/certbot/python \
UV_TOOL_DIR=/opt/poetry-rn-benchmark-20260922/certbot/tools \
UV_TOOL_BIN_DIR=/opt/poetry-rn-benchmark-20260922/certbot/bin \
/opt/poetry-rn-benchmark-20260922/tools/uv-x86_64-unknown-linux-gnu/uv \
tool install --python 3.12 --managed-python certbot
/opt/poetry-rn-benchmark-20260922/certbot/bin/certbot --version
