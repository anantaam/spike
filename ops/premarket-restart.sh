#!/usr/bin/env bash
# Restart each configured service so it picks up a fresh broker session.
# Best-effort per service: one failure must not stop the rest.
set -uo pipefail

CONF="${1:-/opt/market-ops/services.conf}"
rc=0

if [[ ! -r "$CONF" ]]; then
    echo "premarket-restart: cannot read $CONF" >&2
    exit 1
fi

while read -r svc; do
    svc="${svc%%#*}"
    svc="$(echo -n "$svc" | tr -d '[:space:]')"
    [[ -z "$svc" ]] && continue

    if ! systemctl list-unit-files "$svc" --no-legend | grep -q .; then
        echo "premarket-restart: $svc not installed -- skipping"
        continue
    fi
    if systemctl restart "$svc"; then
        echo "premarket-restart: restarted $svc"
    else
        echo "premarket-restart: FAILED to restart $svc" >&2
        rc=1
    fi
done < "$CONF"

exit $rc
