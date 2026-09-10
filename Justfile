set shell := ["bash", "-uc"]

port := "8080"
data := "./data"
warps := "8"
image := "warp-proxy"

default:
    @just --list

test:
    uv run --with pytest pytest tests/ -q

build:
    sudo docker build -t {{image}} .

up: build
    mkdir -p {{data}}
    sudo docker rm -f {{image}} 2>/dev/null || true
    sudo docker run -d --name {{image}} --privileged --device=/dev/net/tun -p 127.0.0.1:{{port}}:8080 -v "$PWD/{{data}}:/data" -e NUM_WARPS={{warps}} -e PROXY_PORT=8080 -e HOLD_TIMEOUT=10 {{image}}
    @echo "up. try: just health && just via-proxy"

# Detached container for Cloudflare Tunnel: loopback-only port + autorestart.
# Point cloudflared at http://127.0.0.1:<listen>, e.g. `just tunnel-up 18080`.
tunnel-up listen="8080": build
    mkdir -p {{data}}
    sudo docker rm -f {{image}}-tunnel 2>/dev/null || true
    sudo docker run -d --name {{image}}-tunnel --restart unless-stopped --privileged --device=/dev/net/tun -p 127.0.0.1:{{listen}}:8080 -v "$PWD/{{data}}:/data" -e NUM_WARPS={{warps}} -e PROXY_PORT=8080 -e HOLD_TIMEOUT=10 {{image}}
    @echo "tunnel-ready: 127.0.0.1:{{listen}} -> container :8080 (restart unless-stopped)"

stop:
    -sudo docker rm -f {{image}} 2>/dev/null
    -[ -f /tmp/warp-proxy-local.pid ] && kill "$(cat /tmp/warp-proxy-local.pid)" 2>/dev/null; rm -f /tmp/warp-proxy-local.pid
    @echo stopped

health:
    curl -s --max-time 10 http://127.0.0.1:{{port}}/health | python3 -m json.tool

rotate:
    curl -s --max-time 15 -X POST http://127.0.0.1:{{port}}/rotate; echo

via-proxy:
    curl -s --max-time 25 -x http://127.0.0.1:{{port}} -L ipconfig.me; echo

direct:
    bash curl.sh

logs:
    -sudo docker logs {{image}} 2>&1 | tail -30
    -tail -30 /tmp/local-proxy.log 2>/dev/null

# Host-local run: loopback-only proxy + per-instance warp-svc.
# Never touches the system warp-svc (/run/cloudflare-warp); proxy mode only.
local:
    PROXY_PORT={{port}} NUM_WARPS={{warps}} WARP_DATA_ROOT="$PWD/{{data}}" bash scripts/local.sh
