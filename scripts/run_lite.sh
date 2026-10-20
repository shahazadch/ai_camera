#!/usr/bin/env bash
# Run SentinelVision without Docker: one Python process + SQLite.
# No Postgres, Redis, Celery or MinIO; background jobs run inline (eager).
#
#   ./scripts/run_lite.sh            backend on :8000
#   (in another terminal) cd frontend && npm install && npm run dev   -> :5173
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

export DATABASE_URL="sqlite:///$ROOT/backend/db.sqlite3"
export CELERY_TASK_ALWAYS_EAGER=True
export EVIDENCE_STORAGE_BACKEND=local
export DEBUG="${DEBUG:-True}"
export CAMERA_MONITOR_INTERVAL="${CAMERA_MONITOR_INTERVAL:-10}"
export CONTINUOUS_RECORDING="${CONTINUOUS_RECORDING:-False}"
export OPENCV_THREADS="${OPENCV_THREADS:-2}"
export OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp|stimeout;8000000|rw_timeout;8000000|max_delay;500000"

.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py seed_dev_data
exec .venv/bin/gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 --workers 1 --threads 4 --timeout 120
