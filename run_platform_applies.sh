#!/bin/bash
# Platform apply skills share one logged-in Chrome profile, so they run one after another (the ATS worker runs alongside in its own profiles).
API=localhost:8000/api
wait_run() { for i in $(seq 1 120); do S=$(curl -s -m 10 "$API/runs?limit=40" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$1];print(rs[0]['status'] if rs else 'gone')"); case "$S" in running|paused_for_human) sleep 15;; *) echo "$(date +%H:%M:%S) run $1 -> $S"; return;; esac; done; }
for SPEC in "linkedin:easy_apply" "naukri:apply" "instahyre:apply" "cutshort:apply"; do
  P=${SPEC%%:*}; M=${SPEC##*:}
  RID=$(curl -s -X POST $API/runs/skill/$P -H 'Content-Type: application/json' -d "{\"params\":{\"mode\":\"$M\",\"limit\":10}}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')
  echo "$(date +%H:%M:%S) START $P $M run=$RID"; wait_run $RID
done
echo "PLATFORM APPLIES DONE"
