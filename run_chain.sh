#!/bin/bash
# Run browser skills one after another (they share one Chrome profile). Usage: ./run_chain.sh discover|apply [limit]
MODE=${1:-discover}; LIMIT=${2:-12}; API=localhost:8000/api
for P in instahyre cutshort hirist naukri wellfound ycombinator peerlist linkedin; do
  if [ "$P" = "linkedin" ]; then M=$([ "$MODE" = "apply" ] && echo easy_apply || echo discover); BODY="{\"params\":{\"mode\":\"$M\",\"max_pages\":1,\"limit\":$LIMIT}}"; else BODY="{\"params\":{\"mode\":\"$MODE\",\"limit\":$LIMIT}}"; fi
  RID=$(curl -s -m 10 -X POST $API/runs/skill/$P -H 'Content-Type: application/json' -d "$BODY" | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')
  echo "$(date +%H:%M:%S) START $P run=$RID"
  for i in $(seq 1 240); do S=$(curl -s -m 10 "$API/runs?limit=20" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$RID];print(rs[0]['status'] if rs else 'gone')"); [ "$S" != "running" ] && [ "$S" != "paused_for_human" ] && break; sleep 5; done
  curl -s -m 10 "$API/runs?limit=20" | python3 -c "import sys,json;rs=[r for r in json.load(sys.stdin) if r['id']==$RID];r=rs[0];print('$(date +%H:%M:%S) END  ',r['name'],r['status'],r['stats'],(r['error'] or '')[:200])"
  sleep 3
done
echo "CHAIN DONE"
