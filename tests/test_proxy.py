import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    sys.path.insert(0, str(ROOT))
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
