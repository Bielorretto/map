#!/bin/sh
# Lance l'API et l'interface : http://localhost:8000
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
( sleep 6 && open http://localhost:8000 2>/dev/null || true ) &
exec .venv/bin/python -m uvicorn api.server:app --port "${PORT:-8000}"
