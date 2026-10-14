#!/usr/bin/env bash
#
# Container entrypoint for the single-container Plexus deployment.
#
# The StreamLit Plexus template expects ONE container serving HTTP on ONE port.
# This app has two cooperating processes: the FastAPI API (which also serves the
# built React UI) and one or more conversion workers that drain the job queue.
# Both run in this container; the API is the foreground process the platform
# health-checks, and the worker(s) run in the background.
#
# Environment:
#   PORT                 HTTP port to serve on (default 8501, the template default)
#   WORKER_CONCURRENCY   number of conversion workers to run (default 1)
#   DATABASE_URL         Postgres job store (unset -> local SQLite; not stateless)
#   WEBAPP_S3_BUCKET     S3 bucket for file storage (unset -> local disk; not stateless)
#   WEBAPP_DISABLE_SFTP  set true on Plexus to hide SFTP/host-only features
set -euo pipefail

cd "$(dirname "$0")/backend"

PORT="${PORT:-8501}"
WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-1}"

echo "[entrypoint] starting ${WORKER_CONCURRENCY} conversion worker(s)"
for i in $(seq 1 "${WORKER_CONCURRENCY}"); do
  python worker.py &
done

echo "[entrypoint] starting API on 0.0.0.0:${PORT}"
exec uvicorn api:app --host 0.0.0.0 --port "${PORT}"
