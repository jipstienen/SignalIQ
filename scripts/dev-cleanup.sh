#!/usr/bin/env bash
# Stop local Next.js / uvicorn dev servers on ports we use in this repo.
set -euo pipefail
PORTS=(3000 3005 3007 3008 3010 3011 8000 8011)
for port in "${PORTS[@]}"; do
  pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
  if [[ -n "${pids}" ]]; then
    echo "Stopping port ${port}: ${pids}"
    kill -9 ${pids} 2>/dev/null || true
  fi
done
echo "Done. Ports: ${PORTS[*]}"
