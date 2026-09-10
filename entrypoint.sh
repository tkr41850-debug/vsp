#!/bin/bash
set -euo pipefail
NUM_WARPS="${NUM_WARPS:-8}"
DATA_ROOT="${WARP_DATA_ROOT:-/data}"
mkdir -p "$DATA_ROOT"
for i in $(seq 1 "$NUM_WARPS"); do
  mkdir -p "$DATA_ROOT/warp$i" "/run/warp$i" "/var/log/warp$i"
  if [ ! -e /dev/net/tun ]; then
    echo "WARN: /dev/net/tun missing (run with --device=/dev/net/tun --cap-add=NET_ADMIN)" >&2
  fi
  echo "starting warp-svc #$i ..."
  STATE_DIRECTORY="$DATA_ROOT/warp$i" \
  RUNTIME_DIRECTORY="/run/warp$i" \
  LOGS_DIRECTORY="/var/log/warp$i" \
  /bin/warp-svc >/var/log/warp$i/svc.stdout.log 2>&1 &
  echo $! > "/run/warp$i/svc.pid"
done
for i in $(seq 1 "$NUM_WARPS"); do
  for _ in $(seq 1 50); do
    [ -S "/run/warp$i/warp_service" ] && break
    sleep 0.2
  done
  echo "warp$i socket: $(ls -l /run/warp$i/warp_service 2>&1)"
  RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos mode proxy >/dev/null 2>&1 || true
  RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos proxy port $((40000 + i)) >/dev/null 2>&1 || true
  RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos tunnel protocol set MASQUE >/dev/null 2>&1 || true
  RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos tunnel masque-options set h2-only >/dev/null 2>&1 || true
done
export LISTEN_HOST="${LISTEN_HOST:-0.0.0.0}"
exec python3 /app/app.py
