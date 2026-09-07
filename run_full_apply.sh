#!/bin/bash
# Apply everything approved: ATS worker (company forms) then each platform skill. Auto-approves freshly prepared apps first (user said approve all).
API=localhost:8000/api
wait_run() { for i in $(seq 1 720); do S=$(curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$1];print(rs[0]['status'] if rs else 'gone')"); [ "$S" != "running" ] && [ "$S" != "paused_for_human" ] && break; sleep 10; done; curl -s -m 10 "$API/runs?limit=30" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$1];r=rs[0];print('$(date +%H:%M:%S) END  ',r['name'],r['status'],r['stats'],(r['error'] or '')[:160])"; }
approve_new() { .venv/bin/python -c "
from backend.app.db import session; from backend.app.models import Application
with session() as db:
    n=0
    for a in db.query(Application).filter(Application.status=='pending_review').all(): a.status='approved'; n+=1
    print('auto-approved', n)"; }
approve_new
RID=$(curl -s -X POST $API/runs/skill/ats_apply -H 'Content-Type: application/json' -d '{"params":{"limit":60}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START ats_apply run=$RID"; wait_run $RID
for P in instahyre cutshort hirist naukri wellfound ycombinator peerlist; do
  approve_new
  RID=$(curl -s -X POST $API/runs/skill/$P -H 'Content-Type: application/json' -d '{"params":{"mode":"apply","limit":10}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START $P run=$RID"; wait_run $RID; sleep 3
done
RID=$(curl -s -X POST $API/runs/skill/linkedin -H 'Content-Type: application/json' -d '{"params":{"mode":"easy_apply","limit":10}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START linkedin easy_apply run=$RID"; wait_run $RID
approve_new
RID=$(curl -s -X POST $API/runs/skill/ats_apply -H 'Content-Type: application/json' -d '{"params":{"limit":60}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])'); echo "$(date +%H:%M:%S) START ats_apply(second pass) run=$RID"; wait_run $RID
curl -s -X POST $API/runs/service/sheets_sync >/dev/null
echo "FULL APPLY DONE"
