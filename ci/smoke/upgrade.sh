#!/usr/bin/env bash
# Upgrade + rollback test on ONE data volume:
#   old image (setup wizard, collect history) → new image (data kept, migrations applied)
#   → old image again (rollback still works on the migrated DB).
# Runs on an internal-only Docker network; the fake API answers as www.acinfinityserver.com.
# Usage: ci/smoke/upgrade.sh <old-image> <new-image>
set -euo pipefail
cd "$(dirname "$0")/.."
OLD="${1:?old image}"; NEW="${2:?new image}"
PROJECT="acdash-upgrade"
C=(docker compose -f compose.upgrade.yml -p "$PROJECT")

fail() { echo "FAIL  $*"; "${C[@]}" logs --no-color --tail=80 acdash || true; exit 1; }
export ACDASH_IMAGE="$OLD"
cleanup() { "${C[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

probe() {  # probe <python code> [args...] — runs inside the sealed network
  local code="$1"; shift
  "${C[@]}" exec -T probe python -c "$code" "$@"
}
http() {  # http METHOD PATH [form-body] → prints "status body"
  probe "
import urllib.request, urllib.error, sys
req = urllib.request.Request('http://acdash:8080$2', data=(sys.argv[1].encode() if len(sys.argv) > 1 and sys.argv[1] else None), method='$1')
req.add_header('Content-Type', 'application/x-www-form-urlencoded')
class N(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
try:
    r = urllib.request.build_opener(N).open(req, timeout=20); print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
" "${3:-}"
}
db() {
  "${C[@]}" exec -T acdash python -c "import sqlite3; c=sqlite3.connect('/app/data/history.db'); print($1)"
}
up() {
  export ACDASH_IMAGE="$1"
  "${C[@]}" up -d --build --wait --wait-timeout 120 >/dev/null
  for _ in $(seq 1 30); do
    out=$(http GET /health 2>/dev/null || true)
    case "$out" in 200*) return 0;; esac
    sleep 2
  done
  fail "$1 never became healthy"
}

echo "== 1. old image: $OLD"
up "$OLD"
out=$(http POST /setup "email=ci%40example.com&password=ci-password"); case "$out" in 30[23]*) ;; *) fail "wizard on old image: $out";; esac
out=$(http GET /); case "$out" in *"CI Flower Tent"*) echo "PASS  old image dashboard";; *) fail "old dashboard: ${out:0:200}";; esac
sleep 12
OLD_ROWS=$(db "c.execute('select count(*) from readings').fetchone()[0]")
[ "$OLD_ROWS" -gt 0 ] || fail "old image stored no history"
echo "old image rows: $OLD_ROWS"

echo "== 2. upgrade to new image: $NEW"
"${C[@]}" stop acdash >/dev/null
"${C[@]}" rm -f acdash >/dev/null
up "$NEW"
out=$(http GET /); case "$out" in *"CI Flower Tent"*) echo "PASS  saved credentials survive upgrade";; *) fail "new dashboard after upgrade: ${out:0:200}";; esac
# Total rows must not shrink. (The previous image may itself backfill cloud rows, so only
# rows written before any source column existed are guaranteed to be marked 'local'.)
ALL_AFTER=$(db "c.execute('select count(*) from readings').fetchone()[0]")
UNSOURCED=$(db "c.execute(\"select count(*) from readings where source is null or source not in ('local','cloud')\").fetchone()[0]")
[ "$ALL_AFTER" -ge "$OLD_ROWS" ] || fail "rows lost on upgrade ($ALL_AFTER < $OLD_ROWS)"
[ "$UNSOURCED" = "0" ] || fail "$UNSOURCED rows without a valid source after upgrade"
echo "PASS  old readings kept ($ALL_AFTER >= $OLD_ROWS), all rows sourced"
SNAP=$(db "c.execute(\"select count(*) from sqlite_master where name='settings_snapshots'\").fetchone()[0]")
[ "$SNAP" = "1" ] || fail "settings_snapshots table missing"
echo "PASS  migrations applied"
out=$(probe "
import json, urllib.request
r = urllib.request.Request('http://acdash:8080/api/port-control', data=json.dumps({'dev_id':'900000000000000001','port':1,'mode':'manual','state':True,'speed':6}).encode(), headers={'Content-Type':'application/json'}, method='POST')
print(urllib.request.urlopen(r, timeout=20).read().decode())")
case "$out" in *'"status":"pending"'*) echo "PASS  write works after upgrade";; *) fail "write after upgrade: $out";; esac

echo "== 3. roll back to old image: $OLD"
"${C[@]}" stop acdash >/dev/null
"${C[@]}" rm -f acdash >/dev/null
up "$OLD"
out=$(http GET /); case "$out" in *"CI Flower Tent"*) echo "PASS  old image still runs on migrated data";; *) fail "rollback dashboard: ${out:0:200}";; esac
BEFORE=$(db "c.execute('select count(*) from readings').fetchone()[0]")
sleep 12
AFTER=$(db "c.execute('select count(*) from readings').fetchone()[0]")
[ "$AFTER" -gt "$BEFORE" ] || fail "old image cannot write history to migrated DB"
echo "PASS  old image keeps collecting on migrated DB ($BEFORE → $AFTER)"
echo "UPGRADE OK ($OLD → $NEW → $OLD)"
