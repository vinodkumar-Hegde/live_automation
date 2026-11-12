#!/bin/bash
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null 2>&1 || ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3,11))' >/dev/null 2>&1; then
  echo 'One-time requirement: run brew install python ffmpeg, then reopen this file.'
  read -r -p 'Press Return to close…' _
  exit 1
fi
if [[ ! -x .office-venv/bin/python ]]; then
  python3 -m venv .office-venv
fi
if ! .office-venv/bin/python -c 'import httpx, fastapi, uvicorn' >/dev/null 2>&1; then
  .office-venv/bin/python -m pip install -r requirements-office.txt
fi
exec .office-venv/bin/python -m office_recorder
