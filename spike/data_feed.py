"""Historical + live candle data for the scan universe.

Fully self-contained: everything is backfilled via kite.historical_data() into
spike's own cache (config.CACHE_DIR) and kept current from there. No other
project's files are ever read.
"""
import logging
import time
from pathlib import Path

import pandas as pd

from . import config

logger = logging.getLogger(__name__)

_instrument_token_cache: dict[str, int] = {}


def _own_1min_path(ticker: str) -> Path:
    return config.CACHE_DIR / f"{ticker}_1min.csv"


def _own_daily_path(ticker: str) -> Path:
    return config.CACHE_DIR / f"{ticker}_daily.csv"


_NIFTY500_FILE = Path(__file__).resolve().parent / "nifty500_constituents.txt"


def _fno_underlyings(kite) -> list[str]:
    """Current F&O stock underlyings, derived live from the NFO instrument
    dump. Index futures (NIFTY, BANKNIFTY, ...) come back too but have no
    underlying equity token, so they get skipped downstream."""
    names = sorted({
        (r.get("name") or "").strip().upper()
        for r in kite.instruments("NFO")
        if r.get("instrument_type") == "FUT"
    })
    if not names:
        raise RuntimeError("kite.instruments('NFO') returned no FUT rows")
    return names


def discover_universe(kite) -> list[str]:
    """The scan universe, shared by both detectors. SPIKE_UNIVERSE wins if set;
    otherwise SPIKE_UNIVERSE_SOURCE picks between the live F&O list (default)
    and the static Nifty 500 file."""
    if config.UNIVERSE_OVERRIDE:
        logger.info("Universe: %d tickers from SPIKE_UNIVERSE override", len(config.UNIVERSE_OVERRIDE))
        return config.UNIVERSE_OVERRIDE

    if config.UNIVERSE_SOURCE == "nifty500":
        if _NIFTY500_FILE.exists():
            names = [ln.strip() for ln in _NIFTY500_FILE.read_text().splitlines() if ln.strip()]
            if names:
                logger.info("Universe: %d Nifty 500 constituents from %s", len(names), _NIFTY500_FILE.name)
                return names
        logger.warning("SPIKE_UNIVERSE_SOURCE=nifty500 but %s is missing/empty -- using F&O",
                       _NIFTY500_FILE.name)

    names = _fno_underlyings(kite)
    logger.info("Universe: %d F&O underlyings from kite.instruments()", len(names))
    return names


def discover_commodity_contracts(kite) -> dict[str, str]:
    """Maps each name in config.COMMODITY_UNIVERSE to its currently active
    (nearest-expiry, unexpired) MCX FUT contract tradingsymbol, and seeds
    the shared instrument-token cache with it -- so get_1min_history /
    refresh_latest etc. work against it completely unchanged, exactly as
    they do for an NSE equity ticker. Call again periodically (daily is
    enough): contracts expire monthly, so the active tradingsymbol for a
    given name changes over time and callers need to notice the rollover."""
    instruments = kite.instruments("MCX")
    today = pd.Timestamp.now().date()
    by_name: dict[str, list] = {}
    for r in instruments:
        if r.get("instrument_type") != "FUT":
            continue
        name = (r.get("name") or "").strip().upper()
        if name not in config.COMMODITY_UNIVERSE:
            continue
        expiry = r.get("expiry")
        if not expiry or expiry < today:
            continue
        by_name.setdefault(name, []).append(r)

    mapping = {}
    for name, rows in by_name.items():
        active = min(rows, key=lambda r: r["expiry"])
        mapping[name] = active["tradingsymbol"]
        _instrument_token_cache[active["tradingsymbol"]] = active["instrument_token"]

    missing = set(config.COMMODITY_UNIVERSE) - mapping.keys()
    if missing:
        logger.warning("No active MCX contract found for: %s", sorted(missing))
    logger.info("MCX contracts: %s", mapping)
    return mapping


def _get_instrument_token(kite, ticker: str) -> int | None:
    if ticker in _instrument_token_cache:
        return _instrument_token_cache[ticker]
    try:
        for row in kite.instruments("NSE"):
            sym = row.get("tradingsymbol")
            if sym:
                _instrument_token_cache[sym] = row["instrument_token"]
    except Exception as exc:
        logger.warning("Could not load NSE instrument tokens (%s)", exc)
        return None
    return _instrument_token_cache.get(ticker)


def _fetch_kite_bars(kite, token: int, from_dt, to_dt, interval: str, chunk_days: int) -> pd.DataFrame:
    """Chunked historical_data pull. Kite caps minute-interval requests to
    ~60 days/call and day-interval to ~2000 days/call; chunk_days should be
    set comfortably under whichever cap applies."""
    frames = []
    cur_to = to_dt
    while cur_to > from_dt:
        cur_from = max(from_dt, cur_to - pd.Timedelta(days=chunk_days))
        try:
            rows = kite.historical_data(token, cur_from, cur_to, interval)
        except Exception as exc:
            logger.warning("historical_data chunk failed (%s -> %s, %s): %s",
                            cur_from, cur_to, interval, exc)
            rows = []
        if rows:
            frames.append(pd.DataFrame(rows))
        time.sleep(config.HISTORICAL_REQUEST_DELAY_SECONDS)
        cur_to = cur_from
    if not frames:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="date").sort_values("date")
    return df


def _load_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["date"], index_col="date")
    return df.sort_index()


def _save_cache(path: Path, df: pd.DataFrame) -> None:
    tmp = str(path) + ".tmp"
    df.reset_index().to_csv(tmp, index=False)
    Path(tmp).replace(path)


def get_1min_history(kite, ticker: str, min_bars_5min: int = config.MIN_BARS_REQUIRED) -> pd.DataFrame:
    """1-min OHLCV, backfilling via Kite (~75 calendar days, chunked in 55-day
    windows) if the local cache is missing or too shallow."""
    path = _own_1min_path(ticker)
    df = _load_cache(path)

    needed_bars_1min = min_bars_5min * 5 + 200  # margin, in 1-min-bar terms
    if df is None or len(df) < needed_bars_1min:
        token = _get_instrument_token(kite, ticker)
        if token is None:
            if df is not None:
                return df
            raise RuntimeError(f"No cached data and no instrument token for {ticker}")
        to_dt = pd.Timestamp.now()
        from_dt = to_dt - pd.Timedelta(days=75)
        logger.info("Backfilling 1-min history for %s via Kite", ticker)
        fresh = _fetch_kite_bars(kite, token, from_dt, to_dt, "minute", chunk_days=55)
        if not fresh.empty:
            fresh["date"] = pd.to_datetime(fresh["date"]).dt.tz_localize(None)
            fresh = fresh.set_index("date")[["open", "high", "low", "close", "volume"]]
            df = fresh if df is None else pd.concat([df, fresh]).sort_index()
            df = df[~df.index.duplicated(keep="last")]
            _save_cache(path, df)

    if df is None:
        raise RuntimeError(f"Could not obtain any 1-min history for {ticker}")
    return df


def refresh_latest(kite, ticker: str, existing: pd.DataFrame, lookback_days: int = 3) -> pd.DataFrame:
    """Pull the last few days of 1-min bars and merge -- cheap per-cycle
    update that keeps `existing` current without re-fetching full history."""
    token = _get_instrument_token(kite, ticker)
    if token is None:
        return existing
    to_dt = pd.Timestamp.now()
    from_dt = to_dt - pd.Timedelta(days=lookback_days)
    try:
        rows = kite.historical_data(token, from_dt, to_dt, "minute")
    except Exception as exc:
        logger.warning("refresh_latest failed for %s (%s)", ticker, exc)
        return existing
    time.sleep(config.HISTORICAL_REQUEST_DELAY_SECONDS)
    if not rows:
        return existing
    fresh = pd.DataFrame(rows)
    fresh["date"] = pd.to_datetime(fresh["date"]).dt.tz_localize(None)
    fresh = fresh.set_index("date")[["open", "high", "low", "close", "volume"]]
    merged = pd.concat([existing, fresh]).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    _save_cache(_own_1min_path(ticker), merged)
    return merged


def get_daily_history(kite, ticker: str, min_bars: int = config.MIN_BARS_REQUIRED) -> pd.DataFrame:
    """Daily OHLCV, backfilling via Kite (~450 calendar days -> comfortably
    130+ trading days even accounting for holidays) if the local cache is
    missing or too shallow. Fetched natively at day interval, not resampled
    from 1-min -- 1-min history isn't kept deep enough for that."""
    path = _own_daily_path(ticker)
    df = _load_cache(path)

    if df is None or len(df) < min_bars + 30:
        token = _get_instrument_token(kite, ticker)
        if token is None:
            if df is not None:
                return df
            raise RuntimeError(f"No cached daily data and no instrument token for {ticker}")
        to_dt = pd.Timestamp.now()
        from_dt = to_dt - pd.Timedelta(days=450)
        logger.info("Backfilling daily history for %s via Kite", ticker)
        fresh = _fetch_kite_bars(kite, token, from_dt, to_dt, "day", chunk_days=400)
        if not fresh.empty:
            fresh["date"] = pd.to_datetime(fresh["date"]).dt.tz_localize(None)
            fresh = fresh.set_index("date")[["open", "high", "low", "close", "volume"]]
            df = fresh if df is None else pd.concat([df, fresh]).sort_index()
            df = df[~df.index.duplicated(keep="last")]
            _save_cache(path, df)

    if df is None:
        raise RuntimeError(f"Could not obtain any daily history for {ticker}")
    return df


def refresh_daily(kite, ticker: str, existing: pd.DataFrame, lookback_days: int = 5) -> pd.DataFrame:
    """Pull the last few days at day interval and merge -- run once after
    market close to pick up the day that just finished."""
    token = _get_instrument_token(kite, ticker)
    if token is None:
        return existing
    to_dt = pd.Timestamp.now()
    from_dt = to_dt - pd.Timedelta(days=lookback_days)
    try:
        rows = kite.historical_data(token, from_dt, to_dt, "day")
    except Exception as exc:
        logger.warning("refresh_daily failed for %s (%s)", ticker, exc)
        return existing
    time.sleep(config.HISTORICAL_REQUEST_DELAY_SECONDS)
    if not rows:
        return existing
    fresh = pd.DataFrame(rows)
    fresh["date"] = pd.to_datetime(fresh["date"]).dt.tz_localize(None)
    fresh = fresh.set_index("date")[["open", "high", "low", "close", "volume"]]
    merged = pd.concat([existing, fresh]).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    _save_cache(_own_daily_path(ticker), merged)
    return merged


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    o = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return o.dropna(subset=["open"])
