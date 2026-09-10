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
    mkdir -p {{data}}
    for i in $(seq 1 {{warps}}); do
        sudo mkdir -p "$PWD/{{data}}/warp$i" "/run/warp$i" "/var/log/warp$i"
        if [ ! -S "/run/warp$i/warp_service" ]; then
            echo "starting warp-svc #$i ..."
            nohup sudo env STATE_DIRECTORY="$PWD/{{data}}/warp$i" RUNTIME_DIRECTORY="/run/warp$i" LOGS_DIRECTORY="/var/log/warp$i" /bin/warp-svc >"$PWD/{{data}}/warp$i/svc.stdout.log" 2>&1 &
        fi
    done
    for i in $(seq 1 {{warps}}); do
        for _ in $(seq 1 50); do [ -S "/run/warp$i/warp_service" ] && break; sleep 0.2; done
        RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos mode proxy >/dev/null 2>&1 || true
        RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos proxy port $((40000 + i)) >/dev/null 2>&1 || true
        RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos tunnel protocol set MASQUE >/dev/null 2>&1 || true
        RUNTIME_DIRECTORY="/run/warp$i" warp-cli --accept-tos tunnel masque-options set h2-only >/dev/null 2>&1 || true
        echo "warp$i socket: $(ls -l /run/warp$i/warp_service 2>&1)"
    done
    export WARP_DATA_ROOT="$PWD/{{data}}" LISTEN_HOST=127.0.0.1 PROXY_PORT={{port}} HOLD_TIMEOUT=10 NUM_WARPS={{warps}}
    exec python3 app.py
