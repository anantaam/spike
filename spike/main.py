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

import pandas as pd

from . import config, data_feed, detector, kite_session, notifier, ob_detector

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


def _mcx_close_hm(now_ist: datetime) -> tuple[int, int]:
    return (23, 55) if now_ist.month in config.MCX_DST_MONTHS else (23, 30)


def _is_mcx_open(now_ist: datetime) -> bool:
    if now_ist.weekday() >= 5:
        return False
    t = now_ist.time()
    return (t.hour, t.minute) >= config.MCX_OPEN_HM and (t.hour, t.minute) <= _mcx_close_hm(now_ist)


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


def _completed(tdf, close_time: datetime):
    """Drop the bar that is still forming.

    resample() labels a bar with the START of its period, so at 13:33 a 15min
    frame ends with a bar labelled 13:30 holding three minutes of data. Both
    detectors trigger on the last bar, so without this they fire on a partial
    candle and the signal can evaporate as the bar fills in. close_time is the
    close of the most recently completed candle, so anything labelled at or
    after it is still open.
    """
    if tdf.empty:
        return tdf
    cutoff = pd.Timestamp(close_time).tz_localize(None)
    return tdf[tdf.index < cutoff]


def _check_and_alert(ticker: str, tf: str, tdf, seen: set,
                      session_open_hm: tuple[int, int] = (9, 15)) -> int:
    try:
        signal = detector.latest_signal(tdf, ticker, tf, session_open_hm=session_open_hm)
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
    if signal.get("is_opening_thrust"):
        logger.info("suppressed opening-thrust alert for %s/%s", ticker, tf)
        return 0
    notifier.send_alert(signal)
    return 1


def _check_ob(ticker: str, tf: str, tdf, seen: set) -> int:
    """Order blocks, scanned independently of the thrust-retest pattern."""
    if not config.OB_ENABLED or tf not in config.OB_TIMEFRAMES:
        return 0
    try:
        events = ob_detector.latest_events(tdf, ticker, tf)
    except Exception as exc:
        logger.warning("ob_detector failed for %s/%s (%s)", ticker, tf, exc)
        return 0
    sent = 0
    for ev in events:
        key = f"OB|{ticker}|{tf}|{ev['event']}|{ev['formation_ts']}"
        if key in seen:
            continue
        seen.add(key)
        logger.info("OB %s", ev)
        notifier.send_ob_alert(ev)
        sent += 1
    return sent


def run():
    logger.info("spike starting up")
    kite = kite_session.login_or_reuse()
    universe = data_feed.discover_universe(kite)
    commodity_map = data_feed.discover_commodity_contracts(kite)  # {name: tradingsymbol}
    intraday_tfs = [tf for tf in config.TIMEFRAMES if tf in INTRADAY_TF_MINUTES]
    want_daily = "1D" in config.TIMEFRAMES
    logger.info("universe: %d equity tickers, %d commodity contracts, timeframes: %s",
                len(universe), len(commodity_map), config.TIMEFRAMES)

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

    commodity_history = {}
    for ticker in commodity_map.values():
        try:
            commodity_history[ticker] = data_feed.get_1min_history(kite, ticker)
        except Exception as exc:
            logger.warning("Could not load history for %s (%s) -- skipping", ticker, exc)
    logger.info("preloaded %d/%d commodity contracts", len(commodity_history), len(commodity_map))

    last_commodity_discovery = datetime.now(IST).date()
    seen = _load_seen()
    last_run = {tf: None for tf in config.TIMEFRAMES}
    last_heartbeat = None

    while True:
        if config.KILL_FILE.exists():
            logger.info("KILL file present -- shutting down")
            return

        now_ist = datetime.now(IST)

        if now_ist.date() != last_commodity_discovery:
            last_commodity_discovery = now_ist.date()
            try:
                new_map = data_feed.discover_commodity_contracts(kite)
            except Exception as exc:
                logger.warning("commodity contract re-discovery failed (%s)", exc)
                new_map = commodity_map
            for name, sym in new_map.items():
                if commodity_map.get(name) != sym:
                    old_sym = commodity_map.get(name)
                    logger.info("MCX contract roll for %s: %s -> %s", name, old_sym, sym)
                    commodity_history.pop(old_sym, None)
                    try:
                        commodity_history[sym] = data_feed.get_1min_history(kite, sym)
                    except Exception as exc:
                        logger.warning("Could not load history for rolled contract %s (%s)", sym, exc)
            commodity_map = new_map

        equity_open = _is_market_open(now_ist)
        mcx_open = _is_mcx_open(now_ist)

        # A silent log is ambiguous between "nothing due" and "stuck" --
        # print a heartbeat regardless, so a stall shows up as missing
        # heartbeats rather than as indistinguishable silence.
        if last_heartbeat is None or (now_ist - last_heartbeat) >= timedelta(minutes=5):
            logger.info("heartbeat: equity_open=%s mcx_open=%s tickers=%d commodities=%d",
                        equity_open, mcx_open, len(history), len(commodity_history))
            last_heartbeat = now_ist

        due_intraday = []
        if intraday_tfs and (equity_open or mcx_open):
            for tf in intraday_tfs:
                # _boundary floors `now` to the tf grid -- that floor value IS
                # the close time of the most recently completed candle. (A
                # previous version added tf_minutes here, which instead gives
                # the close of the candle still in progress -- a time that's
                # always in the future, so `ready` could never fire.)
                close_time = _boundary(now_ist, INTRADAY_TF_MINUTES[tf])
                ready = now_ist >= close_time + timedelta(seconds=GRACE_SECONDS)
                if ready and last_run.get(tf) != close_time:
                    due_intraday.append((tf, close_time))

        daily_due = (want_daily and _daily_bar_ready(now_ist)
                     and last_run.get("1D") != now_ist.date().isoformat())

        if due_intraday:
            if equity_open:
                for ticker in list(history.keys()):
                    try:
                        history[ticker] = data_feed.refresh_latest(kite, ticker, history[ticker])
                    except Exception as exc:
                        logger.warning("refresh_latest failed for %s (%s)", ticker, exc)
            if mcx_open:
                for ticker in list(commodity_history.keys()):
                    try:
                        commodity_history[ticker] = data_feed.refresh_latest(kite, ticker, commodity_history[ticker])
                    except Exception as exc:
                        logger.warning("refresh_latest failed for %s (%s)", ticker, exc)

            for tf, close_time in due_intraday:
                new_alerts = ob_alerts = 0
                if equity_open:
                    for ticker, df1 in history.items():
                        tdf = _completed(data_feed.resample(df1, tf), close_time)
                        new_alerts += _check_and_alert(ticker, tf, tdf, seen)
                        ob_alerts += _check_ob(ticker, tf, tdf, seen)
                if mcx_open:
                    for ticker, df1 in commodity_history.items():
                        tdf = _completed(data_feed.resample(df1, tf), close_time)
                        new_alerts += _check_and_alert(ticker, tf, tdf, seen, session_open_hm=config.MCX_OPEN_HM)
                        ob_alerts += _check_ob(ticker, tf, tdf, seen)
                last_run[tf] = close_time
                logger.info("scanned tf=%s at %s, %d new alert(s), %d OB alert(s)",
                            tf, close_time, new_alerts, ob_alerts)
            _save_seen(seen)

        if daily_due:
            for ticker in list(daily_history.keys()):
                try:
                    daily_history[ticker] = data_feed.refresh_daily(kite, ticker, daily_history[ticker])
                except Exception as exc:
                    logger.warning("refresh_daily failed for %s (%s)", ticker, exc)

            new_alerts = ob_alerts = 0
            for ticker, ddf in daily_history.items():
                new_alerts += _check_and_alert(ticker, "1D", ddf, seen)
                ob_alerts += _check_ob(ticker, "1D", ddf, seen)
            today_key = now_ist.date().isoformat()
            last_run["1D"] = today_key
            logger.info("scanned tf=1D for %s, %d new alert(s), %d OB alert(s)",
                        today_key, new_alerts, ob_alerts)
            _save_seen(seen)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
