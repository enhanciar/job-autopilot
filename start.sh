#!/bin/zsh
# Start the whole system locally: backup the database, run the additive migration, start the API and the durable worker.
# Safe to re-run: each process is started only if it is not already running. Logs go to data/*.log.
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python
mkdir -p data/backups
if [ -f data/autopilot.db ]; then
  $PY -m backend.core.ops backup --path "$PWD/data/backups/autopilot-$(date +%Y%m%d-%H%M%S).db" >/dev/null
  ls -t data/backups/autopilot-*.db | tail -n +15 | xargs -I{} rm -f {}   # keep the newest 14 backups
fi
$PY -c "from backend.app.db import init_db; init_db()"                       # additive migrations only
if ! pgrep -f "uvicorn backend.app.main:app" >/dev/null; then
  nohup .venv/bin/uvicorn backend.app.main:app --port 8000 >> data/backend.log 2>&1 &
  echo "API started on http://localhost:8000"
else
  echo "API already running"
fi
if ! pgrep -f "backend.core.worker" >/dev/null; then
  nohup $PY -m backend.core.worker >> data/worker.log 2>&1 &
  echo "worker started (data/worker.log)"
else
  echo "worker already running"
fi
echo "dashboard: http://localhost:8000  (Vite dev server: npm --prefix frontend run dev)"
