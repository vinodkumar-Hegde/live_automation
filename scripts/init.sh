#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -e .env ]]; then echo 'Existing .env preserved'; exit 0; fi
cp .env.example .env
secret=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
python3 - "$secret" <<'PY'
from pathlib import Path
import sys
p=Path('.env');p.write_text(p.read_text().replace('REPLACE_ME_WITH_64_CHARACTER_RANDOM_STRING',sys.argv[1]))
PY
echo 'Generated .env. Add Zoom credentials after local login test.'
