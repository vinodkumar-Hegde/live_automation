#!/usr/bin/env bash
# ./scripts/run.sh         this Mac only (127.0.0.1)
# ./scripts/run.sh --lan   also reachable from other computers on the same LAN,
#                          so an office recorder on another laptop can connect.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env || ! -x .venv/bin/uvicorn ]]; then
  echo 'First run ./scripts/setup.sh' >&2; exit 1
fi
set -a; source .env; set +a
host=127.0.0.1
if [[ "${1:-}" == "--lan" ]]; then
  host=0.0.0.0
  address=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
  echo "LAN mode. On the office laptop, pair the recorder with: http://${address:-<this-computer-LAN-IP>}:8792"
  echo "Faculty keep using the usual HTTPS address (or http://127.0.0.1:8792 on this computer)."
fi
exec .venv/bin/uvicorn app.main:app --host "$host" --port 8792 --no-access-log
