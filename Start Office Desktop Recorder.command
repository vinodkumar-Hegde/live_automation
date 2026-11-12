#!/bin/bash
# DocTutorials office desktop recorder: joins each live class as a viewer and records it on this computer.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
unset ELECTRON_RUN_AS_NODE
cd "$(dirname "$0")/office_desktop"
if ! command -v node >/dev/null 2>&1 || ! command -v ffmpeg >/dev/null 2>&1; then
  echo 'One-time requirement: run  brew install node ffmpeg  then reopen this file.'
  read -r -p 'Press Return to close…' _
  exit 1
fi
if [[ ! -d node_modules/electron/dist ]]; then
  echo 'First run: downloading the recorder runtime (about 100 MB)…'
  npm install --no-audit --no-fund
  [[ -d node_modules/electron/dist ]] || node node_modules/electron/install.js
fi
exec ./node_modules/.bin/electron .
