"""Binance public market data for the crypto scanner.

Deliberately unauthenticated: /api/v3/klines is a public endpoint, so the
scanner needs no API key, no signature, and holds no credential that could be
abused. It also means none of the daily token-expiry failure mode that took
the equity scanner blind for three sessions applies here.

No local cache either. Binance returns up to 1000 bars per call, comfortably
more than the detectors need, so every scan just refetches. That removes the
backfill/merge machinery data_feed.py needs for Kite.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

from . import config

logger = logging.getLogger(__name__)

BASE = "https://api.binance.com"
# Binance interval codes differ from the pandas rules used elsewhere.
# 4h/12h are not scan timeframes -- they exist so WAVE_CONTEXT_TF can be
# pointed at a different wave degree without a code change.
INTERVAL = {"5min": "5m", "15min": "15m", "1h": "1h",
            "4h": "4h", "12h": "12h", "1D": "1d"}
BARS = 1000            # max per call; weight 2, against a 6000/min budget
TIMEOUT = 20


def _get(path: str, params: dict):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "spike-crypto/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


def fetch_ohlcv(symbol: str, tf: str, limit: int = BARS) -> pd.DataFrame:
    """OHLCV frame indexed by UTC open time, matching what the detectors expect.

    The final kline Binance returns is the candle still forming, so it is
    dropped here -- the scanner alerts on confirmed closes only.
    """
    rows = _get("/api/v3/klines", {"symbol": symbol, "interval": INTERVAL[tf],
                                    "limit": limit})
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore"])
    df = df[["open_time", "open", "high", "low", "close", "volume"]].astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float})
    df["open_time"] = pd.to_datetime(df.open_time, unit="ms")   # UTC, tz-naive
    df = df.set_index("open_time")
    return df.iloc[:-1]     # drop the in-progress candle


def fetch_with_retry(symbol: str, tf: str, attempts: int = 3) -> pd.DataFrame | None:
    """Binance answers 429 with a Retry-After when the weight budget is spent;
    honour it rather than hammering, since ignoring it earns an IP ban."""
    for n in range(attempts):
        try:
            return fetch_ohlcv(symbol, tf)
        except urllib.error.HTTPError as exc:
            wait = int(exc.headers.get("Retry-After", 2 ** n)) if exc.code == 429 else 2 ** n
            logger.warning("%s/%s HTTP %s -- retry in %ss", symbol, tf, exc.code, wait)
            time.sleep(wait)
        except Exception as exc:
            logger.warning("%s/%s fetch failed (%s)", symbol, tf, exc)
            time.sleep(2 ** n)
    return None


def universe() -> list[str]:
    return list(config.CRYPTO_UNIVERSE)
