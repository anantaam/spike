# spike

Live breakout-thrust + volume-confirmed-retest scanner for NSE F&O names, via
Kite Connect. Detection only — it posts a Discord alert (ticker, direction,
zone, entry, stop, target) when a setup's retest confirms on the latest
candle close; it does not place orders.

The detection logic (thrust run, source-anchored adaptive base, recent
high/low level check, retest, baseline-relative volume decline) is the
validated result of a historical study across the full F&O universe on
5min/15min/1h/1D — see `spike/detector.py` and `spike/config.py` for the
settled thresholds.

## Data independence

All historical and live candle data — 1-min (for 5min/15min/1h) and native
daily bars (for 1D) — is backfilled via `kite.historical_data()` straight
into spike's own `data_cache/`, and kept current from there every scan
cycle. Nothing from any other project's directory is ever read. Deleting any
sibling project on this box does not affect spike.

The one deliberate exception is the Kite *session* (`spike/kite_session.py`),
purely as a startup-time optimization, not a dependency:
1. A shared token file at `SPIKE_KITE_TOKEN_FILE` (e.g. another project's
   already-logged-in session) — read-only, never written to.
2. Its own previously-persisted token (`state/kite_token.json`).
3. A full independent TOTP login, if `KITE_API_KEY`/`KITE_API_SECRET`/
   `KITE_USER_ID`/`KITE_PASSWORD`/`KITE_TOTP_SECRET` are set — and persists
   its own token afterward.

If step 1's file doesn't exist (e.g. a sibling project was deleted, or a
fresh box), spike just logs in itself via step 3 and carries on — nothing
else changes.

## Deploying on a (this or another) box

```bash
git clone git@github.com:anantaam/spike.git
cd spike
./install.sh          # venv, deps, .env from template, systemd unit
nano .env              # fill in secrets / paths for THIS box
sudo systemctl enable --now spike.service
journalctl -u spike -f  # watch it run
```

To stop gracefully without touching systemd: `touch KILL` in the project
root — the loop checks for this file each cycle and exits cleanly.

## Manual/dev run (no systemd)

```bash
set -a; source .env; set +a
./venv/bin/python -m spike.main
```

## Config

See `.env.example` for every variable. The ones worth knowing:

- `SPIKE_TIMEFRAMES` — which timeframes to scan (default `5min,15min,1h,1D`).
  1D bars are fetched natively at day interval (not resampled from 1-min,
  which isn't kept deep enough for that) and backfilled to ~450 calendar days
  to comfortably clear LEVEL_LOOKBACK=60 + BASE_MAX=40 trading-day bars.
  Scheduling is calendar-day based (fires once, ~10min after the 15:30 IST
  close, within a same-day catch-up window), unlike the other timeframes'
  minutes-since-midnight boundaries.
- `SPIKE_UNIVERSE` — comma-separated ticker override. Empty = auto-discover
  current F&O underlyings from a fresh `kite.instruments()` call.
- `SPIKE_HIST_DELAY` — seconds between `historical_data` calls during the
  per-cycle universe refresh; keeps requests under Kite's rate limit.

## What this is not (yet)

No order placement, no position sizing, no risk controls. That was a
deliberate call: alert-only lets you validate signal quality against real
market conditions before trusting any capital to automation. See the
detector's historical validation notes for context on hit rates and the
close-stop vs. touch-stop distinction before treating any of this as
production-ready for real money.
