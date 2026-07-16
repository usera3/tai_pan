#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -e .

if (echo > /dev/tcp/127.0.0.1/8765) >/dev/null 2>&1; then
  echo "Port 8765 is already in use. Close the existing service and try again."
  exit 1
fi

exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
