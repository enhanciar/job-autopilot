#!/bin/bash
API=localhost:8000/api
until grep -q "AFTER DONE" data/after_apply.log; do sleep 20; done
sleep 5
RID=$(curl -s -X POST $API/runs/skill/ats_apply -H 'Content-Type: application/json' -d '{"params":{"limit":90}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START ats_apply(third pass) run=$RID"
for i in $(seq 1 900); do S=$(curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$RID];print(rs[0]['status'] if rs else 'gone')"); [ "$S" != "running" ] && break; sleep 20; done
for P in wellfound ycombinator; do RID=$(curl -s -X POST $API/runs/skill/$P -H 'Content-Type: application/json' -d '{"params":{"mode":"apply","limit":5}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); for i in $(seq 1 60); do S=$(curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$RID];print(rs[0]['status'] if rs else 'gone')"); [ "$S" != "running" ] && break; sleep 10; done; done
curl -s -X POST $API/runs/service/sheets_sync >/dev/null; echo "THIRD PASS DONE"
