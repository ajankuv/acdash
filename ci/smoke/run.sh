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

# Control experiment tool vs the fake API (runs inside the image, which has the app deps).
# The fake only applies speed-only writes that carry onlyUpdateSpeed=1 (the HA #166 hypothesis).
curl -fsS -X POST http://127.0.0.1:19000/__behavior -H 'Content-Type: application/json' \
  -d '{"speed_only_needs": {"onlyUpdateSpeed": "1"}}' >/dev/null
EXP_OUT=$(docker run --rm --network "${PROJECT}_default" -v "$(cd .. && pwd)":/src:ro -w /src \
  -e ACINFINITY_API_BASE=http://fake:9000/api -e ACDASH_EXPERIMENT_DEV_ID=900000000000000001 \
  -e ACDASH_EXPERIMENT_DEV_NAME=Flower -e ACINFINITY_EMAIL=ci@example.com -e ACINFINITY_PASSWORD=ci-password \
  --entrypoint python "$ACDASH_IMAGE" tools/control_experiment.py --port 1 --live --confirm "CI Flower Tent" \
  --window 12 --poll 2 --write-gap 2 --report-dir /tmp/exp 2>&1) || { echo "$EXP_OUT"; echo "FAIL  control experiment tool exited non-zero"; exit 1; }
SUMMARY=$(echo "$EXP_OUT" | grep "^SUMMARY ")
echo "$SUMMARY" | grep -q "reproduced_problem=True" \
  && echo "$SUMMARY" | grep -q "applied=\['onlyUpdateSpeed=1', 'onlyUpdateSpeed=1 (repeat)'\]" \
  && echo "$SUMMARY" | grep -q "restore_failed=False" \
  || { echo "$EXP_OUT"; echo "FAIL  experiment summary unexpected: $SUMMARY"; exit 1; }
if echo "$EXP_OUT" | grep -q -i "traceback"; then echo "$EXP_OUT"; echo "FAIL  experiment logged a traceback"; exit 1; fi
echo "PASS  control experiment tool finds the applying variant and restores the port"
curl -fsS -X POST http://127.0.0.1:19000/__behavior -H 'Content-Type: application/json' -d '{"speed_only_needs": null}' >/dev/null

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

# Cloud outage: stop the fake API; data must go stale on /status while /health stays up.
"${COMPOSE[@]}" stop fake >/dev/null
python3 smoke/smoke.py stale
echo "SMOKE OK ($ACDASH_IMAGE)"
