#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/init.sh
if [[ ! -d .venv ]]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -q -r requirements.txt
set -a; source .env; set +a
read -rp 'Admin email address: ' address
.venv/bin/python -m app.cli create-admin --email "$address"
echo 'Setup complete. Next run ./scripts/run.sh'
