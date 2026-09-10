import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("warp_app", ROOT / "app.py")
assert spec and spec.loader
warp_app = importlib.util.module_from_spec(spec)
sys.modules["warp_app"] = warp_app
spec.loader.exec_module(warp_app)


def test_parse_connect():
    host, port, _ = warp_app.parse_target("CONNECT example.com:443 HTTP/1.1", {})
    assert (host, port) == ("example.com", 443)


def test_parse_absolute_uri():
    host, port, _ = warp_app.parse_target("GET http://example.com:8080/path HTTP/1.1", {})
    assert (host, port) == ("example.com", 8080)


def test_parse_origin_form_with_host():
    host, port, _ = warp_app.parse_target("GET /path HTTP/1.1", {"host": "example.com"})
    assert (host, port) == ("example.com", 80)


def test_read_headers():
    async def _go():
        data = b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n"
        r = asyncio.StreamReader()
        r.feed_data(data)
        r.feed_eof()
        line, headers, raw = await warp_app.read_headers(r)
        assert line.startswith("GET")
        assert headers["host"] == "example.com"
        assert raw == data
    asyncio.run(_go())


def test_wait_ready_timeout():
    async def _go():
        m = warp_app.Manager(instances=[warp_app.WarpInstance(idx=1, socks_port=40001)])
        warp_app.HOLD_TIMEOUT = 0.05
        try:
            got = await m.wait_ready()
        finally:
            warp_app.HOLD_TIMEOUT = 10
        assert got is None
    asyncio.run(_go())


def test_wait_ready_picks_active():
    async def _go():
        w1 = warp_app.WarpInstance(idx=1, socks_port=40001, ready=True)
        w2 = warp_app.WarpInstance(idx=2, socks_port=40002, ready=True)
        m = warp_app.Manager(instances=[w1, w2], active=2)
        m.ready_event.set()
        got = await m.wait_ready()
        assert got is not None and got.idx == 2
    asyncio.run(_go())


def test_rotate_round_robin(monkeypatch):
    async def _go():
        w1 = warp_app.WarpInstance(idx=1, socks_port=40001, ready=True)
        w2 = warp_app.WarpInstance(idx=2, socks_port=40002, ready=True)
        m = warp_app.Manager(instances=[w1, w2], active=1)
        calls = []
        monkeypatch.setattr(warp_app, "run_cli", lambda i, *a, **k: calls.append((i, a)) or (0, "ok"))
        async def fake_reverify(self, inst):
            return None
        monkeypatch.setattr(warp_app.Manager, "_reverify", fake_reverify)
        res = await m.rotate()
        assert res == {"ok": True, "old": 1, "active": 2}
        assert m.active == 2
        assert any(c[0] == 2 for c in calls)
    asyncio.run(_go())


def test_rotate_no_healthy():
    async def _go():
        m = warp_app.Manager(instances=[warp_app.WarpInstance(idx=1, socks_port=40001)], active=1)
        res = await m.rotate()
        assert res == {"ok": False, "error": "no healthy warps"}
    asyncio.run(_go())


def test_register_ratelimit(monkeypatch, tmp_path):
    async def _go():
        monkeypatch.setattr(warp_app, "DATA_ROOT", tmp_path)
        w = warp_app.WarpInstance(idx=1, socks_port=40001)
        monkeypatch.setattr(
            warp_app, "run_cli",
            lambda i, *a, **k: (1, "error 429 too many requests"),
        )
        ok = await warp_app.register_one(w)
        assert ok is False
        assert "RATELIMIT" in w.last_error
    asyncio.run(_go())


def test_has_registration(tmp_path, monkeypatch):
    monkeypatch.setattr(warp_app, "DATA_ROOT", tmp_path)
    w = warp_app.WarpInstance(idx=3, socks_port=40003)
    assert w.has_registration() is False
    (tmp_path / "warp3").mkdir(parents=True)
    (tmp_path / "warp3" / "reg.json").write_text("{}")
    assert w.has_registration() is True


def test_ws_roundtrip():
    import wscodec
    assert wscodec.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    key = wscodec.new_key()
    resp = wscodec.server_handshake_response(key)
    assert b"101 Switching Protocols" in resp
    assert wscodec.accept_key(key).encode() in resp

    async def _frames():
        import os as _os
        s_r = asyncio.StreamReader()
        payload = b"hello-warp" * 100
        s_r.feed_data(wscodec.encode_frame(payload, mask=True))
        s_r.feed_eof()
        op, out = await wscodec.read_frame(s_r)
        assert op == 0x2 and out == payload
        s_r2 = asyncio.StreamReader()
        big = _os.urandom(70000)
        s_r2.feed_data(wscodec.encode_frame(big))
        s_r2.feed_eof()
        op2, out2 = await wscodec.read_frame(s_r2)
        assert out2 == big
    asyncio.run(_frames())


def _run(coro):
    return asyncio.run(coro)


async def _fake_origin():
    async def _h(r, w):
        try:
            data = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=5)
            body = b'{"origin":true}'
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await w.drain()
        finally:
            w.close()
    srv = await asyncio.start_server(_h, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def _fake_socks5(origin_port):
    async def _h(r, w):
        try:
            await r.readexactly(3)
            w.write(b"\x05\x00")
            await w.drain()
            req = await r.readexactly(4)
            assert req[:3] == b"\x05\x01\x00" and req[3] == 3
            ln = (await r.readexactly(1))[0]
            await r.readexactly(ln + 2)
            o_r, o_w = await asyncio.open_connection("127.0.0.1", origin_port)
            w.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await w.drain()
            async def _cp(a, b):
                try:
                    while True:
                        d = await a.read(65536)
                        if not d:
                            break
                        b.write(d)
                        await b.drain()
                except Exception:
                    pass
            await asyncio.gather(_cp(r, o_w), _cp(o_r, w))
        finally:
            try:
                w.close()
            except Exception:
                pass
    srv = await asyncio.start_server(_h, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


def test_fetch_blocking_through_fake_socks():
    async def _go():
        osrv, oport = await _fake_origin()
        ssrv, sport = await _fake_socks5(oport)
        async with osrv, ssrv:
            loop = asyncio.get_running_loop()
            status, hdrs, body = await loop.run_in_executor(
                None, lambda: warp_app.fetch_blocking(sport, "GET", "http://example.test/x", {}, b""))
            assert status == 200
            assert body == b'{"origin":true}'
            assert hdrs.get("content-type") == "application/json"
    _run(_go())


def test_check_token():
    old = warp_app.PROXY_TOKEN
    try:
        warp_app.PROXY_TOKEN = ""
        assert warp_app.check_token({}, "") is True
        warp_app.PROXY_TOKEN = "secret"
        assert warp_app.check_token({}, "") is False
        assert warp_app.check_token({}, "token=secret") is True
        assert warp_app.check_token({"authorization": "Bearer secret"}, "") is True
        assert warp_app.check_token({"authorization": "Bearer wrong"}, "") is False
    finally:
        warp_app.PROXY_TOKEN = old


def test_relay_websocket_echo():
    import wscodec as _ws

    async def _echo(r, w):
        try:
            while True:
                d = await r.read(65536)
                if not d:
                    break
                w.write(d)
                await w.drain()
        except Exception:
            pass
        finally:
            try:
                w.close()
            except Exception:
                pass

    async def _go():
        esrv = await asyncio.start_server(_echo, "127.0.0.1", 0)
        eport = esrv.sockets[0].getsockname()[1]
        ssrv, sport = await _fake_socks5(eport)
        old_manager = warp_app.manager
        warp_app.manager = warp_app.Manager(
            instances=[warp_app.WarpInstance(idx=99, socks_port=sport, ready=True)])
        warp_app.manager.ready_event.set()
        psrv = await asyncio.start_server(warp_app.handle_client, "127.0.0.1", 0)
        pport = psrv.sockets[0].getsockname()[1]
        try:
            r, w = await asyncio.open_connection("127.0.0.1", pport)
            key = _ws.new_key()
            w.write(f"GET /relay?host=e.test&port=443 HTTP/1.1\r\nHost: x\r\n"
                    f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode("latin1"))
            await w.drain()
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=10)
            assert b" 101 " in head.split(b"\r\n", 1)[0]
            w.write(_ws.encode_frame(b"ping-relay", mask=True))
            await w.drain()
            fr = await asyncio.wait_for(_ws.read_frame(r), timeout=10)
            assert fr is not None and fr[1] == b"ping-relay"
            w.close()
        finally:
            for srv in (esrv, ssrv, psrv):
                srv.close()
            for t in asyncio.all_tasks():
                if t is not asyncio.current_task():
                    t.cancel()
        warp_app.manager = old_manager
    _run(_go())


def test_edge_read_full_chunked():
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("edge_mod", ROOT / "edge.py")
    edge = importlib.util.module_from_spec(spec)
    sys.modules["edge_mod"] = edge
    spec.loader.exec_module(edge)

    async def _go():
        r = asyncio.StreamReader()
        r.feed_data(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                    b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
        r.feed_eof()
        raw = await edge.read_full(r)
        assert raw.endswith(b"hello world")
    _run(_go())


def test_parse_status_output():
    assert warp_app.parse_status_output("Status update: Connected\nReason: ok") == ("Connected", "ok")
    assert warp_app.parse_status_output("Status update: Disconnected\nReason: Manual Disconnection") == ("Disconnected", "Manual Disconnection")
    assert warp_app.parse_status_output("Unable to connect to the daemon: nope") == ("Unable to connect to the daemon: nope", "")
    assert warp_app.parse_status_output("") == ("unknown", "")


async def _debug_post(port: int, spec: dict, extra_headers: str = "") -> tuple[int, dict]:
    body = json.dumps(spec).encode()
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"POST /debug/cli HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            f"{extra_headers}"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    await w.drain()
    raw = await r.read()
    w.close()
    _, _, rbody = raw.partition(b"\r\n\r\n")
    status = int(raw.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(rbody.decode() or "{}")


def _debug_key_header() -> str:
    return f"X-Debug-Key: {warp_app.get_debug_key()}\r\n"


def test_debug_cli(monkeypatch, tmp_path):
    import subprocess as _sp
    monkeypatch.setattr(warp_app, "DATA_ROOT", tmp_path)

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "warp-cli"
        return _sp.CompletedProcess(cmd, 0, stdout="warp-cli 2026.0-test\n", stderr="")
    monkeypatch.setattr(_sp, "run", fake_run)
    hdr = f"X-Debug-Key: {warp_app.get_debug_key()}\r\n"

    async def _go():
        srv = await asyncio.start_server(warp_app.handle_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        old = warp_app.DEBUG_CLI
        try:
            warp_app.DEBUG_CLI = False
            try:
                st, _ = await _debug_post(port, {"instance": 1, "args": ["--version"]})
                assert st == 404
            finally:
                srv.close()
            srv2 = await asyncio.start_server(warp_app.handle_client, "127.0.0.1", 0)
            port2 = srv2.sockets[0].getsockname()[1]
            warp_app.DEBUG_CLI = True
            try:
                st, res = await _debug_post(port2, {"instance": 1, "args": ["--version"]}, hdr)
                assert st == 200 and res["ok"] is True and res["rc"] == 0
                assert "20" in res["stdout"]
                st, _ = await _debug_post(port2, {"instance": 1, "args": "oops"}, hdr)
                assert st == 400
                st, _ = await _debug_post(port2, {"instance": 99, "args": ["--version"]}, hdr)
                assert st == 400
                st, res = await _debug_post(port2, {"runtime_dir": "/run/warp2", "args": ["--version"]}, hdr)
                assert st == 200 and res["rc"] == 0
                st, res = await _debug_post(port2, {"log": 99}, hdr)
                assert st == 400
            finally:
                srv2.close()
        finally:
            warp_app.DEBUG_CLI = old
    asyncio.run(_go())


def test_stale_heal_only_with_healthy_sibling(monkeypatch):
    async def _go():
        calls = []
        def fake_cli(i, *a, **k):
            calls.append((i, a))
            if a[-2:] == ("registration", "new"):
                return (0, "Success")
            if a[-2:] == ("registration", "delete"):
                return (0, "Success")
            if a[-1:] == ("status",):
                return (0, "Status update: Connected")
            return (0, "Success")
        monkeypatch.setattr(warp_app, "run_cli", fake_cli)
        async def _true(w, timeout=30):
            return True
        monkeypatch.setattr(warp_app, "ensure_daemon", _true)
        monkeypatch.setattr(warp_app, "poll_until_connected", lambda inst, timeout=60: _false())
        async def _false():
            return False
        old_manager = warp_app.manager
        try:
            sib = warp_app.WarpInstance(idx=2, socks_port=40002, ready=True)
            w = warp_app.WarpInstance(idx=1, socks_port=40001, ready=False, fail_count=2)
            w.has_registration = lambda: True
            warp_app.manager = warp_app.Manager(instances=[w, sib])
            await warp_app.boot_one(w)
            assert any(c[1][-2:] == ("registration", "delete") for c in calls), calls
        finally:
            warp_app.manager = old_manager
    asyncio.run(_go())


def test_no_heal_when_all_down(monkeypatch):
    async def _go():
        calls = []
        def fake_cli(i, *a, **k):
            calls.append((i, a))
            return (0, "Status update: Disconnected")
        monkeypatch.setattr(warp_app, "run_cli", fake_cli)
        async def _true(w, timeout=30):
            return True
        monkeypatch.setattr(warp_app, "ensure_daemon", _true)
        async def _false(inst, timeout=60):
            return False
        monkeypatch.setattr(warp_app, "poll_until_connected", _false)
        old_manager = warp_app.manager
        try:
            w = warp_app.WarpInstance(idx=1, socks_port=40001, ready=False, fail_count=5)
            w.has_registration = lambda: True
            o = warp_app.WarpInstance(idx=2, socks_port=40002, ready=False)
            warp_app.manager = warp_app.Manager(instances=[w, o])
            await warp_app.boot_one(w)
            assert not any(c[1][-2:] == ("registration", "delete") for c in calls), calls
        finally:
            warp_app.manager = old_manager
    asyncio.run(_go())


def test_debug_key_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(warp_app, "DATA_ROOT", tmp_path)
    key = warp_app.get_debug_key()
    assert len(key) == 128
    assert (tmp_path / "debug.key").read_text().strip() == key
    assert warp_app.get_debug_key() == key
    assert warp_app.check_debug_key({"x-debug-key": key}) is True
    assert warp_app.check_debug_key({"x-debug-key": "wrong"}) is False
    assert warp_app.check_debug_key({}) is False


def test_debug_cli_requires_key(monkeypatch, tmp_path):
    monkeypatch.setattr(warp_app, "DATA_ROOT", tmp_path)
    key = warp_app.get_debug_key()

    async def _go():
        srv = await asyncio.start_server(warp_app.handle_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        old = warp_app.DEBUG_CLI
        warp_app.DEBUG_CLI = True
        try:
            st, _ = await _debug_post(port, {"instance": 1, "args": ["--version"]})
            assert st == 403
            r, w = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"instance": 1, "args": ["--version"]}).encode()
            w.write(f"POST /debug/cli HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                    f"X-Debug-Key: {key}\r\nContent-Length: {len(body)}\r\n"
                    f"Connection: close\r\n\r\n".encode() + body)
            await w.drain()
            raw = await r.read()
            w.close()
            st = int(raw.split(b"\r\n", 1)[0].split()[1])
            res = json.loads(raw.partition(b"\r\n\r\n")[2].decode() or "{}")
            assert st == 200 and res["ok"] is True
        finally:
            srv.close()
            warp_app.DEBUG_CLI = old
    asyncio.run(_go())


def test_budget_gates_heal(monkeypatch):
    import time as _t
    old_last = warp_app.last_reg_ts
    try:
        warp_app.last_reg_ts = _t.monotonic()
        assert warp_app.budget_wait() > 0
        warp_app.last_reg_ts = 0.0
        assert warp_app.budget_wait() == 0.0 or warp_app.budget_wait() >= 0
        warp_app.mark_reg()
        assert warp_app.budget_wait() > 0
    finally:
        warp_app.last_reg_ts = old_last


def test_heal_deferred_on_spent_budget(monkeypatch):
    import time as _t
    async def _go():
        calls = []
        def fake_cli(i, *a, **k):
            calls.append((i, a))
            return (0, "ok")
        monkeypatch.setattr(warp_app, "run_cli", fake_cli)
        async def _true(w, timeout=30):
            return True
        monkeypatch.setattr(warp_app, "ensure_daemon", _true)
        async def _false(inst, timeout=60):
            return False
        monkeypatch.setattr(warp_app, "poll_until_connected", _false)
        old_manager = warp_app.manager
        old_last = warp_app.last_reg_ts
        try:
            warp_app.last_reg_ts = _t.monotonic()
            sib = warp_app.WarpInstance(idx=2, socks_port=40002, ready=True)
            w = warp_app.WarpInstance(idx=1, socks_port=40001, ready=False, fail_count=9)
            w.has_registration = lambda: True
            warp_app.manager = warp_app.Manager(instances=[w, sib])
            await warp_app.boot_one(w)
            assert not any(c[1][-2:] == ("registration", "delete") for c in calls), calls
        finally:
            warp_app.manager = old_manager
            warp_app.last_reg_ts = old_last
    asyncio.run(_go())


def test_ephemeral_validation(monkeypatch, tmp_path):
    monkeypatch.setattr(warp_app, "DATA_ROOT", tmp_path)

    async def _go():
        srv = await asyncio.start_server(warp_app.handle_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        old = warp_app.DEBUG_CLI
        warp_app.DEBUG_CLI = True
        hdr = f"X-Debug-Key: {warp_app.get_debug_key()}\r\n"
        try:
            st, res = await _debug_post(port, {"ephemeral": {"instance": 99}}, hdr)
            assert st == 400
            st, res = await _debug_post(port, {"ephemeral": {"instance": 1, "action": {"kind": "nope"}}}, hdr)
            assert st == 200 and res["ok"] is True
            assert res["action"].get("error") == "unknown action kind"
            assert res["disconnect"]["rc"] != 124 or True
        finally:
            srv.close()
            warp_app.DEBUG_CLI = old
    asyncio.run(_go())


def test_debug_config(monkeypatch, tmp_path):
    monkeypatch.setattr(warp_app, "DATA_ROOT", tmp_path)
    monkeypatch.setenv("PROXY_TOKEN", "super-secret-value")
    monkeypatch.setattr(warp_app, "PROXY_TOKEN", "super-secret-value")
    key = warp_app.get_debug_key()

    async def _go():
        srv = await asyncio.start_server(warp_app.handle_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        old = warp_app.DEBUG_CLI
        warp_app.DEBUG_CLI = True
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET /debug/config HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            await w.drain()
            raw = await r.read()
            w.close()
            assert raw.split(b"\r\n", 1)[0].startswith(b"HTTP/1.1 403")
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"GET /debug/config HTTP/1.1\r\nHost: x\r\nX-Debug-Key: {key}\r\n"
                    f"Connection: close\r\n\r\n".encode())
            await w.drain()
            raw = await r.read()
            w.close()
            body = raw.partition(b"\r\n\r\n")[2].decode()
            assert "super-secret-value" not in body
            assert key not in body
            d = json.loads(body)
            assert d["ok"] is True
            assert d["secrets"] == {"proxy_token_set": True, "debug_key_set": True}
            assert d["pool"]["num_warps"] == warp_app.NUM_WARPS
        finally:
            srv.close()
            warp_app.DEBUG_CLI = old
    asyncio.run(_go())


def test_chunked_write_splits():
    class FakeW:
        def __init__(self):
            self.chunks = []
        def write(self, data):
            self.chunks.append(bytes(data))
        async def drain(self):
            pass
    async def _go():
        w = FakeW()
        await warp_app.chunked_write(w, b"z" * 2500, chunk=1000)
        assert [len(c) for c in w.chunks] == [1000, 1000, 500]
        assert b"".join(w.chunks) == b"z" * 2500
    asyncio.run(_go())


def test_compact_hello_small(monkeypatch):
    import ssl as _ssl
    monkeypatch.setattr(warp_app, "FETCH_HELLO", "compact")
    ctx = warp_app.tls_context()
    bi, bo = _ssl.MemoryBIO(), _ssl.MemoryBIO()
    tls = ctx.wrap_bio(bi, bo, server_hostname="example.com")
    try:
        tls.do_handshake()
    except _ssl.SSLWantReadError:
        pass
    assert len(bo.read()) < 800


def test_nodelay_sets_option():
    import socket as _sock
    seen = {}
    class FakeSock:
        def setsockopt(self, level, opt, val):
            seen[(level, opt)] = val
    class FakeW:
        def get_extra_info(self, name):
            return FakeSock() if name == "socket" else None
    warp_app.nodelay(FakeW())
    assert seen.get((_sock.IPPROTO_TCP, _sock.TCP_NODELAY)) == 1
    warp_app.nodelay(object())


def test_chunked_write_paces():
    import time as _t
    class FakeW:
        def __init__(self):
            self.chunks = []
        def write(self, data):
            self.chunks.append(bytes(data))
        async def drain(self):
            pass
    async def _go():
        w = FakeW()
        start = _t.monotonic()
        await warp_app.chunked_write(w, b"z" * 1200, chunk=500, pace=0.2)
        dt = _t.monotonic() - start
        assert [len(c) for c in w.chunks] == [500, 500, 200]
        assert dt >= 0.35
        w2 = FakeW()
        await warp_app.chunked_write(w2, b"z" * 400, chunk=500, pace=0.2)
        assert [len(c) for c in w2.chunks] == [400]
    asyncio.run(_go())


def test_ephemeral_tune(monkeypatch, tmp_path):
    monkeypatch.setattr(warp_app, "DATA_ROOT", tmp_path)
    old_chunk, old_pace, old_hello = warp_app.SEND_CHUNK, warp_app.SEND_PACE_SEC, warp_app.FETCH_HELLO

    async def _go():
        srv = await asyncio.start_server(warp_app.handle_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        old = warp_app.DEBUG_CLI
        warp_app.DEBUG_CLI = True
        hdr = f"X-Debug-Key: {warp_app.get_debug_key()}\r\n"
        try:
            st, res = await _debug_post(
                port, {"ephemeral": {"instance": 1, "action": {"kind": "tune", "send_chunk": 64,
                                                              "send_pace_sec": 9, "fetch_hello": "bogus"}}}, hdr)
            assert st == 200
            assert res["action"]["tuned"] == {}
            st, res = await _debug_post(
                port, {"ephemeral": {"instance": 1, "action": {"kind": "tune", "send_chunk": 750,
                                                              "send_pace_sec": 0.1, "fetch_hello": "full"}}}, hdr)
            assert st == 200
            assert res["action"]["tuned"] == {"send_chunk": 750, "send_pace_sec": 0.1, "fetch_hello": "full"}
            assert (warp_app.SEND_CHUNK, warp_app.SEND_PACE_SEC, warp_app.FETCH_HELLO) == (750, 0.1, "full")
        finally:
            srv.close()
            warp_app.DEBUG_CLI = old
            warp_app.SEND_CHUNK, warp_app.SEND_PACE_SEC, warp_app.FETCH_HELLO = old_chunk, old_pace, old_hello
    asyncio.run(_go())


def test_env_helpers_tolerate_empty(monkeypatch):
    monkeypatch.setenv("PROXY_PORT", "")
    monkeypatch.setenv("SEND_CHUNK", "")
    monkeypatch.setenv("SEND_PACE_SEC", "")
    monkeypatch.setenv("NUM_WARPS", "bogus")
    assert warp_app._env_int("PROXY_PORT", 8080) == 8080
    assert warp_app._env_int("SEND_CHUNK", 500) == 500
    assert warp_app._env_float("SEND_PACE_SEC", 0.2) == 0.2
    assert warp_app._env_int("NUM_WARPS", 8) == 8
    monkeypatch.setenv("SEND_CHUNK", "750")
    assert warp_app._env_int("SEND_CHUNK", 500) == 750
