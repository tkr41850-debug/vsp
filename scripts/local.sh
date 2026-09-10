#!/bin/bash
# Host-local run: loopback-only forward proxy + per-instance warp-svc.
# Never touches the system warp-svc (/run/cloudflare-warp); proxy mode only.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PROXY_PORT:-8080}"
WARPS="${NUM_WARPS:-8}"
DATA="${WARP_DATA_ROOT:-$PWD/data}"
mkdir -p "$DATA"
for i in $(seq 1 "$WARPS"); do
    sudo mkdir -p "$DATA/warp$i" "/run/warp$i" "/var/log/warp$i"
done
echo "dirs ready; app supervises warp-svc on demand via sudo (starts with 1 daemon)"
export WARP_DATA_ROOT="$DATA" LISTEN_HOST=127.0.0.1 PROXY_PORT="$PORT" HOLD_TIMEOUT=10 NUM_WARPS="$WARPS"
exec python3 app.py
