#!/usr/bin/env bash
# 배포 직후 최소 검증 (06 Smoke Testing).
#
# 실패하면 즉시 비정상 종료한다 — 스모크가 조용히 넘어가면 배포 확인의
# 의미가 없다.
set -euo pipefail

ROOT="${1:-.}"
ECHO_URL="${AAESTRO_ECHO_URL:-http://127.0.0.1:18081}"

say() { printf '\n== %s\n' "$1"; }

say "1/3 declared contracts validate"
# CLI 가 아직 설치되지 않은 환경(부분 배포)에서도 스모크가 멈추지 않도록,
# entry point 가 없으면 모듈 경로로 떨어진다
if command -v aaestro >/dev/null 2>&1; then
  aaestro --root "$ROOT" validate
elif command -v uv >/dev/null 2>&1; then
  uv run python -m aaestro.cli --root "$ROOT" validate
else
  # uv 도 없으면 현재 인터프리터로 — CLI 미설치 환경을 가정하면서
  # uv 를 전제하는 것은 모순이다
  python3 -m aaestro.cli --root "$ROOT" validate
fi

say "2/3 agent reports healthy"
status=$(curl -fsS --max-time 5 "$ECHO_URL/v1/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
[ "$status" = "healthy" ] || { echo "agent is $status"; exit 1; }
echo "healthy"

say "3/3 direct request round-trips"
result=$(curl -fsS --max-time 10 -X POST "$ECHO_URL/v1/invoke" \
  -H 'content-type: application/json' \
  -d '{"task_id":"smoke-1","run_id":"direct-smoke","node_id":null,
       "input":{"msg":"smoke"},"trace":{"trace_id":"trace-smoke"}}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["status"], d["output"].get("msg"))')
[ "$result" = "completed smoke" ] || { echo "unexpected result: $result"; exit 1; }
echo "$result"

printf '\nsmoke passed\n'
