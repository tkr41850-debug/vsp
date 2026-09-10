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
    if [ ! -S "/run/warp$i/warp_service" ]; then
        echo "starting warp-svc #$i ..."
        nohup sudo env STATE_DIRECTORY="$DATA/warp$i" RUNTIME_DIRECTORY="/run/warp$i" LOGS_DIRECTORY="/var/log/warp$i" /bin/warp-svc >"$DATA/warp$i/svc.stdout.log" 2>&1 &
    fi
done
for i in $(seq 1 "$WARPS"); do
    for _ in $(seq 1 50); do [ -S "/run/warp$i/warp_service" ] && break; sleep 0.2; done
    RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos mode proxy >/dev/null 2>&1 || true
    RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos proxy port $((40000 + i)) >/dev/null 2>&1 || true
        RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos tunnel protocol set "${WARP_PROTOCOL:-WireGuard}" >/dev/null 2>&1 || true
        if [ -n "${WARP_MASQUE:-}" ]; then
            RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos tunnel masque-options set "$WARP_MASQUE" >/dev/null 2>&1 || true
        fi
    echo "warp$i socket: $(ls -l /run/warp$i/warp_service 2>&1)"
done
export WARP_DATA_ROOT="$DATA" LISTEN_HOST=127.0.0.1 PROXY_PORT="$PORT" HOLD_TIMEOUT=10 NUM_WARPS="$WARPS"
exec python3 app.py
