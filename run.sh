#!/bin/bash
set -euo pipefail
mkdir -p ./data
sudo docker build -t warp-proxy .
sudo docker rm -f warp-proxy 2>/dev/null || true
sudo docker run -d --name warp-proxy --privileged \
  --device=/dev/net/tun \
  -p 8080:8080 \
  -v "$PWD/data:/data" \
  -e NUM_WARPS=8 -e PROXY_PORT=8080 -e HOLD_TIMEOUT=10 \
  warp-proxy
echo "up. test: curl -x http://127.0.0.1:8080 -L --max-time 20 ipconfig.me ; curl http://127.0.0.1:8080/health"
