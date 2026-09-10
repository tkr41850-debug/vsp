#!/usr/bin/env python3
"""TLS handshake through pool /relay. Usage:
    python3 scripts/probe_tls.py [--base URL] [--host example.com]
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


async def main(base: str, host: str) -> int:
    u = urlsplit(base)
    pool_host, pool_port = u.hostname or "", u.port or 443
    ctx = ssl.create_default_context()
    r, w = await asyncio.open_connection(pool_host, pool_port, ssl=ctx, server_hostname=pool_host)
    key = wscodec.new_key()
    w.write(wscodec.client_handshake_request(pool_host, f"/relay?host={host}&port=443", key))
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=15)
    first = head.split(b"\r\n", 1)[0]
    if b" 101 " not in first:
        print(f"handshake failed: {first!r}")
        return 1
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
    except TimeoutError:
        print("TIMEOUT held")
        return 1
    print(f"serverhello: {None if fr is None else len(fr[1])}")
    w.close()
    return 0 if fr else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("VSP_API_BASE", ""))
    ap.add_argument("--host", default="example.com")
    args = ap.parse_args()
    if not args.base:
        ap.error("set VSP_API_BASE or pass --base, e.g. VSP_API_BASE=https://<pool-host>")
    raise SystemExit(asyncio.run(main(args.base, args.host)))
