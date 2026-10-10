#!/bin/sh
# Sentinel Live - start the gateway.
#
#   ./run.sh                 listen on 0.0.0.0:8080
#   PORT=9000 ./run.sh       different port
#   SENTINEL_TOKEN=secret ./run.sh   require a token (recommended if exposed)
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

if [ -n "$SENTINEL_TOKEN" ]; then
  echo "token auth enabled"
fi

SENTINEL_BIND="$HOST" exec python3 -m uvicorn backend.app:app --host "$HOST" --port "$PORT" "$@"