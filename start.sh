#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-10000}"
echo "Starting gunicorn on 0.0.0.0:${PORT}"
exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers 1 \
  --threads 4 \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  app:app
