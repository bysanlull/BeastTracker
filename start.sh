#!/usr/bin/env bash
# Render: poller + gunicorn in one container (shared SQLite). Requires PORT (Render sets it).
set -eo pipefail
cd "$(dirname "$0")"

# Render sets PORT for web services; default avoids bash 'nounset' / empty bind during health probes
export PORT="${PORT:-5000}"

LEAGUE="${LEAGUE:-Mirage}"
INTERVAL="${POLL_INTERVAL:-3}"

python3 poller.py --league "$LEAGUE" --interval "$INTERVAL" &

exec python3 -m gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads 2 \
  --timeout 120 \
  dashboard:app
