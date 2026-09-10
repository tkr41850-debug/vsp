#!/usr/bin/env python3
"""Reproducible pool probes. Usage:
    python3 scripts/probe.py sizes [--base https://pool.example.invalid]
    python3 scripts/probe.py tls   [--base ...] [--host example.com]
    python3 scripts/probe.py fetch [--base ...] [--url http://example.com]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wscodec


def _pool_parts(base: str) -> tuple[str, int]:
    from urllib.parse import urlsplit
    u = urlsplit(base)
    return u.hostname or "", u.port or 443


async def _relay_conn(base: str, host: str, port: int):
    pool_host, pool_port = _pool_parts(base)
    ctx = ssl.create_default_context()
    r, w = await asyncio.open_connection(pool_host, pool_port, ssl=ctx, server_hostname=pool_host)
    key = wscodec.new_key()
    w.write(wscodec.client_handshake_request(pool_host, f"/relay?host={host}&port={port}", key))
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=15)
    if b" 101 " not in head.split(b"\r\n", 1)[0]:
        w.close()
        first = head.split(b"\r\n", 1)[0]
        raise RuntimeError(f"handshake failed: {first!r}")
    return r, w


async def cmd_sizes(base: str) -> int:
    ok = True
    for size in (100, 1000, 1400, 1500, 2000, 4000, 8000):
        try:
            r, w = await _relay_conn(base, "example.com", 80)
            body = b"x" * size
            req = (f"POST /x HTTP/1.1\r\nHost: example.com\r\nContent-Length: {size}\r\n"
                   f"Connection: close\r\n\r\n").encode() + body
            w.write(wscodec.encode_frame(req, mask=True))
            await w.drain()
            try:
                fr = await asyncio.wait_for(wscodec.read_frame(r), timeout=15)
                print(f"{size} -> {None if fr is None else f'{len(fr[1])}B'}", flush=True)
                ok = ok and fr is not None
            except TimeoutError:
                print(f"{size} -> STALL", flush=True)
                ok = False
            w.close()
        except Exception as exc:
            print(f"{size} -> ERR {type(exc).__name__}: {exc}"[:120], flush=True)
            ok = False
        await asyncio.sleep(1)
    return 0 if ok else 1


async def cmd_tls(base: str, host: str) -> int:
    r, w = await _relay_conn(base, host, 443)
    cli = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cli.check_hostname = False
    cli.verify_mode = ssl.CERT_NONE
    bi, bo = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = cli.wrap_bio(bi, bo, server_hostname=host)
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    w.write(wscodec.encode_frame(bo.read(), mask=True))
    await w.drain()
    try:
        fr = await asyncio.wait_for(wscodec.read_frame(r), timeout=20)
        print(f"serverhello: {None if fr is None else len(fr[1])}", flush=True)
        rc = 0 if fr else 1
    except TimeoutError:
        print("TIMEOUT held", flush=True)
        rc = 1
    w.close()
    return rc


def cmd_fetch(base: str, url: str) -> int:
    req = urllib.request.Request(f"{base}/fetch?url={url}")
    try:
        with urllib.request.urlopen(req, timeout=40) as res:
            d = json.load(res)
        print(f"ok={d.get('ok')} status={d.get('status')} body_b64_len={len(d.get('body_b64',''))}")
        print("err:", str(d.get("error", ""))[:150])
        return 0 if d.get("ok") else 1
    except Exception as exc:
        print(f"fetch failed: {exc}"[:200])
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sizes", "tls", "fetch"])
    ap.add_argument("--base", default="https://pool.example.invalid")
    ap.add_argument("--host", default="example.com")
    ap.add_argument("--url", default="http://example.com")
    args = ap.parse_args(argv)
    if args.cmd == "sizes":
        return asyncio.run(cmd_sizes(args.base))
    if args.cmd == "tls":
        return asyncio.run(cmd_tls(args.base, args.host))
    return cmd_fetch(args.base, args.url)


if __name__ == "__main__":
    raise SystemExit(main())
