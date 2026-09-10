# vsp — WARP proxy pool + edge client

Split design for networks where UDP is blocked:

```
client --HTTPS--> pool --UDP/WireGuard--> WARP --> internet
(local edge)      (where UDP works)
```

- **Server** (`app.py`): 8 WARP identities (2 at boot, +1/hr), forward proxy on `:8080`
  (`GET` + `CONNECT`), plus edge-safe endpoints: `GET /health`, `POST /rotate`,
  `POST /fetch` (single request through WARP), `/relay` (websocket TCP bridge).
  WARP runs in proxy mode only — never touches system routes/DNS.
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

## Env

| Var | Default | Meaning |
|---|---|---|
| `VSP_API_BASE` | `https://pool.example.invalid` | Pool URL for the edge client |
| `PROXY_TOKEN` | empty (open) | If set, `/fetch` + `/relay` require `Bearer` token or `?token=` |
| `WARP_PROTOCOL` | `WireGuard` | `warp-cli tunnel protocol set` value per instance |
| `WARP_MASQUE` | empty (CF default) | e.g. `h2-only` for TCP-only MASQUE where UDP is filtered |
| `NUM_WARPS` | `8` | Pool size (registrations stagger: 2 at boot, +1/hr) |
| `HOLD_TIMEOUT` | `10` | Seconds to hold requests while no warp is ready, then `502 + Retry-After: 5` |

Identities persist in `./data/warpN` (mounted to `/data`, git-ignored) so restarts
reuse registrations. If prompted `Kill existing? [Y/n]`, a server container is
already running.
