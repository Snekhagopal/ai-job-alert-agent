#!/usr/bin/env bash
# Run ON THE SERVER from the project directory after `docker compose up -d`.
set -euo pipefail
source <(grep -E '^(BACKEND_API_KEY)=' .env)
API=http://127.0.0.1:8000
pass() { printf "  \033[32mPASS\033[0m %s\n" "$1"; }
fail() { printf "  \033[31mFAIL\033[0m %s\n" "$1"; FAILED=1; }
FAILED=0

echo "1. Containers"
for svc in db backend dashboard n8n; do
  state=$(docker compose ps --format '{{.Service}} {{.State}} {{.Health}}' | awk -v s=$svc '$1==s{print $2" "$3}')
  [[ "$state" == running* ]] && pass "$svc: $state" || fail "$svc: ${state:-not running}"
done

echo "2. Backend health"
curl -fsS $API/health >/dev/null && pass "/health" || fail "/health"
code=$(curl -s -o /dev/null -w '%{http_code}' $API/api/stats)
[[ "$code" == 401 ]] && pass "API rejects requests without key" || fail "API without key returned $code"

echo "3. One monitoring cycle (forced)"
out=$(curl -fsS -X POST -H "X-API-Key: $BACKEND_API_KEY" "$API/api/run-cycle?force=true" || true)
echo "     $out" | cut -c1-400
[[ "$out" == *sources_ok* ]] && pass "cycle completed" || fail "cycle failed"

echo "4. Source health"
curl -fsS -H "X-API-Key: $BACKEND_API_KEY" $API/api/health/sources \
  | python3 -c "import json,sys;d=json.load(sys.stdin);[print('     %-38s %s %s'%(s['source'],'ok ' if s['healthy'] else 'BAD',s['last_error'][:80])) for s in d['sources']]"

echo "5. Persistence (restart backend, data must survive)"
before=$(curl -fsS -H "X-API-Key: $BACKEND_API_KEY" $API/api/stats | python3 -c "import json,sys;print(json.load(sys.stdin)['total_discovered'])")
docker compose restart backend >/dev/null
for i in $(seq 1 30); do curl -fsS $API/health >/dev/null 2>&1 && break; sleep 2; done
after=$(curl -fsS -H "X-API-Key: $BACKEND_API_KEY" $API/api/stats | python3 -c "import json,sys;print(json.load(sys.stdin)['total_discovered'])")
[[ "$before" == "$after" && "$after" -gt 0 ]] && pass "jobs before/after restart: $before/$after" || fail "jobs before/after restart: $before/$after"

echo "6. n8n workflow"
docker compose exec -T n8n n8n list:workflow 2>/dev/null | grep -q "AI Job Monitor" && pass "workflow imported" || fail "workflow not imported"

echo
[[ $FAILED == 0 ]] && echo "All checks passed. Send a test email from the dashboard (Source health tab) to verify Gmail." || { echo "Some checks failed."; exit 1; }
