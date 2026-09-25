#!/usr/bin/env bash
# Full image smoke test: start stack → fresh checks → record DB rows → restart → persist checks.
# Usage: ACDASH_IMAGE=acdash:ci ci/smoke/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export ACDASH_IMAGE="${ACDASH_IMAGE:-acdash:ci}"
PROJECT="acdash-smoke"
COMPOSE=(docker compose -f compose.smoke.yml -p "$PROJECT")

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "---- acdash logs ----"; "${COMPOSE[@]}" logs --no-color --tail=200 acdash || true
    echo "---- fake logs ----"; "${COMPOSE[@]}" logs --no-color --tail=50 fake || true
  fi
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  exit $status
}
trap cleanup EXIT

"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build --wait --wait-timeout 90

python3 smoke/smoke.py fresh

count_rows() {
  "${COMPOSE[@]}" exec -T acdash python -c \
    "import sqlite3; print(sqlite3.connect('/app/data/history.db').execute('select count(*) from readings').fetchone()[0])"
}
BEFORE=$(count_rows)
echo "history rows before restart: $BEFORE"
if [ "$BEFORE" -lt 1 ]; then echo "FAIL  collector stored no readings"; exit 1; fi

count_cloud() {
  "${COMPOSE[@]}" exec -T acdash python -c \
    "import sqlite3; print(sqlite3.connect('/app/data/history.db').execute(\"select count(*) from readings where source='cloud'\").fetchone()[0])"
}
CLOUD=0
for _ in $(seq 1 30); do CLOUD=$(count_cloud); [ "$CLOUD" -gt 0 ] && break; sleep 2; done
echo "backfilled cloud rows: $CLOUD"
if [ "$CLOUD" -lt 100 ]; then echo "FAIL  backfill did not fill the new-install gap"; exit 1; fi
echo "PASS  backfill filled history from the (fake) cloud"

"${COMPOSE[@]}" restart acdash
"${COMPOSE[@]}" up -d --wait --wait-timeout 90 acdash

AFTER=$(count_rows)
echo "history rows after restart: $AFTER"
if [ "$AFTER" -lt "$BEFORE" ]; then echo "FAIL  history lost across restart"; exit 1; fi
echo "PASS  history persisted across restart"

python3 smoke/smoke.py persist
echo "SMOKE OK ($ACDASH_IMAGE)"
