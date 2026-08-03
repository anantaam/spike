"""Historical + live candle data for the scan universe.

Source priority per ticker:
  1. A sibling project's local 1-min CSV (config.EXTERNAL_DATA_DIR), read-only --
     the fast path on a box that already has one (e.g. ORB on this server).
  2. spike's own cache (config.CACHE_DIR), backfilled via kite.historical_data()
     when the external source is missing or too shallow for the configured
     timeframes. This is what makes spike self-sufficient on a fresh box.

Either way, updates are only ever written to spike's own cache -- the
external directory is never modified.
"""
import glob
import logging
import time
from pathlib import Path

import pandas as pd

from . import config

logger = logging.getLogger(__name__)

_instrument_token_cache: dict[str, int] = {}


def _external_csv_path(ticker: str) -> Path:
    return Path(config.EXTERNAL_DATA_DIR) / f"{ticker}_1min_kite.csv"


def _own_csv_path(ticker: str) -> Path:
    return config.CACHE_DIR / f"{ticker}_1min.csv"


def discover_universe(kite) -> list[str]:
    """F&O-underlying equity symbols. Override via SPIKE_UNIVERSE, else derive
    from a fresh kite.instruments() call (same approach as ORB's downloader),
    else fall back to whatever tickers already have a local CSV."""
    if config.UNIVERSE_OVERRIDE:
        return config.UNIVERSE_OVERRIDE

    try:
        instruments = kite.instruments("NFO")
        names = sorted({
            (r.get("name") or "").strip().upper()
            for r in instruments
            if r.get("instrument_type") == "FUT"
        })
        if names:
            logger.info("Universe: %d F&O underlyings from kite.instruments()", len(names))
            return names
    except Exception as exc:
        logger.warning("Could not derive universe from kite.instruments() (%s)", exc)

    external = sorted(Path(config.EXTERNAL_DATA_DIR).glob("*_1min_kite.csv"))
    if external:
        names = [p.name.replace("_1min_kite.csv", "") for p in external]
        logger.info("Universe: %d tickers from external data dir (fallback)", len(names))
        return names

    raise RuntimeError("Could not determine a ticker universe from any source")


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


def _fetch_kite_minute_bars(kite, token: int, from_dt, to_dt) -> pd.DataFrame:
    """Chunked historical_data pull -- Kite caps minute-interval requests to
    ~60 days per call, so walk backward in 55-day windows."""
    frames = []
    cur_to = to_dt
    while cur_to > from_dt:
        cur_from = max(from_dt, cur_to - pd.Timedelta(days=55))
        try:
            rows = kite.historical_data(token, cur_from, cur_to, "minute")
        except Exception as exc:
            logger.warning("historical_data chunk failed (%s -> %s): %s", cur_from, cur_to, exc)
            rows = []
        if rows:
            frames.append(pd.DataFrame(rows))
        time.sleep(config.HISTORICAL_REQUEST_DELAY_SECONDS)
        cur_to = cur_from
    if not frames:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="date").sort_values("date")
    return df


def _load_external(ticker: str) -> pd.DataFrame | None:
    path = _external_csv_path(ticker)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["date"])
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        return df.set_index("date").sort_index()
    except Exception as exc:
        logger.warning("Could not read external CSV for %s (%s)", ticker, exc)
        return None


def _load_own_cache(ticker: str) -> pd.DataFrame | None:
    path = _own_csv_path(ticker)
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["date"], index_col="date")
    return df.sort_index()


def _save_own_cache(ticker: str, df: pd.DataFrame) -> None:
    tmp = str(_own_csv_path(ticker)) + ".tmp"
    df.reset_index().to_csv(tmp, index=False)
    Path(tmp).replace(_own_csv_path(ticker))


def get_1min_history(kite, ticker: str, min_bars_5min: int = config.MIN_BARS_REQUIRED) -> pd.DataFrame:
    """Return a 1-min OHLCV frame with enough depth for the configured
    timeframes, backfilling via Kite if neither local source is deep enough."""
    df = _load_external(ticker)
    source = "external"
    if df is None or len(df) < min_bars_5min * 5:  # 1min bars needed for 5min lookback
        cached = _load_own_cache(ticker)
        if cached is not None and (df is None or len(cached) > len(df)):
            df, source = cached, "own_cache"

    needed_bars_1min = min_bars_5min * 5 + 200  # margin
    if df is None or len(df) < needed_bars_1min:
        token = _get_instrument_token(kite, ticker)
        if token is None:
            if df is not None:
                return df
            raise RuntimeError(f"No local data and no instrument token for {ticker}")
        to_dt = pd.Timestamp.now()
        from_dt = to_dt - pd.Timedelta(days=75)
        logger.info("Backfilling %s via Kite historical_data (source was %s)", ticker, source)
        fresh = _fetch_kite_minute_bars(kite, token, from_dt, to_dt)
        if not fresh.empty:
            fresh["date"] = pd.to_datetime(fresh["date"]).dt.tz_localize(None)
            fresh = fresh.set_index("date")[["open", "high", "low", "close", "volume"]]
            df = fresh if df is None else pd.concat([df, fresh]).sort_index()
            df = df[~df.index.duplicated(keep="last")]
            _save_own_cache(ticker, df)

    if df is None:
        raise RuntimeError(f"Could not obtain any history for {ticker}")
    return df


def refresh_latest(kite, ticker: str, existing: pd.DataFrame, lookback_days: int = 3) -> pd.DataFrame:
    """Pull the last few days fresh and merge -- cheap per-cycle update that
    keeps `existing` current without re-fetching full history each time."""
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
    _save_own_cache(ticker, merged)
    return merged


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    o = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return o.dropna(subset=["open"])
