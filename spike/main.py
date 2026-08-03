"""Live scan loop: refreshes 1-min history for the universe, resamples to
each configured timeframe on its own candle-close boundary, and alerts on any
newly-confirmed retest signal.
"""
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, data_feed, detector, kite_session, notifier

IST = ZoneInfo("Asia/Kolkata")
TF_MINUTES = {"5min": 5, "15min": 15, "1h": 60, "1D": 1440}
GRACE_SECONDS = 25       # wait this long past a candle boundary before scanning it
POLL_SECONDS = 20
SEEN_SIGNALS_FILE = config.STATE_DIR / "seen_signals.json"

logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("spike.main")


def _load_seen() -> set:
    if SEEN_SIGNALS_FILE.exists():
        try:
            return set(json.loads(SEEN_SIGNALS_FILE.read_text()))
        except Exception:
            return set()
    return set()


def _save_seen(seen: set) -> None:
    tmp = str(SEEN_SIGNALS_FILE) + ".tmp"
    Path(tmp).write_text(json.dumps(sorted(seen)[-5000:]))  # cap growth
    Path(tmp).replace(SEEN_SIGNALS_FILE)


def _is_market_open(now_ist: datetime) -> bool:
    if now_ist.weekday() >= 5:
        return False
    t = now_ist.time()
    return (t.hour, t.minute) >= (9, 15) and (t.hour, t.minute) <= (15, 30)


def _boundary(now_ist: datetime, tf_minutes: int) -> datetime:
    total = now_ist.hour * 60 + now_ist.minute
    floored = (total // tf_minutes) * tf_minutes
    return now_ist.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0)


def run():
    logger.info("spike starting up")
    kite = kite_session.login_or_reuse()
    universe = data_feed.discover_universe(kite)
    logger.info("universe: %d tickers, timeframes: %s", len(universe), config.TIMEFRAMES)

    history = {}
    for i, ticker in enumerate(universe, 1):
        try:
            history[ticker] = data_feed.get_1min_history(kite, ticker)
        except Exception as exc:
            logger.warning("Could not load history for %s (%s) -- skipping", ticker, exc)
        if i % 40 == 0:
            logger.info("preloaded %d/%d tickers", i, len(universe))

    seen = _load_seen()
    last_run = {tf: None for tf in config.TIMEFRAMES}

    while True:
        if config.KILL_FILE.exists():
            logger.info("KILL file present -- shutting down")
            return

        now_ist = datetime.now(IST)
        if not _is_market_open(now_ist):
            time.sleep(POLL_SECONDS)
            continue

        due_tfs = []
        for tf in config.TIMEFRAMES:
            boundary = _boundary(now_ist, TF_MINUTES[tf])
            close_time = boundary + timedelta(minutes=TF_MINUTES[tf])  # the candle closes here
            ready = now_ist >= close_time + timedelta(seconds=GRACE_SECONDS)
            if ready and last_run.get(tf) != close_time:
                due_tfs.append((tf, close_time))

        if due_tfs:
            for ticker in list(history.keys()):
                try:
                    history[ticker] = data_feed.refresh_latest(kite, ticker, history[ticker])
                except Exception as exc:
                    logger.warning("refresh_latest failed for %s (%s)", ticker, exc)

            for tf, close_time in due_tfs:
                new_alerts = 0
                for ticker, df1 in history.items():
                    try:
                        tdf = data_feed.resample(df1, tf)
                        signal = detector.latest_signal(tdf, ticker, tf)
                    except Exception as exc:
                        logger.warning("detector failed for %s/%s (%s)", ticker, tf, exc)
                        continue
                    if signal is None:
                        continue
                    key = f"{ticker}|{tf}|{signal['retest_ts']}"
                    if key in seen:
                        continue
                    seen.add(key)
                    new_alerts += 1
                    logger.info("SIGNAL %s", signal)
                    notifier.send_alert(signal)
                last_run[tf] = close_time
                logger.info("scanned tf=%s at %s, %d new alert(s)", tf, close_time, new_alerts)
            _save_seen(seen)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
