#!/bin/bash
API=localhost:8000/api
wait_run() { for i in $(seq 1 720); do S=$(curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$1];print(rs[0]['status'] if rs else 'gone')"); [ "$S" != "running" ] && [ "$S" != "paused_for_human" ] && break; sleep 10; done; curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$1];r=rs[0];print('$(date +%H:%M:%S) END  ',r['name'],r['status'],r['stats'],(r['error'] or '')[:160])"; }
until grep -q "FULL APPLY DONE" data/full_apply.log; do sleep 20; done
for M in find_people connect; do
  RID=$(curl -s -X POST $API/runs/skill/linkedin -H 'Content-Type: application/json' -d "{\"params\":{\"mode\":\"$M\",\"limit\":20}}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START linkedin $M run=$RID"; wait_run $RID; sleep 3
done
RID=$(curl -s -X POST $API/runs/service/gmail_send | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START gmail_send run=$RID"; wait_run $RID
curl -s -X POST $API/runs/service/sheets_sync >/dev/null; echo "AFTER DONE"
