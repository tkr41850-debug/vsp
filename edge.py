"""Edge forward-proxy: runs where UDP is blocked, forwards over HTTPS to the pool.

Client -> edge (plain HTTP forward-proxy, loopback) -> pool /fetch or /relay (wss).
Pool egresses via WARP. Env: POOL_BASE, PROXY_TOKEN, LISTEN_HOST, EDGE_PORT.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import ssl
import sys
from urllib.parse import urlsplit, urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wscodec

POOL_BASE = os.environ.get("VSP_API_BASE", os.environ.get("POOL_BASE", "https://pool.example.invalid")).rstrip("/")
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
EDGE_PORT = int(os.environ.get("EDGE_PORT", "8080"))

_pool = urlsplit(POOL_BASE)
POOL_HOST = _pool.hostname or ""
POOL_PORT = _pool.port or 443
POOL_TLS = _pool.scheme == "https"

log = logging.getLogger("edge-proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_ctx = ssl.create_default_context()


async def pool_conn():
    if POOL_TLS:
        return await asyncio.open_connection(POOL_HOST, POOL_PORT, ssl=_ctx, server_hostname=POOL_HOST)
    return await asyncio.open_connection(POOL_HOST, POOL_PORT)


async def read_full(r: asyncio.StreamReader) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await r.read(65536)
        if not chunk:
            break
        head += chunk
    header, _, rest = head.partition(b"\r\n\r\n")
    length = 0
    chunked = False
    for line in header.split(b"\r\n")[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            kl = k.strip().lower()
            if kl == b"content-length" and v.strip().isdigit():
                length = int(v.strip())
            elif kl == b"transfer-encoding" and b"chunked" in v.lower():
                chunked = True
    if chunked:
        body, buf = b"", rest
        while True:
            while b"\r\n" not in buf:
                more = await r.read(65536)
                if not more:
                    break
                buf += more
            if b"\r\n" not in buf:
                buf = b""
                break
            line, buf = buf.split(b"\r\n", 1)
            size = int(line.decode().strip().split(";")[0] or "0", 16)
            if size == 0:
                break
            while len(buf) < size + 2:
                more = await r.read(65536)
                if not more:
                    break
                buf += more
            body += buf[:size]
            buf = buf[size + 2:]
        return header + b"\r\n\r\n" + body
    if length:
        need = length - len(rest)
        if need > 0:
            rest += await r.readexactly(need)
        return header + b"\r\n\r\n" + rest[:length]
    tail = rest
    while True:
        d = await r.read(65536)
        if not d:
            break
        tail += d
    return header + b"\r\n\r\n" + tail


def auth_query() -> str:
    return ("?" + urlencode({"token": PROXY_TOKEN})) if PROXY_TOKEN else ""


async def pool_fetch(method: str, url: str, headers: dict, body: bytes) -> dict:
    spec = json.dumps({
        "url": url,
        "headers": {k: v for k, v in headers.items()
                    if k.lower() not in ("host", "connection", "proxy-connection")},
        "body_b64": base64.b64encode(body).decode() if body else "",
    }).encode()
    req = (f"POST /fetch{auth_query()} HTTP/1.1\r\nHost: {POOL_HOST}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(spec)}\r\n"
           "Connection: close\r\n\r\n").encode("latin1") + spec
    r, w = await pool_conn()
    try:
        w.write(req)
        await w.drain()
        raw = await read_full(r)
    finally:
        w.close()
    _, _, rbody = raw.partition(b"\r\n\r\n")
    return json.loads(rbody.decode("utf-8") or "{}")


async def handle_client(c_r: asyncio.StreamReader, c_w: asyncio.StreamWriter):
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(c_r.read(65536), timeout=15)
            if not chunk:
                break
            head += chunk
        if not head:
            c_w.close()
            return
        header, _, rest = head.partition(b"\r\n\r\n")
        lines = header.split(b"\r\n")
        parts = lines[0].decode("latin1").split()
        if len(parts) < 2:
            c_w.close()
            return
        method, target = parts[0].upper(), parts[1]
        headers = {}
        for line in lines[1:]:
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()

        if method == "CONNECT":
            host, _, port_s = target.partition(":")
            await handle_connect(c_r, c_w, host, int(port_s or 443))
            return

        if target.startswith("/"):
            await handle_manager_passthrough(c_r, c_w, method, target, headers, rest)
            return

        length = int(headers.get("content-length", "0") or 0)
        body = rest[:length]
        if len(body) < length:
            body += await c_r.readexactly(length - len(body))
        try:
            res = await pool_fetch(method, target, headers, body)
        except Exception as exc:
            c_w.write(f"HTTP/1.1 502 pool fetch failed {exc}\r\nConnection: close\r\n\r\n".encode()[:200])
            await c_w.drain()
            c_w.close()
            return
        if not res.get("ok"):
            msg = str(res.get("error", "fetch failed"))[:200]
            c_w.write(f"HTTP/1.1 502 {msg}\r\nConnection: close\r\nRetry-After: 5\r\n\r\n".encode("latin1"))
            await c_w.drain()
            c_w.close()
            return
        rbody = base64.b64decode(res.get("body_b64", ""))
        out = [f"HTTP/1.1 {res.get('status', 502)} OK"]
        for k, v in (res.get("headers", {}) or {}).items():
            if k.lower() in ("connection", "transfer-encoding", "content-length"):
                continue
            out.append(f"{k}: {v}")
        out.append(f"Content-Length: {len(rbody)}")
        out.append("Connection: close")
        c_w.write(("\r\n".join(out) + "\r\n\r\n").encode("latin1") + rbody)
        await c_w.drain()
        c_w.close()
    except Exception as exc:
        log.warning("edge client error: %r", exc)
        try:
            c_w.close()
        except Exception:
            pass


async def handle_manager_passthrough(c_r, c_w, method, target, headers, rest):
    length = int(headers.get("content-length", "0") or 0)
    body = rest[:length]
    if len(body) < length:
        body += await c_r.readexactly(length - len(body))
    req = (f"{method} {target} HTTP/1.1\r\nHost: {POOL_HOST}\r\n"
           f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode("latin1") + body
    try:
        r, w = await pool_conn()
        try:
            w.write(req)
            await w.drain()
            raw = await read_full(r)
        finally:
            w.close()
    except Exception:
        c_w.write(b"HTTP/1.1 502 pool unreachable\r\nConnection: close\r\n\r\n")
        await c_w.drain()
        c_w.close()
        return
    c_w.write(raw)
    await c_w.drain()
    c_w.close()


async def handle_connect(c_r, c_w, host: str, port: int):
    try:
        qs = urlencode({"host": host, "port": port, **({"token": PROXY_TOKEN} if PROXY_TOKEN else {})})
        key = wscodec.new_key()
        r, w = await pool_conn()
        w.write(wscodec.client_handshake_request(POOL_HOST, f"/relay?{qs}", key))
        await w.drain()
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(r.read(65536), timeout=15)
            if not chunk:
                raise OSError("relay handshake failed")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            c_w.write(b"HTTP/1.1 502 relay rejected\r\nConnection: close\r\n\r\n")
            await c_w.drain()
            c_w.close()
            w.close()
            return
        c_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await c_w.drain()

        async def _c2p():
            try:
                while True:
                    d = await c_r.read(65536)
                    if not d:
                        break
                    w.write(wscodec.encode_frame(d, mask=True))
                    await w.drain()
            except Exception:
                pass

        async def _p2c():
            try:
                while True:
                    fr = await wscodec.read_frame(r)
                    if fr is None:
                        break
                    c_w.write(fr[1])
                    await c_w.drain()
            except Exception:
                pass

        await asyncio.gather(_c2p(), _p2c())
    except Exception as exc:
        log.warning("connect relay error: %r", exc)
    finally:
        for s in (c_w, w):
            try:
                s.close()
            except Exception:
                pass


async def main():
    server = await asyncio.start_server(handle_client, LISTEN_HOST, EDGE_PORT)
    log.info("edge forward-proxy on %s:%s pool=%s", LISTEN_HOST, EDGE_PORT, POOL_BASE)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
