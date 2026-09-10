#!/usr/bin/env python3
"""Live /debug/cli probe against a pool. Env:
    VSP_API_BASE=https://<pool-host>  VSP_DEBUG_KEY=<key from just debug-key>
   Usage: python3 scripts/probe_debug.py [--instance 1]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("VSP_API_BASE", "https://pool.example.invalid")
KEY = os.environ.get("VSP_DEBUG_KEY", "")


def call(payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE}/debug/cli", data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "probe-debug/1.0",
                 **({"X-Debug-Key": KEY} if KEY else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception as exc:
        return -1, {"error": str(exc)[:120]}


def main(instance: int) -> int:
    if not KEY:
        print("set VSP_DEBUG_KEY first (just debug-key on the pool box)")
        return 2
    st, res = call({"instance": instance, "args": ["--version"]})
    print(f"version: http={st} {res.get('stdout', '')!r:.60}")
    if st != 200:
        return 1
    st, res = call({"instance": instance, "args": ["status"]})
    print(f"status: http={st} {res.get('stdout', '').strip().replace(chr(10), ' | ')[:160]}")
    return 0 if st == 200 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", type=int, default=1)
    args = ap.parse_args()
    sys.exit(main(args.instance))
