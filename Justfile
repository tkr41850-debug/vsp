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

alias up := server

# Pool server: WARP pool + forward-proxy + /fetch + /relay. Loopback-only + autorestart.
# Deploy where UDP egress works, then expose over e.g. `cloudflared tunnel`.
server listen="8080": build
    mkdir -p {{data}}
    if [ -n "$(sudo docker ps -q -f name=^{{image}}$)" ]; then read -p "Kill existing {{image}}? [Y/n] " ans; case "$ans" in [Nn]*) echo "aborted; keeping existing {{image}}"; exit 1;; *) sudo docker rm -f {{image}};; esac; else sudo docker rm -f {{image}} 2>/dev/null || true; fi
    sudo docker run -d --name {{image}} --restart unless-stopped --privileged --device=/dev/net/tun -p 127.0.0.1:{{listen}}:8080 -v "$PWD/{{data}}:/data" -e NUM_WARPS={{warps}} -e PROXY_PORT=8080 -e HOLD_TIMEOUT=10 -e WARP_PROTOCOL="${WARP_PROTOCOL:-}" -e WARP_MASQUE="${WARP_MASQUE:-}" -e WARP_NET_MTU="${WARP_NET_MTU:-}" -e SEND_CHUNK="${SEND_CHUNK:-}" -e SEND_PACE_SEC="${SEND_PACE_SEC:-}" -e FETCH_HELLO="${FETCH_HELLO:-}" -e PROXY_TOKEN="${PROXY_TOKEN:-}" -e DEBUG="${DEBUG:-}" {{image}}
    @echo "server up: 127.0.0.1:{{listen}} (restart unless-stopped). try: just health && just via-proxy"

edge-build:
    sudo docker build -f Dockerfile.edge -t warp-edge .

# Edge client: forward-proxy for networks with UDP blocked. Reads pool from VSP_API_BASE.
# Usage: VSP_API_BASE=https://<pool-host> just client [port]
client listen="8080": edge-build
    if [ -z "${VSP_API_BASE:-}" ]; then echo "set VSP_API_BASE first, e.g. export VSP_API_BASE=https://<pool-host>" >&2; exit 2; fi
    sudo docker rm -f warp-edge 2>/dev/null || true
    sudo docker run -d --name warp-edge --restart unless-stopped -p 127.0.0.1:{{listen}}:8080 -e VSP_API_BASE="${VSP_API_BASE}" -e EDGE_PORT=8080 warp-edge
    @echo "edge up: 127.0.0.1:{{listen}} -> ${VSP_API_BASE} (restart unless-stopped)"

debug-key:
    @cat {{data}}/debug.key 2>/dev/null || echo "no debug key yet (start server with DEBUG=1 first)"

stop:
    -sudo docker rm -f {{image}} 2>/dev/null
    -[ -f /tmp/warp-proxy-local.pid ] && kill "$(cat /tmp/warp-proxy-local.pid)" 2>/dev/null; rm -f /tmp/warp-proxy-local.pid
    @echo stopped

health:
    curl -s --max-time 10 http://127.0.0.1:{{port}}/health | python3 -m json.tool

rotate:
    curl -s --max-time 15 -X POST http://127.0.0.1:{{port}}/rotate; echo

# Pool snapshot (any pool): VSP_API_BASE=https://<pool-host> just pool
pool:
    @bash scripts/pool.sh

# Probes (pool from VSP_API_BASE, edge default http://127.0.0.1:8080)
probe-edge:
    @python3 scripts/probe_edge.py --edge http://127.0.0.1:{{port}}

probe-fetch:
    @python3 scripts/probe_fetch.py --base "${VSP_API_BASE:?set VSP_API_BASE, e.g. export VSP_API_BASE=https://<pool-host>}"

probe-tls:
    @python3 scripts/probe_tls.py --base "${VSP_API_BASE:?set VSP_API_BASE, e.g. export VSP_API_BASE=https://<pool-host>}"

probe-sizes:
    @python3 scripts/probe_sizes.py --base "${VSP_API_BASE:?set VSP_API_BASE, e.g. export VSP_API_BASE=https://<pool-host>}"

probe-debug:
    @python3 scripts/probe_debug.py

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
