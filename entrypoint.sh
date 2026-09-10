#!/bin/bash
set -euo pipefail
NUM_WARPS="${NUM_WARPS:-8}"
DATA_ROOT="${WARP_DATA_ROOT:-/data}"
mkdir -p "$DATA_ROOT"
if [ -n "${WARP_NET_MTU:-}" ]; then
  IFACE=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
  if [ -z "$IFACE" ]; then
    IFACE=$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' | head -1)
  fi
  IFACE="${IFACE:-eth0}"
  ip link set dev "$IFACE" mtu "$WARP_NET_MTU" 2>&1 || echo "WARN: could not set MTU on $IFACE" >&2
  echo "MTU $WARP_NET_MTU on $IFACE"
fi
if [ ! -e /dev/net/tun ]; then
  echo "WARN: /dev/net/tun missing (run with --device=/dev/net/tun --cap-add=NET_ADMIN)" >&2
fi
for i in $(seq 1 "$NUM_WARPS"); do
  mkdir -p "$DATA_ROOT/warp$i" "/run/warp$i" "/var/log/warp$i"
done
echo "dirs ready; app supervises warp-svc on demand (starts with 1 daemon)"
export LISTEN_HOST="${LISTEN_HOST:-0.0.0.0}"
exec python3 /app/app.py
