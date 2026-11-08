#!/bin/bash
set -euo pipefail

# Single worker is mandatory: app.py takes an exclusive flock on
# <AUDIT_LOG_PATH>.lock at boot, and the audit chain + tools._LOCK are
# process-local. A second worker fails to start by design.
uvicorn app:app \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 1 &
UV=$!

nginx -g 'daemon off;' &
NG=$!

trap 'kill "$UV" "$NG" 2>/dev/null || true' TERM INT

# If either process dies, take the container down so Coolify restarts it.
wait -n || true
kill "$UV" "$NG" 2>/dev/null || true
exit 1
