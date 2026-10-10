#!/usr/bin/env bash
# Set up and run the Sentinel Live gateway on a machine that can reach your
# NVR/DVR over the LAN. It does NOT need to be reachable from the internet -
# see deploy/REMOTE-ACCESS.md for reaching it from your phone.
#
#   bash deploy/linux/install-and-run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

say() { printf '\033[36m==> %s\033[0m\n' "$1"; }

command -v python3 >/dev/null || { say "Python 3.11+ is required."; exit 1; }
say "python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

say "installing dependencies"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements.txt

export SENTINEL_DATA="$ROOT/data"
mkdir -p "$SENTINEL_DATA"

if [ -z "${SENTINEL_ADMIN_PASSWORD:-}" ]; then
    printf 'Create an admin account - password: '
    read -rs SENTINEL_ADMIN_PASSWORD
    echo
    export SENTINEL_ADMIN_PASSWORD
    export SENTINEL_BIND=127.0.0.1
    say "bound to 127.0.0.1 (this machine only)"
    say "REMOTE-ACCESS.md explains how to reach it from your phone"
fi

say "starting gateway on http://127.0.0.1:8080"
say "open that URL, sign in, then Import your DeviceManager.xml"
echo
exec python3 -m uvicorn backend.app:app --host "${SENTINEL_BIND:-0.0.0.0}" --port 8080