#!/usr/bin/env bash
# Runs relay + mock drone on this machine. Dashboard: http://127.0.0.1:8000/?token=$VAYUVEER_TOKEN
set -euo pipefail
cd "$(dirname "$0")"
export VAYUVEER_TOKEN="${VAYUVEER_TOKEN:-dev-token}"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt -r drone/requirements.txt; }
.venv/bin/uvicorn main:app --app-dir server --host 127.0.0.1 --port 8000 &
RELAY=$!
sleep 1
( cd drone && VAYUVEER_SERVER_URL=ws://127.0.0.1:8000 ../.venv/bin/python agent.py )
kill $RELAY
