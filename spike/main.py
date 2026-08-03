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
INTRADAY_TF_MINUTES = {"5min": 5, "15min": 15, "1h": 60}
GRACE_SECONDS = 25       # wait this long past a candle boundary before scanning it
POLL_SECONDS = 20
SEEN_SIGNALS_FILE = config.STATE_DIR / "seen_signals.json"

# 1D is a calendar-day bar, not a minutes-since-midnight one -- it needs its
# own close-time + catch-up-window logic rather than the generic boundary math.
EOD_GRACE_MINUTES = 10       # wait this long after market close before treating the day's bar as final
EOD_WINDOW_END_HOUR = 20     # don't bother running the daily scan overnight if the service was down

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


def _daily_bar_ready(now_ist: datetime) -> bool:
    """True from (close + grace) until a same-day cutoff -- a window, not a
    single instant, so a brief service restart doesn't skip the day's scan."""
    if now_ist.weekday() >= 5:
        return False
    close_dt = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    window_end = now_ist.replace(hour=EOD_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    return close_dt + timedelta(minutes=EOD_GRACE_MINUTES) <= now_ist <= window_end


def _check_and_alert(ticker: str, tf: str, tdf, seen: set) -> int:
    try:
        signal = detector.latest_signal(tdf, ticker, tf)
    except Exception as exc:
        logger.warning("detector failed for %s/%s (%s)", ticker, tf, exc)
        return 0
    if signal is None:
        return 0
    key = f"{ticker}|{tf}|{signal['retest_ts']}"
    if key in seen:
        return 0
    seen.add(key)
    logger.info("SIGNAL %s", signal)
    notifier.send_alert(signal)
    return 1


def run():
    logger.info("spike starting up")
    kite = kite_session.login_or_reuse()
    universe = data_feed.discover_universe(kite)
    intraday_tfs = [tf for tf in config.TIMEFRAMES if tf in INTRADAY_TF_MINUTES]
    want_daily = "1D" in config.TIMEFRAMES
    logger.info("universe: %d tickers, timeframes: %s", len(universe), config.TIMEFRAMES)

    history = {}
    daily_history = {}
    for i, ticker in enumerate(universe, 1):
        try:
            if intraday_tfs:
                history[ticker] = data_feed.get_1min_history(kite, ticker)
            if want_daily:
                daily_history[ticker] = data_feed.get_daily_history(kite, ticker)
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

        due_intraday = []
        if intraday_tfs and _is_market_open(now_ist):
            for tf in intraday_tfs:
                boundary = _boundary(now_ist, INTRADAY_TF_MINUTES[tf])
                close_time = boundary + timedelta(minutes=INTRADAY_TF_MINUTES[tf])
                ready = now_ist >= close_time + timedelta(seconds=GRACE_SECONDS)
                if ready and last_run.get(tf) != close_time:
                    due_intraday.append((tf, close_time))

        daily_due = (want_daily and _daily_bar_ready(now_ist)
                     and last_run.get("1D") != now_ist.date().isoformat())

        if due_intraday:
            for ticker in list(history.keys()):
                try:
                    history[ticker] = data_feed.refresh_latest(kite, ticker, history[ticker])
                except Exception as exc:
                    logger.warning("refresh_latest failed for %s (%s)", ticker, exc)

            for tf, close_time in due_intraday:
                new_alerts = 0
                for ticker, df1 in history.items():
                    tdf = data_feed.resample(df1, tf)
                    new_alerts += _check_and_alert(ticker, tf, tdf, seen)
                last_run[tf] = close_time
                logger.info("scanned tf=%s at %s, %d new alert(s)", tf, close_time, new_alerts)
            _save_seen(seen)

        if daily_due:
            for ticker in list(daily_history.keys()):
                try:
                    daily_history[ticker] = data_feed.refresh_daily(kite, ticker, daily_history[ticker])
                except Exception as exc:
                    logger.warning("refresh_daily failed for %s (%s)", ticker, exc)

            new_alerts = 0
            for ticker, ddf in daily_history.items():
                new_alerts += _check_and_alert(ticker, "1D", ddf, seen)
            today_key = now_ist.date().isoformat()
            last_run["1D"] = today_key
            logger.info("scanned tf=1D for %s, %d new alert(s)", today_key, new_alerts)
            _save_seen(seen)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
