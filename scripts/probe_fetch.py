#!/usr/bin/env python3
"""Pool /fetch check (http + https). Usage:
    python3 scripts/probe_fetch.py [--base https://pool.example.invalid]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) probe-fetch/1.0"}


def fetch(base: str, url: str) -> bool:
    try:
        req = urllib.request.Request(f"{base}/fetch?url={url}", headers=_UA)
        with urllib.request.urlopen(req, timeout=40) as res:
            d = json.load(res)
        ok = d.get("ok") is True and 200 <= int(d.get("status", 0)) < 400
        print(f"{url}: ok={d.get('ok')} status={d.get('status')} err={str(d.get('error',''))[:80]}")
        return ok
    except Exception as exc:
        print(f"{url}: FAILED {str(exc)[:120]}")
        return False


def main(base: str) -> int:
    ok = fetch(base, "http://example.com")
    ok = fetch(base, "https://example.com") and ok
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://pool.example.invalid")
    args = ap.parse_args()
    raise SystemExit(main(args.base))
