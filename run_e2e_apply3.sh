#!/bin/bash
API=localhost:8000/api
wait_run() { for i in $(seq 1 240); do S=$(curl -s -m 10 "$API/runs?limit=20" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$1];print(rs[0]['status'] if rs else 'gone')"); [ "$S" != "running" ] && [ "$S" != "paused_for_human" ] && break; sleep 5; done; curl -s -m 10 "$API/runs?limit=20" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$1];r=rs[0];print('$(date +%H:%M:%S) END  ',r['name'],r['status'],r['stats'],(r['error'] or '')[:200])"; }
sleep 8
RID=$(curl -s -X POST $API/runs/skill/wellfound -H 'Content-Type: application/json' -d '{"params":{"mode":"apply","limit":1}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START wellfound run=$RID"; wait_run $RID; sleep 3
RID=$(curl -s -X POST $API/runs/skill/ats_apply -H 'Content-Type: application/json' -d '{"params":{"limit":4}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START ats_apply run=$RID"; wait_run $RID
echo "E2E APPLY DONE"
