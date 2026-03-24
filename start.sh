#!/usr/bin/env bash
# Render: run poller + gunicorn in one container so they share one SQLite file.
set -euo pipefail
cd "$(dirname "$0")"
LEAGUE="${LEAGUE:-Mirage}"
INTERVAL="${POLL_INTERVAL:-3}"
python poller.py --league "$LEAGUE" --interval "$INTERVAL" &
exec gunicorn --bind "0.0.0.0:${PORT}" --workers 1 --threads 2 dashboard:app
