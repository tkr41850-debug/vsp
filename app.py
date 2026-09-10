from __future__ import annotations
import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

DATA_ROOT = Path(os.environ.get("WARP_DATA_ROOT", "/data"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8080"))
NUM_WARPS = int(os.environ.get("NUM_WARPS", "8"))
HOLD_TIMEOUT = float(os.environ.get("HOLD_TIMEOUT", "10"))
BASE_SOCKS_PORT = int(os.environ.get("BASE_SOCKS_PORT", "40001"))
REG_INTERVAL_SEC = int(os.environ.get("REG_INTERVAL_SEC", "3600"))
INITIAL_BURST = int(os.environ.get("INITIAL_BURST", "2"))
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")
MAX_FETCH_BYTES = 10 * 1024 * 1024

log = logging.getLogger("warp-proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def warp_env(i: int) -> dict:
    env = dict(os.environ)
    env["STATE_DIRECTORY"] = str(DATA_ROOT / f"warp{i}")
    env["RUNTIME_DIRECTORY"] = f"/run/warp{i}"
    env["LOGS_DIRECTORY"] = f"/var/log/warp{i}"
    return env

def run_cli(i: int, *args: str, timeout: int = 20) -> tuple[int, str]:
    if args[:1] != ("--accept-tos",):
        args = ("--accept-tos", *args)
    try:
        p = subprocess.run(
            ["warp-cli", *args],
            env=warp_env(i),
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:
        return 1, str(exc)

@dataclass
class WarpInstance:
    idx: int
    socks_port: int
    ready: bool = False
    last_error: str = ""
    registration_id: str = ""

    @property
    def runtime_dir(self) -> Path:
        return Path(f"/run/warp{self.idx}")

    @property
    def state_dir(self) -> Path:
        return DATA_ROOT / f"warp{self.idx}"

    def has_registration(self) -> bool:
        return (self.state_dir / "reg.json").exists()

async def ensure_proxy_mode(inst: WarpInstance) -> None:
    loop = asyncio.get_running_loop()
    def _do():
        run_cli(inst.idx, "mode", "proxy")
        run_cli(inst.idx, "proxy", "port", str(inst.socks_port))
        return run_cli(inst.idx, "--accept-tos", "connect")
    await loop.run_in_executor(None, _do)

async def poll_until_connected(inst: WarpInstance, timeout: float = 60) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        loop = asyncio.get_running_loop()
        def _check():
            rc, out = run_cli(inst.idx, "status")
            return rc, out
        _rc, out = await loop.run_in_executor(None, _check)
        low = out.lower()
        if "connected" in low and "disconnected" not in low:
            try:
                r = await loop.run_in_executor(None, lambda: run_cli(inst.idx, "registration", "show"))
                if r[0] == 0:
                    for line in r[1].splitlines():
                        if line.strip().lower().startswith("id:"):
                            inst.registration_id = line.split(":", 1)[1].strip()
            except Exception:
                pass
            return True
        if "rate" in low or "429" in low or "too many" in low:
            inst.last_error = f"ratelimited: {out[:200]}"
            return False
        await asyncio.sleep(2)
    inst.last_error = "connect timeout"
    return False

async def socks5_connect(reader_host_port: tuple, host: str, port: int):
    r, w = reader_host_port
    w.write(b"\x05\x01\x00")
    await w.drain()
    resp = await r.readexactly(2)
    if resp != b"\x05\x00":
        raise OSError(f"socks5 auth failed: {resp!r}")
    import socket as _s
    try:
        ip = _s.inet_aton(host)
        atyp = b"\x01" + ip
    except OSError:
        hb = host.encode()
        atyp = b"\x03" + bytes([len(hb)]) + hb
    req = b"\x05\x01\x00" + atyp + port.to_bytes(2, "big")
    w.write(req)
    await w.drain()
    hdr = await r.readexactly(4)
    if hdr[0] != 5 or hdr[1] != 0:
        raise OSError(f"socks5 connect failed: {hdr!r}")
    at = hdr[3]
    if at == 1:
        await r.readexactly(6)
    elif at == 3:
        ln = (await r.readexactly(1))[0]
        await r.readexactly(ln + 2)
    elif at == 4:
        await r.readexactly(18)
    return r, w

def check_token(headers: dict, query: str) -> bool:
    if not PROXY_TOKEN:
        return True
    if headers.get("authorization", "") == f"Bearer {PROXY_TOKEN}":
        return True
    for kv in query.split("&"):
        k, _, v = kv.partition("=")
        if k == "token" and v == PROXY_TOKEN:
            return True
    return False


def _recvn(sock, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise OSError("socks closed")
        data += chunk
    return data


def fetch_blocking(socks_port: int, method: str, url: str,
                   headers: dict, body: bytes, timeout: int = 20):
    import socket as _sock
    import ssl as _ssl
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise ValueError("unsupported scheme")
    host = u.hostname or ""
    if not host:
        raise ValueError("no host")
    port = u.port or (443 if u.scheme == "https" else 80)
    if len(body) > MAX_FETCH_BYTES:
        raise ValueError("body too large")
    s = _sock.create_connection(("127.0.0.1", socks_port), timeout=timeout)
    s.settimeout(timeout)
    try:
        s.sendall(b"\x05\x01\x00")
        if _recvn(s, 2) != b"\x05\x00":
            raise OSError("socks auth failed")
        hb = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + port.to_bytes(2, "big"))
        hdr = _recvn(s, 4)
        if hdr[0] != 5 or hdr[1] != 0:
            raise OSError("socks connect failed")
        at = hdr[3]
        if at == 1:
            _recvn(s, 6)
        elif at == 3:
            _recvn(s, _recvn(s, 1)[0] + 2)
        elif at == 4:
            _recvn(s, 18)
        if u.scheme == "https":
            ctx = _ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=host)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        skip = {"host", "connection", "proxy-connection", "content-length",
                "transfer-encoding", "upgrade", "keep-alive"}
        out = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
        for k, v in headers.items():
            if k.lower() in skip or "\n" in v or "\r" in v:
                continue
            out.append(f"{k}: {v}")
        if body:
            out.append(f"Content-Length: {len(body)}")
        raw = ("\r\n".join(out) + "\r\n\r\n").encode("latin1") + body
        s.sendall(raw)
        f = s.makefile("rb")
        status_line = f.readline(8192).decode("latin1").strip()
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 502
        rheaders: dict[str, str] = {}
        while True:
            line = f.readline(8192).decode("latin1")
            if line in ("\r\n", "\n", ""):
                break
            if ":" in line:
                k, v = line.split(":", 1)
                rheaders[k.strip().lower()] = v.strip()
        if rheaders.get("transfer-encoding", "").lower() == "chunked":
            chunks = b""
            while True:
                size_line = f.readline(256).decode("latin1").strip().split(";")[0]
                size = int(size_line, 16)
                if size == 0:
                    f.readline(16)
                    break
                chunks += f.read(size)
                f.read(2)
                if len(chunks) > MAX_FETCH_BYTES:
                    raise ValueError("response too large")
            rbody = chunks
        elif rheaders.get("content-length", "").isdigit():
            remaining = int(rheaders["content-length"])
            if remaining > MAX_FETCH_BYTES:
                raise ValueError("response too large")
            rbody = f.read(remaining)
        else:
            rbody = f.read(MAX_FETCH_BYTES + 1)
            if len(rbody) > MAX_FETCH_BYTES:
                raise ValueError("response too large")
        return status, rheaders, rbody
    finally:
        try:
            s.close()
        except Exception:
            pass


@dataclass
class Manager:
    instances: list[WarpInstance] = field(default_factory=list)
    active: int = 1
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def healthy(self) -> list[WarpInstance]:
        return [w for w in self.instances if w.ready]

    async def wait_ready(self) -> WarpInstance | None:
        try:
            await asyncio.wait_for(self.ready_event.wait(), timeout=HOLD_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        h = self.healthy()
        if not h:
            return None
        for w in h:
            if w.idx == self.active:
                return w
        return h[0]

    async def rotate(self) -> dict:
        async with self.lock:
            h = self.healthy()
            order = sorted(h, key=lambda w: w.idx)
            nxt = None
            for w in order:
                if w.idx > self.active:
                    nxt = w
                    break
            if nxt is None and order:
                nxt = order[0]
            if nxt is None:
                return {"ok": False, "error": "no healthy warps"}
            old = self.active
            self.active = nxt.idx
            loop = asyncio.get_running_loop()
            def _reopen(i: int):
                run_cli(i, "disconnect")
                run_cli(i, "mode", "proxy")
                run_cli(i, "proxy", "port", str(BASE_SOCKS_PORT + i - 1))
                return run_cli(i, "--accept-tos", "connect")
            await loop.run_in_executor(None, _reopen, nxt.idx)
            asyncio.create_task(self._reverify(nxt))
            return {"ok": True, "old": old, "active": self.active}
    async def _reverify(self, inst: WarpInstance):
        inst.ready = False
        if not self.healthy():
            self.ready_event.clear()
        ok = await poll_until_connected(inst, timeout=45)
        inst.ready = ok
        if ok and self.healthy():
            self.ready_event.set()

manager = Manager(
    instances=[WarpInstance(idx=i, socks_port=BASE_SOCKS_PORT + i - 1) for i in range(1, NUM_WARPS + 1)]
)

async def read_headers(reader: asyncio.StreamReader) -> tuple[str, dict, bytes]:
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = await asyncio.wait_for(reader.read(65536), timeout=15)
        if not chunk:
            break
        raw += chunk
        if len(raw) > 1 << 20:
            break
    head, _, _ = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    request_line = lines[0].decode("latin1") if lines else ""
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
    return request_line, headers, raw

def parse_target(request_line: str, headers: dict) -> tuple[str, int, str] | None:
    parts = request_line.split()
    if len(parts) < 2:
        return None
    method, target = parts[0].upper(), parts[1]
    if method == "CONNECT":
        host, _, port_s = target.partition(":")
        return host, int(port_s or 443), request_line
    if target.startswith("http://") or target.startswith("https://"):
        from urllib.parse import urlsplit
        u = urlsplit(target)
        port = u.port or (443 if u.scheme == "https" else 80)
        return u.hostname or "", port, request_line
    host_h = headers.get("host", "")
    host, _, port_s = host_h.partition(":")
    return host, int(port_s or 80), request_line

async def relay(a_r, a_w, b_r, b_w):
    async def _copy(r, w):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                w.write(data)
                await w.drain()
        except Exception:
            pass
        try:
            w.write_eof()
        except Exception:
            pass
    await asyncio.gather(_copy(a_r, b_w), _copy(b_r, a_w))

async def serve_manager_api(writer: asyncio.StreamWriter, method: str, path: str, body: bytes):
    if method == "GET" and path in ("/health", "/healthz"):
        payload = {
            "active": manager.active,
            "warps": [
                {"idx": w.idx, "ready": w.ready, "socks": w.socks_port,
                 "registered": w.has_registration(), "error": w.last_error[-200:]}
                for w in manager.instances
            ],
        }
        data = json.dumps(payload).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(data)).encode() + b"\r\nConnection: close\r\n\r\n" + data)
        await writer.drain()
        return True
    if path == "/rotate" and method in ("POST", "GET"):
        res = await manager.rotate()
        data = json.dumps(res).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(data)).encode() + b"\r\nConnection: close\r\n\r\n" + data)
        await writer.drain()
        return True
    return False

async def serve_fetch(writer, method: str, headers: dict, query: str, body: bytes):
    import base64 as _b64
    from urllib.parse import parse_qs, unquote as _uq
    loop = asyncio.get_running_loop()
    if method == "GET":
        url = parse_qs(query).get("url", [""])[0]
        fwd_headers: dict = {}
        fwd_body = b""
    else:
        try:
            spec = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            spec = {}
        url = spec.get("url", "")
        fwd_headers = spec.get("headers", {}) or {}
        raw_body = spec.get("body_b64", "")
        fwd_body = _b64.b64decode(raw_body) if raw_body else b""
    if not url or not url.startswith(("http://", "https://")):
        payload = json.dumps({"ok": False, "error": "missing url"}).encode()
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)
        await writer.drain()
        return
    inst = await manager.wait_ready()
    if inst is None:
        payload = json.dumps({"ok": False, "error": "no warp ready"}).encode()
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\nRetry-After: 5\r\n\r\n" + payload)
        await writer.drain()
        return
    try:
        status, rheaders, rbody = await loop.run_in_executor(
            None, lambda: fetch_blocking(inst.socks_port, method if method != "GET" else "GET",
                                         url, fwd_headers, fwd_body))
    except Exception as exc:
        inst.last_error = str(exc)[-200:]
        payload = json.dumps({"ok": False, "error": str(exc)[-200:]}).encode()
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)
        await writer.drain()
        return
    payload = json.dumps({"ok": True, "status": status, "headers": rheaders,
                          "body_b64": _b64.b64encode(rbody).decode()}).encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)
    await writer.drain()


async def handle_client(c_r: asyncio.StreamReader, c_w: asyncio.StreamWriter):
    try:
        request_line, headers, raw = await read_headers(c_r)
        if not request_line:
            c_w.close()
            return
        parts = request_line.split()
        method = parts[0].upper() if parts else ""
        target = parts[1] if len(parts) > 1 else ""
        if target in ("/rotate", "/health", "/healthz") or target.startswith("/rotate?"):
            if await serve_manager_api(c_w, method, target.split("?")[0], b""):
                c_w.close()
                return
        from urllib.parse import parse_qs, urlsplit as _us
        _t = _us(target) if target.startswith("http") else None
        _path = (_t.path if _t else target.split("?")[0])
        _query = (_t.query if _t else target.partition("?")[2])
        if _path == "/fetch":
            if not check_token(headers, _query):
                c_w.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await c_w.drain()
                c_w.close()
                return
            length = int(headers.get("content-length", "0") or 0)
            if length > MAX_FETCH_BYTES + 65536:
                c_w.write(b"HTTP/1.1 413 Too Large\r\nConnection: close\r\n\r\n")
                await c_w.drain()
                c_w.close()
                return
            rest = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else b""
            need = length - len(rest)
            body = rest[:length] if need <= 0 else rest + await c_r.readexactly(need)
            await serve_fetch(c_w, method, headers, _query, body)
            c_w.close()
            return
        parsed = parse_target(request_line, headers)
        if not parsed or not parsed[0]:
            c_w.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await c_w.drain()
            c_w.close()
            return
        host, port, _ = parsed
        inst = await manager.wait_ready()
        if inst is None:
            c_w.write(b"HTTP/1.1 502 No Warp Ready (10s hold exceeded)\r\nConnection: close\r\nRetry-After: 5\r\n\r\n")
            await c_w.drain()
            c_w.close()
            return
        try:
            s_r, s_w = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", inst.socks_port), timeout=10)
            s_r, s_w = await asyncio.wait_for(
                socks5_connect((s_r, s_w), host, port), timeout=10)
        except Exception as exc:
            inst.last_error = str(exc)[-200:]
            c_w.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await c_w.drain()
            c_w.close()
            return
        if method == "CONNECT":
            c_w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await c_w.drain()
        else:
            s_w.write(raw)
            await s_w.drain()
            try:
                extra = c_r.read_nowait()
                if extra:
                    s_w.write(extra)
                    await s_w.drain()
            except Exception:
                pass
            async def _fwd_rest():
                try:
                    while True:
                        d = await c_r.read(65536)
                        if not d:
                            break
                        s_w.write(d)
                        await s_w.drain()
                except Exception:
                    pass
            asyncio.create_task(_fwd_rest())
        await relay(c_r, c_w, s_r, s_w)
    except Exception as exc:
        log.warning("client error: %r", exc)
    finally:
        try:
            c_w.close()
        except Exception:
            pass

async def registration_scheduler():
    registered_now: set[int] = set()
    for w in manager.instances:
        if w.has_registration():
            registered_now.add(w.idx)
    need_initial = [w for w in manager.instances[:INITIAL_BURST] if w.idx not in registered_now]
    for w in need_initial:
        ok = await register_one(w)
        if ok:
            registered_now.add(w.idx)
            manager.instances[w.idx - 1].ready = await poll_until_connected(w, timeout=45)
    if manager.healthy():
        manager.ready_event.set()
    for w in list(manager.instances):
        if w.ready:
            continue
        if w.has_registration():
            asyncio.create_task(boot_one(w))
    start = time.monotonic()
    while len(registered_now) < NUM_WARPS:
        await asyncio.sleep(REG_INTERVAL_SEC)
        nxt = next((w for w in manager.instances if w.idx not in registered_now), None)
        if nxt is None:
            break
        ok = await register_one(nxt)
        if ok:
            registered_now.add(nxt.idx)
            asyncio.create_task(boot_one(nxt))
        else:
            log.warning("registration failed idx=%s err=%s; retry next hour", nxt.idx, nxt.last_error)

async def register_one(w: WarpInstance) -> bool:
    loop = asyncio.get_running_loop()
    def _do():
        r1 = run_cli(w.idx, "--accept-tos", "registration", "new", timeout=60)
        return r1
    rc, out = await loop.run_in_executor(None, _do)
    low = out.lower()
    if rc == 0 or w.has_registration():
        return True
    if "rate" in low or "429" in low or "too many" in low or "limit" in low:
        w.last_error = f"RATELIMITED: {out[:300]}"
        log.warning("ratelimit idx=%s: %s", w.idx, out[:300])
        return False
    w.last_error = out[:300]
    log.warning("register failed idx=%s: %s", w.idx, out[:300])
    return "already" in low
async def boot_one(w: WarpInstance):
    await ensure_proxy_mode(w)
    ok = await poll_until_connected(w, timeout=60)
    w.ready = ok
    if ok and manager.healthy():
        manager.ready_event.set()

async def main():
    for i in range(1, NUM_WARPS + 1):
        (DATA_ROOT / f"warp{i}").mkdir(parents=True, exist_ok=True)
    asyncio.create_task(registration_scheduler())
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    log.info("warp forward-proxy on %s:%s hold=%ss warps=%s", LISTEN_HOST, LISTEN_PORT, HOLD_TIMEOUT, NUM_WARPS)
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
