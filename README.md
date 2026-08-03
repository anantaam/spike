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

## How it reuses another project's Kite session (and how it doesn't need to)

`spike/kite_session.py` tries, in order:
1. A shared token file at `SPIKE_KITE_TOKEN_FILE` (e.g. another project's
   already-logged-in session) — read-only, never writes there.
2. Its own previously-persisted token (`state/kite_token.json`).
3. A full independent TOTP login, if `KITE_API_KEY`/`KITE_API_SECRET`/
   `KITE_USER_ID`/`KITE_PASSWORD`/`KITE_TOTP_SECRET` are set — and persists
   its own token afterward.

Same idea for historical data (`spike/data_feed.py`): reads a sibling
project's local 1-min CSVs at `SPIKE_EXTERNAL_DATA_DIR` if present and deep
enough, otherwise backfills its own cache under `data_cache/` via
`kite.historical_data()`. Nothing outside `spike`'s own directory is ever
written to.

This means: on this box, `spike` piggybacks on `orb`'s live session and data
with zero duplicated secrets. On a fresh box with no sibling project, point
those two paths at anything that won't exist and fill in the `KITE_*` creds
instead — the code path is identical either way.

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

- `SPIKE_TIMEFRAMES` — which timeframes to scan (default `5min,15min,1h`).
  `1D` is deliberately left out by default: it needs history the local 1-min
  cache doesn't have enough of (LEVEL_LOOKBACK=60 + BASE_MAX=40 daily bars —
  roughly 4-6 months). Add it only once `data_feed`'s backfill has actually
  been run with enough depth for daily.
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
