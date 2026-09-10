#!/usr/bin/env python3
"""Full edge end-to-end: http/https/exit-ip/rotate via local edge proxy. Usage:
    python3 scripts/probe_edge.py [--edge http://127.0.0.1:8080]
"""
from __future__ import annotations

import argparse
import json
import urllib.request


def via_edge(edge: str, url: str, timeout: int = 40) -> tuple[bool, str]:
    proxy = urllib.request.ProxyHandler({"http": edge, "https": edge})
    opener = urllib.request.build_opener(proxy)
    try:
        with opener.open(url, timeout=timeout) as res:
            return True, f"{res.status} {len(res.read())}B"
    except Exception as exc:
        return False, str(exc)[:100]


def main(edge: str) -> int:
    ok = True
    for url in ("http://example.com", "https://example.com", "https://ifconfig.me/ip"):
        good, detail = via_edge(edge, url)
        print(f"{url}: {'PASS' if good else 'FAIL'} {detail[:80]}")
        ok = ok and good
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"{edge}/rotate", method="POST"), timeout=20) as res:
            print("rotate:", json.load(res))
    except Exception as exc:
        print(f"rotate: FAIL {str(exc)[:80]}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge", default="http://127.0.0.1:8080")
    args = ap.parse_args()
    raise SystemExit(main(args.edge))
