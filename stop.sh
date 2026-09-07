#!/bin/zsh
# Stop the API and the worker. A run in progress is marked interrupted by the worker on its next start; the browser stays open.
cd "$(dirname "$0")"
pkill -f "backend.core.worker" && echo "worker stopped" || echo "worker was not running"
pkill -f "uvicorn backend.app.main:app" && echo "API stopped" || echo "API was not running"
