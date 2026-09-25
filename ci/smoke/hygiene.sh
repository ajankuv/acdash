#!/usr/bin/env bash
# Fail if the image ships files or packages that must never be published.
# Usage: ci/smoke/hygiene.sh acdash:ci
set -euo pipefail
IMAGE="${1:-acdash:ci}"

docker run --rm --entrypoint sh "$IMAGE" -c '
fail=0
for p in /app/tests /app/RND /app/openspec /app/ci /app/.git /app/.github; do
  if [ -e "$p" ]; then echo "FAIL  image contains $p"; fail=1; fi
done
envs=$(find /app -name ".env" -o -name "*.env" -o -name ".env.*" 2>/dev/null | grep -v "^/app/data/" || true)
if [ -n "$envs" ]; then echo "FAIL  image contains env files: $envs"; fail=1; fi
if ls /app/app/qc_*.py >/dev/null 2>&1; then echo "FAIL  image contains app/qc_*.py"; fail=1; fi
dumps=$(find /app -name "*dump*.json" -o -name "*.db" -o -name "*.sqlite" 2>/dev/null || true)
if [ -n "$dumps" ]; then echo "FAIL  image contains data/dump files: $dumps"; fail=1; fi
if pip show pytest >/dev/null 2>&1; then echo "FAIL  pytest installed in runtime image"; fail=1; fi
[ $fail -eq 0 ] && echo "PASS  image hygiene"
exit $fail
'
