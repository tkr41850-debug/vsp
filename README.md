# warp-proxy

HTTP forward proxy (`GET` + `CONNECT`) on `:8080` that routes through up to 8 Cloudflare WARP identities. WARP runs in proxy mode only — never touches system routes/DNS.

## Prereqs

- Docker (with `--privileged` + `/dev/net/tun` available)
- [`just`](https://just.systems/) (`curl` used by test recipes)
- A network with UDP egress (WARP tunnels need UDP; without it the proxy holds requests 10s then returns `502`)

## Quickstart

```sh
git clone <repo> && cd warp-proxy
just up        # build image, start container (port 127.0.0.1:8080)
just health    # {"active":1,"warps":[...]} — 2 registered at boot, +1/hr to 8
just via-proxy # curl ipconfig.me through the proxy, expect a WARP exit IP
```

## Recipes

| Command | What it does |
|---|---|
| `just up` | Build + run container, loopback port `127.0.0.1:8080` |
| `just tunnel-up [port]` | Build + run with `--restart unless-stopped` for `cloudflared` (default `8080`, e.g. `just tunnel-up 18080`), then point cloudflared at `http://127.0.0.1:<port>` |
| `just local` | Same stack on the host (no container): 8x `warp-svc` + proxy on `127.0.0.1:8080` |
| `just health` | Show active warp + per-instance status |
| `just rotate` | `POST /rotate` — reopen current proxy, advance to next healthy warp |
| `just test` | Mocked pytest suite (no network needed) |
| `just stop` / `just logs` | Stop everything / tail logs |

Identities persist in `./data/warpN` (mounted to `/data`, git-ignored) so restarts reuse registrations instead of re-registering. Requests arriving while no warp is ready are held up to 10s, then get `502 + Retry-After: 5`.
