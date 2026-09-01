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

from . import (config, data_feed, detector, kite_session, notifier,
               ob_detector, wave, wavechart)

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


# Minutes past the hour that each segment's session starts on. Only matters
# for the 1h timeframe: NSE opens 09:15 so its hourly candles run 09:15-10:15,
# while MCX opens 09:00 and is already on the clock grid. 5min/15min divide
# evenly into 09:15 either way.
EQUITY_HOUR_OFFSET = 15
MCX_HOUR_OFFSET = 0


def _tf_offset(tf: str, hour_offset: int) -> int:
    return hour_offset if tf == "1h" else 0


def _boundary(now_ist: datetime, tf_minutes: int, offset_min: int = 0) -> datetime:
    """Close of the most recently completed candle on this timeframe's grid.

    offset_min shifts the grid off the clock so it lines up with the session
    open -- without it, 1h boundaries land on the hour while NSE's candles
    close at quarter past, and the scanner evaluates a candle that is still
    45 minutes from closing.
    """
    total = now_ist.hour * 60 + now_ist.minute - offset_min
    floored = (total // tf_minutes) * tf_minutes + offset_min
    day_shift, mins = divmod(floored, 24 * 60)
    base = now_ist.replace(hour=mins // 60, minute=mins % 60, second=0, microsecond=0)
    return base + timedelta(days=day_shift)


def _due(now_ist: datetime, tfs: list[str], last_run: dict,
         hour_offset: int, segment: str) -> list[tuple[str, datetime]]:
    """Timeframes whose candle has closed and hasn't been scanned yet.

    Computed per segment because equity and MCX sit on different 1h grids, so
    one shared due-list would scan one of them against a half-formed candle.
    """
    out = []
    for tf in tfs:
        close_time = _boundary(now_ist, INTRADAY_TF_MINUTES[tf], _tf_offset(tf, hour_offset))
        if now_ist >= close_time + timedelta(seconds=GRACE_SECONDS) \
                and last_run.get((segment, tf)) != close_time:
            out.append((tf, close_time))
    return out


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


def _mcx_context(df1):
    """Wave-context bars for a commodity, resampled from its 1-min cache.

    Not daily: MCX contracts roll monthly, so a front-month contract has only
    ~90-120 daily bars and many printed no volume before it became liquid --
    a 100-140 daily count would be fitted to untraded bars. See the
    WAVE_CONTEXT_TF note in config.py.
    """
    tf = config.WAVE_CONTEXT_TF.get("commodity", "4h")
    try:
        return data_feed.resample(df1, tf, _tf_offset(tf, MCX_HOUR_OFFSET))
    except Exception:
        return None


def _tag_wave(ev: dict, cbars) -> str | None:
    """Attach wave context to an event in place; return a chart path if any.

    Tagging is strictly additive -- every exception here is swallowed, because
    an alert that fires without wave tags is far better than one that never
    fires because a pivot search blew up on a thin symbol.
    """
    if not config.WAVE_ENABLED or cbars is None:
        return None
    try:
        ts = pd.Timestamp(ev.get("retouch_ts") or ev["formation_ts"])
        wc = wave.context(cbars, ts, ev["direction"], key=ev["ticker"])
        ev["grade"] = wave.grade(wc)
        if wc is None:
            return None
        ev["wave_context"] = {k: wc[k] for k in
                              ("wave", "leg_dir", "where", "agrees", "score", "span")}
        return wavechart.render(ev, wc, cbars)
    except Exception as exc:
        logger.warning("wave context failed for %s/%s (%s)",
                       ev.get("ticker"), ev.get("tf"), exc)
        return None


def _check_ob(ticker: str, tf: str, tdf, seen: set, cbars=None) -> int:
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
        chart = _tag_wave(ev, cbars)
        logger.info("OB %s", ev)
        notifier.send_ob_alert(ev, chart=chart)
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
    last_run: dict = {}   # (segment, tf) -> close_time of the last scan
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

        due_eq = _due(now_ist, intraday_tfs, last_run, EQUITY_HOUR_OFFSET, "eq") \
            if (intraday_tfs and equity_open) else []
        due_mcx = _due(now_ist, intraday_tfs, last_run, MCX_HOUR_OFFSET, "mcx") \
            if (intraday_tfs and mcx_open) else []

        daily_due = (want_daily and _daily_bar_ready(now_ist)
                     and last_run.get(("eq", "1D")) != now_ist.date().isoformat())

        if due_eq:
            for ticker in list(history.keys()):
                try:
                    history[ticker] = data_feed.refresh_latest(kite, ticker, history[ticker])
                except Exception as exc:
                    logger.warning("refresh_latest failed for %s (%s)", ticker, exc)
            for tf, close_time in due_eq:
                off = _tf_offset(tf, EQUITY_HOUR_OFFSET)
                new_alerts = ob_alerts = 0
                for ticker, df1 in history.items():
                    tdf = _completed(data_feed.resample(df1, tf, off), close_time)
                    new_alerts += _check_and_alert(ticker, tf, tdf, seen)
                    ob_alerts += _check_ob(ticker, tf, tdf, seen,
                                           daily_history.get(ticker))
                last_run[("eq", tf)] = close_time
                logger.info("scanned equity tf=%s at %s, %d new alert(s), %d OB alert(s)",
                            tf, close_time, new_alerts, ob_alerts)
            _save_seen(seen)

        if due_mcx:
            for ticker in list(commodity_history.keys()):
                try:
                    commodity_history[ticker] = data_feed.refresh_latest(kite, ticker, commodity_history[ticker])
                except Exception as exc:
                    logger.warning("refresh_latest failed for %s (%s)", ticker, exc)
            for tf, close_time in due_mcx:
                off = _tf_offset(tf, MCX_HOUR_OFFSET)
                new_alerts = ob_alerts = 0
                for ticker, df1 in commodity_history.items():
                    tdf = _completed(data_feed.resample(df1, tf, off), close_time)
                    new_alerts += _check_and_alert(ticker, tf, tdf, seen,
                                                   session_open_hm=config.MCX_OPEN_HM)
                    ob_alerts += _check_ob(ticker, tf, tdf, seen,
                                           _mcx_context(df1))
                last_run[("mcx", tf)] = close_time
                logger.info("scanned mcx tf=%s at %s, %d new alert(s), %d OB alert(s)",
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
                ob_alerts += _check_ob(ticker, "1D", ddf, seen, ddf)
            today_key = now_ist.date().isoformat()
            last_run[("eq", "1D")] = today_key
            logger.info("scanned tf=1D for %s, %d new alert(s), %d OB alert(s)",
                        today_key, new_alerts, ob_alerts)
            _save_seen(seen)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
