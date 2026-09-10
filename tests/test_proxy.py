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
    assert warp_app.parse_status_output("Status update: Connected\nReason: ok") == "Connected"
    assert warp_app.parse_status_output("Status update: Disconnected\nReason: Manual") == "Disconnected"
    assert warp_app.parse_status_output("Unable to connect to the daemon: nope") == "Unable to connect to the daemon: nope"
    assert warp_app.parse_status_output("") == "unknown"
