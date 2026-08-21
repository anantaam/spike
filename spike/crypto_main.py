"""Crypto scan loop -- same two detectors, Binance data, 24/7.

Kept as a separate service from main.py rather than bolted into it. The
equity loop is built around IST session hours, opening-thrust handling and a
daily Kite login; none of that applies here, and interleaving the two would
make both harder to reason about. What IS shared is the part that matters --
detector.py and ob_detector.py are imported unchanged.

Differences that drove the design:
  * No sessions. Nothing to gate on, so every boundary is simply due.
  * No auth. /api/v3/klines is public, so the daily token-expiry failure that
    blinded the equity scanner for three sessions cannot happen here.
  * UTC throughout. Binance timestamps are UTC and a crypto "day" is a UTC
    day, so there is no timezone juggling.
  * No preload or cache. Binance serves 1000 bars per call, more than the
    detectors need, so each scan just refetches.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import binance_feed, config, detector, notifier, ob_detector

TF_MINUTES = {"5min": 5, "15min": 15, "1h": 60, "1D": 1440}
GRACE_SECONDS = 20        # let Binance settle the candle before asking for it
POLL_SECONDS = 15
SEEN_FILE = config.STATE_DIR / "seen_crypto.json"

# Crypto never opens, so there is no opening candle. A sentinel that no real
# timestamp can equal keeps is_opening_thrust permanently false rather than
# having it accidentally match 09:15 UTC.
NO_SESSION_OPEN = (-1, -1)

logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("spike.crypto")


def _load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            return set()
    return set()


def _save_seen(seen: set) -> None:
    tmp = str(SEEN_FILE) + ".tmp"
    Path(tmp).write_text(json.dumps(sorted(seen)[-5000:]))
    Path(tmp).replace(SEEN_FILE)


def _boundary(now: datetime, tf: str) -> datetime:
    """Close of the most recently completed candle, in UTC."""
    if tf == "1D":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    mins = TF_MINUTES[tf]
    total = now.hour * 60 + now.minute
    floored = (total // mins) * mins
    return now.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def _check(symbol: str, tf: str, df, seen: set) -> tuple[int, int]:
    hook = config.CRYPTO_DISCORD_WEBHOOK_URL or None
    spike_n = ob_n = 0

    try:
        sig = detector.latest_signal(df, symbol, tf, session_open_hm=NO_SESSION_OPEN)
    except Exception as exc:
        logger.warning("detector failed for %s/%s (%s)", symbol, tf, exc)
        sig = None
    if sig is not None:
        key = f"{symbol}|{tf}|{sig['retest_ts']}"
        if key not in seen:
            seen.add(key)
            logger.info("SIGNAL %s", sig)
            notifier.send_alert(sig, hook)
            spike_n = 1

    if config.OB_ENABLED and tf in config.CRYPTO_TIMEFRAMES:
        try:
            events = ob_detector.latest_events(df, symbol, tf)
        except Exception as exc:
            logger.warning("ob_detector failed for %s/%s (%s)", symbol, tf, exc)
            events = []
        floor = config.CRYPTO_MIN_RISK_PCT_BY_TF.get(tf, 0.5)
        for ev in events:
            if ev["risk_pct"] < floor:
                continue
            key = f"OB|{symbol}|{tf}|{ev['event']}|{ev['formation_ts']}"
            if key in seen:
                continue
            seen.add(key)
            logger.info("OB %s", ev)
            notifier.send_ob_alert(ev, hook)
            ob_n += 1
    return spike_n, ob_n


def run():
    logger.info("crypto scanner starting up")
    universe = binance_feed.universe()
    tfs = [tf for tf in config.CRYPTO_TIMEFRAMES if tf in TF_MINUTES]
    logger.info("universe: %d pairs %s, timeframes: %s", len(universe), universe, tfs)
    logger.info("gates: %s", config.CRYPTO_MIN_RISK_PCT_BY_TF)
    if not config.CRYPTO_DISCORD_WEBHOOK_URL:
        logger.warning("SPIKE_CRYPTO_DISCORD_WEBHOOK_URL not set -- "
                       "alerts will fall back to the equity webhook")

    seen = _load_seen()
    last_run = {tf: None for tf in tfs}
    last_heartbeat = None

    while True:
        if config.KILL_FILE.exists():
            logger.info("KILL file present -- shutting down")
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        if last_heartbeat is None or (now - last_heartbeat) >= timedelta(minutes=5):
            logger.info("heartbeat: %d pairs, %d seen", len(universe), len(seen))
            last_heartbeat = now

        due = []
        for tf in tfs:
            close_time = _boundary(now, tf)
            if now >= close_time + timedelta(seconds=GRACE_SECONDS) and last_run.get(tf) != close_time:
                due.append((tf, close_time))

        for tf, close_time in due:
            spike_n = ob_n = 0
            for symbol in universe:
                df = binance_feed.fetch_with_retry(symbol, tf)
                if df is None or len(df) < config.OB_MIN_BARS:
                    continue
                s, o = _check(symbol, tf, df, seen)
                spike_n += s
                ob_n += o
            last_run[tf] = close_time
            logger.info("scanned tf=%s at %s UTC, %d signal(s), %d OB alert(s)",
                        tf, close_time, spike_n, ob_n)
            _save_seen(seen)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
