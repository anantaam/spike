#!/usr/bin/env bash
# Bootstrap spike on a fresh box: venv, deps, .env, systemd unit.
# Idempotent -- safe to re-run after a git pull.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ]; then
    cp .env.example .env
    echo ">>> Created .env from .env.example -- edit it before starting the service."
fi

mkdir -p state data_cache logs

UNIT_SRC="systemd/spike.service"
UNIT_DST="/etc/systemd/system/spike.service"
if [ -w /etc/systemd/system ] || sudo -n true 2>/dev/null; then
    sudo cp "$UNIT_SRC" "$UNIT_DST"
    sudo systemctl daemon-reload
    echo ">>> Installed $UNIT_DST. Enable/start with:"
    echo "      sudo systemctl enable --now spike.service"
else
    echo ">>> No sudo access -- copy $UNIT_SRC to /etc/systemd/system/spike.service yourself,"
    echo "    then: sudo systemctl daemon-reload && sudo systemctl enable --now spike.service"
fi

echo ">>> Done. Edit .env, then start the service (see above)."
