#!/bin/sh
set -e

# 1. Bring the schema up to date (waits for the database if it is still starting).
python -m app.migrate

# 2. Start the API. "exec" makes uvicorn PID 1, so it receives SIGTERM from
#    "docker compose stop/down" directly and can shut down gracefully: stop
#    accepting connections, finish in-flight requests, then close the DB pool.
exec uvicorn app.main:create_app --factory \
    --host 0.0.0.0 --port 8080 \
    --workers "${WEB_CONCURRENCY:-4}" \
    --loop uvloop --http httptools \
    --no-access-log \
    --timeout-graceful-shutdown 20
