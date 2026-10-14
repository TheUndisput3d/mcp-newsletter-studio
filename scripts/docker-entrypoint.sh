#!/bin/sh
set -eu
mkdir -p /data/workspace /data/secrets
python /usr/local/bin/migrate-docker-config
command -v node >/dev/null
command -v npx >/dev/null
if [ "$#" -gt 0 ]; then exec "$@"; fi
exec newsletter serve --config /app/config.yaml --host "${DASHBOARD_HOST:-0.0.0.0}" --port 8000
