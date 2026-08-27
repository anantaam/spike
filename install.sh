#!/usr/bin/env bash
# Bootstrap spike on a fresh box: venv, deps, .env, systemd units, shared ops.
# Idempotent -- safe to re-run after a git pull.
#
# Everything needed to rebuild from scratch is in this repo EXCEPT .env, which
# holds credentials and is deliberately untracked. Restore that from your own
# backup, or fill it in from .env.example.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ]; then
    cp .env.example .env
    echo ">>> Created .env from .env.example -- fill it in before starting anything."
fi
chmod 600 .env

mkdir -p state data_cache logs

if [ -w /etc/systemd/system ] || sudo -n true 2>/dev/null; then
    sudo cp systemd/spike.service systemd/crypto.service /etc/systemd/system/

    # Shared pre-market restart: any algo on this box whose broker session
    # expires daily can register in services.conf. Lives outside the repo tree
    # on purpose so other projects can use it.
    sudo mkdir -p /opt/market-ops
    sudo cp ops/premarket-restart.sh /opt/market-ops/
    sudo cp -n ops/services.conf /opt/market-ops/ 2>/dev/null || true   # never clobber a live list
    sudo chmod 755 /opt/market-ops/premarket-restart.sh
    sudo cp systemd/premarket-restart.service systemd/premarket-restart.timer /etc/systemd/system/

    sudo systemctl daemon-reload
    echo ">>> Units installed. Enable with:"
    echo "      sudo systemctl enable --now spike.service crypto.service premarket-restart.timer"
else
    echo ">>> No sudo -- copy systemd/*.service and systemd/*.timer to /etc/systemd/system/,"
    echo "    and ops/* to /opt/market-ops/, then daemon-reload and enable."
fi

echo ">>> Done. Fill in .env, then enable the services above."
echo ">>> The equity scanner needs a Kite Connect app WITH historical data API."
echo ">>> The crypto scanner needs no credentials at all (Binance public API)."
