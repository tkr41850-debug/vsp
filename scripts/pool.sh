#!/bin/bash
# Pool health snapshot. Usage: VSP_API_BASE=https://<pool> bash scripts/pool.sh
set -uo pipefail
BASE="${VSP_API_BASE:-}"
if [ -z "$BASE" ]; then
    echo "set VSP_API_BASE, e.g. VSP_API_BASE=https://<pool-host> just pool" >&2
    exit 2
fi
curl -s --max-time 25 "$BASE/health" | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print('unreachable:', exc); sys.exit(1)
print('active:', d.get('active'))
for w in d.get('warps', []):
    print(f\"{w['idx']} ready={w['ready']} status={w.get('status')} | {w.get('reason','')[:50]} | reg={w['registered']} err={w.get('error','')[:40]}\")"
