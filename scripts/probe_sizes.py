#!/usr/bin/env python3
"""Graded upstream sizes through pool /relay (MTU blackhole detector). Usage:
    python3 scripts/probe_sizes.py [--base URL]
"""
from __future__ import annotations

import argparse
import os
import asyncio
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wscodec

SIZES = (100, 1000, 1400, 1500, 2000, 4000, 8000)


async def probe(base: str, size: int) -> bool:
    u = urlsplit(base)
    pool_host, pool_port = u.hostname or "", u.port or 443
    try:
        ctx = ssl.create_default_context()
        r, w = await asyncio.open_connection(pool_host, pool_port, ssl=ctx, server_hostname=pool_host)
        key = wscodec.new_key()
        w.write(wscodec.client_handshake_request(pool_host, "/relay?host=example.com&port=80", key))
        await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=15)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            print(f"{size} hs-fail", flush=True)
            w.close()
            return False
        body = b"x" * size
        req = (f"POST /x HTTP/1.1\r\nHost: example.com\r\nContent-Length: {size}\r\n"
               f"Connection: close\r\n\r\n").encode() + body
        w.write(wscodec.encode_frame(req, mask=True))
        await w.drain()
        try:
            fr = await asyncio.wait_for(wscodec.read_frame(r), timeout=15)
            print(f"{size} -> {None if fr is None else f'{len(fr[1])}B'}", flush=True)
            ok = fr is not None
        except TimeoutError:
            print(f"{size} -> STALL", flush=True)
            ok = False
        w.close()
        return ok
    except Exception as exc:
        print(f"{size} ERR {type(exc).__name__}", flush=True)
        return False


async def main(base: str) -> int:
    results = []
    for size in SIZES:
        results.append(await probe(base, size))
        await asyncio.sleep(1)
    return 0 if all(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("VSP_API_BASE", ""))
    args = ap.parse_args()
    if not args.base:
        ap.error("set VSP_API_BASE or pass --base, e.g. VSP_API_BASE=https://<pool-host>")
    raise SystemExit(asyncio.run(main(args.base)))
