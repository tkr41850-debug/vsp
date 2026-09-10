#!/bin/bash
# Local (host) run: loopback-only forward proxy + per-instance warp-svc.
# Never touches the system warp-svc (/run/cloudflare-warp); proxy mode only.
set -euo pipefail
cd "$(dirname "$0")"
export WARP_DATA_ROOT="${WARP_DATA_ROOT:-$PWD/data}"
export LISTEN_HOST=127.0.0.1
export PROXY_PORT="${PROXY_PORT:-8080}"
export HOLD_TIMEOUT="${HOLD_TIMEOUT:-10}"
export NUM_WARPS="${NUM_WARPS:-8}"
mkdir -p "$WARP_DATA_ROOT"
for i in $(seq 1 "$NUM_WARPS"); do
  sudo mkdir -p "$WARP_DATA_ROOT/warp$i" "/run/warp$i" "/var/log/warp$i"
  if [ ! -S "/run/warp$i/warp_service" ]; then
    echo "starting warp-svc #$i ..."
    sudo STATE_DIRECTORY="$WARP_DATA_ROOT/warp$i" \
      RUNTIME_DIRECTORY="/run/warp$i" \
      LOGS_DIRECTORY="/var/log/warp$i" \
      nohup /bin/warp-svc >/var/log/warp$i/svc.stdout.log 2>&1 &
  fi
done
for i in $(seq 1 "$NUM_WARPS"); do
  for _ in $(seq 1 50); do
    [ -S "/run/warp$i/warp_service" ] && break
    sleep 0.2
  done
  export RUNTIME_DIRECTORY="/run/warp$i"
  warp-cli --accept-tos mode proxy >/dev/null 2>&1 || true
  warp-cli --accept-tos proxy port $((40000 + i)) >/dev/null 2>&1 || true
  warp-cli --accept-tos tunnel protocol set MASQUE >/dev/null 2>&1 || true
  warp-cli --accept-tos tunnel masque-options set h2-only >/dev/null 2>&1 || true
  unset RUNTIME_DIRECTORY
  echo "warp$i ready: $(ls -l /run/warp$i/warp_service 2>&1)"
done
exec python3 app.py
