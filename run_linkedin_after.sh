#!/bin/bash
API=localhost:8000/api
for i in $(seq 1 900); do S=$(curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==113];print(rs[0]['status'] if rs else 'gone')"); [ "$S" != "running" ] && break; sleep 15; done; sleep 5
for M in find_people connect; do
  RID=$(curl -s -X POST $API/runs/skill/linkedin -H 'Content-Type: application/json' -d "{\"params\":{\"mode\":\"$M\",\"limit\":20}}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START linkedin $M run=$RID"
  for i in $(seq 1 240); do S=$(curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$RID];print(rs[0]['status'] if rs else 'gone')"); [ "$S" != "running" ] && [ "$S" != "paused_for_human" ] && break; sleep 10; done
  curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$RID];r=rs[0];print('$(date +%H:%M:%S) END  ',r['name'],r['status'],r['stats'],(r['error'] or '')[:160])"; sleep 3
done
curl -s -X POST $API/runs/service/sheets_sync >/dev/null; echo "LINKEDIN DONE"
