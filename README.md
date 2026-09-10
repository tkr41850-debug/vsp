# vsp — WARP proxy pool + edge client

Split design for networks where UDP is blocked:

```
client --HTTPS--> pool --UDP/WireGuard--> WARP --> internet
(local edge)      (where UDP works)
```

- **Server** (`app.py`): up to 8 WARP identities (1 at boot, +1 per 8h shared
  budget — re-registrations consume the same budget), forward proxy on `:8080`
  (`GET` + `CONNECT`), plus edge-safe endpoints: `GET /health`, `POST /rotate`,
  `POST /fetch` (single request through WARP), `/relay` (websocket TCP bridge).
  Daemons start lazily (1 at first, more as registrations arrive), so churn stays
  minimal. WARP runs in proxy mode only — never touches system routes/DNS.
- **Client** (`edge.py`): forward proxy for UDP-blocked networks. Plain HTTP goes
  via pool `/fetch`, `CONNECT` goes via pool `/relay` over wss.

## Prereqs

- Docker (server needs `--privileged` + `/dev/net/tun`), [`just`](https://just.systems/)
- Server side needs UDP egress (WireGuard, the default). The client side needs only HTTPS.

## Quickstart

Server (where UDP works), exposed e.g. via `cloudflared tunnel`:

```sh
git clone <repo> && cd vsp
just server            # build + run, loopback 127.0.0.1:8080, autorestart
just health            # per-warp ready/status/registration
```

Client (here):

```sh
VSP_API_BASE=https://<pool-host> just client   # edge proxy on 127.0.0.1:8080
just via-proxy         # curl through the edge, expect a WARP exit IP
```

## Recipes

| Command | What it does |
|---|---|
| `just server [port]` | Build + run pool, loopback-only, `--restart unless-stopped` |
| `just client [port]` | Build + run edge, reads pool URL from `VSP_API_BASE` |
| `just local` | Pool stack directly on the host (dev, loopback-only) |
| `just health` / `just rotate` | Pool status (ready, status, registration) / reopen current proxy, advance to next healthy warp |
| `just via-proxy` / `just direct` | `curl.sh` through the edge / direct baseline IP |
| `just test` | Mocked pytest suite (no network needed) |
| `just stop` / `just logs` | Stop everything / tail logs |
| `just debug-key` | Print the debug key (pool machine, needs a `DEBUG=1` boot first) |

## Env

| Var | Default | Meaning |
|---|---|---|
| `VSP_API_BASE` | `https://pool.example.invalid` | Pool URL for the edge client |
| `PROXY_TOKEN` | empty (open) | If set, `/fetch` + `/relay` require `Bearer` token or `?token=` |
| `WARP_PROTOCOL` | `MASQUE` | `warp-cli tunnel protocol set` value per instance (proxy mode only supports MASQUE — WireGuard fails instantly) |
| `WARP_MASQUE` | empty (CF default) | e.g. `h2-only` for TCP-only MASQUE where UDP is filtered |
| `NUM_WARPS` | `8` | Pool size (registrations stagger: 1 at boot, +1 per 8h shared budget) |
| `HOLD_TIMEOUT` | `10` | Seconds to hold requests while no warp is ready, then `502 + Retry-After: 5` |
| `BOOT_RETRY_SEC` | `300` | Reconnect retry interval for registered-but-unready warps |
| `STALE_FAIL_THRESHOLD` | `3` | Failed boots before an identity counts as stale |
| `HEAL_COOLDOWN_SEC` | `3600` | Min interval between heal re-registrations per warp |
| `STATUS_CACHE_SEC` | `30` | `/health` reads cached statuses instead of spawning warp-cli per hit |
| `SEND_CHUNK` | `500` | Upstream write chunk size (bytes) toward warp |
| `SEND_PACE_SEC` | `0.2` | Pause between upstream chunks; `0` disables (lossy paths stall) |
| `FETCH_HELLO` | `compact` | Pool-originated TLS hello (`compact` = small TLS1.2, `full` = defaults) |
| `WARP_NET_MTU` | empty | Optionally force container egress MTU (e.g. `1400`) |
| `PROXY_TOKEN` | empty | Gates `/fetch` + `/relay` via `Bearer` or `?token=` |
| `DEBUG` | empty | `1` exposes `/debug/cli` (key-gated, see below) |

Identities persist in `./data/warpN` (mounted to `/data`, git-ignored) so restarts
reuse registrations. If prompted `Kill existing? [Y/n]`, a server container is
already running.

## Debugging the pool

`DEBUG=1 just server` exposes `POST /debug/cli`, which runs
`warp-cli --accept-tos <args…>` against instance `N` (`{"instance":N,"args":[...]}`,
or `{"runtime_dir":"/run/warpN",...}`). Every call needs the secret header:

```
KEY=$(just debug-key)   # on the pool machine; 64 random bytes in data/debug.key
curl -X POST https://<pool>/debug/cli -H "X-Debug-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"instance":1,"args":["status"]}'
```

Without `DEBUG=1` the endpoint is 404; with a wrong key it's 403. Turn it off
(restart without `DEBUG`) when done.

`POST /debug/cli` with `{"ephemeral": {...}}` runs a one-off config on warp1 by
default: `setup` (list of warp-cli arg lists), then one `action`, then `teardown`,
then `disconnect` unless `"leave": true`. Action kinds: `status`, `fetch`
(`url`, `method`, `headers`, `body_b64` through that instance's SOCKS), `sleep`
(`seconds`, lets you poll `status` between calls):

```
curl -X POST https://<pool>/debug/cli -H "X-Debug-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"ephemeral":{"instance":1,
    "setup":[["tunnel","protocol","set","MASQUE"]],
    "action":{"kind":"fetch","url":"http://example.com/"}}}'
```
