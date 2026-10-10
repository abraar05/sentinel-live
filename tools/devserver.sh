#!/bin/sh
# Start / stop the mock NVR and the Sentinel server for local testing.
#
#   tools/devserver.sh up      start mock NVR (34570) + Sentinel (8099)
#   tools/devserver.sh down    stop both
#   tools/devserver.sh status  show what is listening
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN=/tmp/opencode
NVR_PORT=34570
APP_PORT=8099
CLIP=$RUN/testclip.h264

mkdir -p "$RUN"

start_mock() {
  [ -f "$CLIP" ] || python3 "$ROOT/tools/make_testclip.py" "$CLIP" 300
  pkill -f "mock_nvr.py --port $NVR_PORT" 2>/dev/null || true
  sleep 0.5
  setsid python3 "$ROOT/tools/mock_nvr.py" --port "$NVR_PORT" --clip "$CLIP" \
    > "$RUN/nvr.log" 2>&1 < /dev/null &
  echo "mock nvr -> $NVR_PORT (pid $!)"
}

start_app() {
  pkill -f "uvicorn backend.app" 2>/dev/null || true
  sleep 0.5
  cd "$ROOT"
  setsid python3 -m uvicorn backend.app:app --host 0.0.0.0 --port "$APP_PORT" \
    --log-level warning > "$RUN/app.log" 2>&1 < /dev/null &
  echo "sentinel -> $APP_PORT (pid $!)"
}

case "$1" in
  up)     start_mock; start_app; sleep 3; curl -sS "http://127.0.0.1:$APP_PORT/api/health" || true; echo ;;
  down)   pkill -f "mock_nvr.py" || true; pkill -f "uvicorn backend.app" || true; echo stopped ;;
  status) ps -o pid,args -e 2>/dev/null | grep -E "mock_nvr|uvicorn" | grep -v grep || echo "nothing running" ;;
  *) echo "usage: $0 {up|down|status}"; exit 1 ;;
esac