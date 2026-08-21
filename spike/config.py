"""All paths, credentials, and detector thresholds -- everything env-driven so
the same code runs unmodified on a different box with a different .env."""
import os
from pathlib import Path

def _env(name: str, default: str = "") -> str:
    """os.getenv, but treats an explicitly-blank var (SPIKE_X=) as unset too --
    an empty .env value shouldn't silently become Path('')."""
    val = os.getenv(name)
    return val if val else default


BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = Path(_env("SPIKE_STATE_DIR") or str(BASE_DIR / "state"))
CACHE_DIR = Path(_env("SPIKE_CACHE_DIR") or str(BASE_DIR / "data_cache"))
STATE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Kite session reuse: default points at ORB's persisted token on this box.
# On a box without a sibling project, point this at nothing (or a path that
# won't exist) and set the KITE_* creds below -- kite_session falls back to
# an independent login.
KITE_TOKEN_FILE = _env("SPIKE_KITE_TOKEN_FILE", "/home/ubuntu/orb/state/live/kite_token.json")
KITE_TOKEN_MAX_AGE_HOURS = float(_env("SPIKE_KITE_TOKEN_MAX_AGE_HOURS", "20"))
OWN_TOKEN_FILE = STATE_DIR / "kite_token.json"

KITE_API_KEY = os.getenv("KITE_API_KEY")
KITE_API_SECRET = os.getenv("KITE_API_SECRET")
KITE_USER_ID = os.getenv("KITE_USER_ID")
KITE_PASSWORD = os.getenv("KITE_PASSWORD")
KITE_TOTP_SECRET = os.getenv("KITE_TOTP_SECRET")

DISCORD_WEBHOOK_URL = _env("SPIKE_DISCORD_WEBHOOK_URL")

# Which timeframes to scan live. Both intraday (resampled from spike's own
# 1-min cache) and 1D (fetched natively at day interval -- see data_feed) are
# backfilled and maintained entirely by spike itself.
TIMEFRAMES = [tf.strip() for tf in _env("SPIKE_TIMEFRAMES", "5min,15min,1h,1D").split(",") if tf.strip()]

# Universe override: comma-separated tickers. Empty = auto-discover.
UNIVERSE_OVERRIDE = [t.strip().upper() for t in _env("SPIKE_UNIVERSE").split(",") if t.strip()]

# Which universe to scan: 'fno' derives the current F&O underlyings live from
# kite.instruments("NFO") (~213 names); 'nifty500' reads the static
# nifty500_constituents.txt (500 names). Applies to BOTH detectors -- they
# share the same preloaded history. F&O is the default: liquid, optionable,
# and small enough to keep alert volume readable.
UNIVERSE_SOURCE = _env("SPIKE_UNIVERSE_SOURCE", "fno").strip().lower()

# Spot indices, appended to the universe. These are Kite NSE tradingsymbols
# (segment INDICES) and resolve through the normal token lookup. Kite reports
# volume=0 on every index bar, so the thrust-retest detector correctly finds
# nothing on them -- but order blocks are purely structural and work fine.
INDEX_UNIVERSE = [s.strip() for s in _env(
    "SPIKE_INDICES", "NIFTY 50,NIFTY BANK,NIFTY FIN SERVICE,NIFTY MID SELECT,NIFTY NEXT 50"
).split(",") if s.strip()]

# MCX commodity underlyings to scan (majors only -- mini/micro/guinea/petal
# variants of the same commodity are deliberately excluded, they just
# duplicate the same price action at a smaller lot size). Intraday only
# (5min/15min/1h): a front-month contract rarely accumulates the ~130
# trading days of daily history the detector needs for 1D before it rolls
# to the next month, so 1D is not wired up for commodities.
COMMODITY_UNIVERSE = [c.strip().upper() for c in _env(
    "SPIKE_COMMODITIES", "GOLD,SILVER,CRUDEOIL,NATURALGAS,COPPER,ZINC,ALUMINIUM,LEAD,NICKEL"
).split(",") if c.strip()]

# MCX session: 9:00 to 23:30 IST, extended to 23:55 during US daylight saving
# (~mid-Mar to early-Nov) since MCX aligns its close with COMEX/NYMEX hours.
# Approximated by calendar month rather than the exact DST transition date --
# off by at most ~1-2 weeks at the March/November edges.
MCX_OPEN_HM = (9, 0)
MCX_DST_MONTHS = {3, 4, 5, 6, 7, 8, 9, 10, 11}

KILL_FILE = BASE_DIR / "KILL"  # touch this file to stop the service gracefully

# --- detector thresholds (settled values from the historical validation pass) ---
STRONG_K = 1.2
MAX_RUN = 5
LARGE_K = 2.5
VOL_SPIKE_K = 2.5
MIN_EXTENSION = 0.5
MAX_WAIT = 60
DECLINE_BASELINE_THRESH = 0.85
BASE_MIN, BASE_MAX, BASE_CAP = 5, 40, 4.5
LEVEL_LOOKBACK = 60
LEVEL_TOL_ATR = 1.5

# A run-bridging exception: a single candle that merely pauses inside an
# otherwise-strong thrust (small body, contained range, doesn't undercut the
# prior bar's low, still green) doesn't split the run. Validated in isolation
# against full history: +3.3% more candidates, reach-100% unchanged/slightly
# better on every timeframe -- a clean addition, not a quality tradeoff.
PAUSE_BODY_K = 1.0      # pause candle's body must be < this many ATRs
PAUSE_RANGE_K = 3.0     # pause candle's total high-low range must be < this many ATRs

# --- order block detector (ob_detector.py) -- independent of the above ---
OB_ENABLED = _env("SPIKE_OB_ENABLED", "1") not in ("0", "false", "False")
OB_TIMEFRAMES = [tf.strip() for tf in _env("SPIKE_OB_TIMEFRAMES", "5min,15min,1h,1D").split(",") if tf.strip()]
OB_SWING_LEN = int(_env("SPIKE_OB_SWING_LEN", "10"))
OB_USE_BODY = _env("SPIKE_OB_USE_BODY", "1") not in ("0", "false", "False")
OB_ALERT_FORMATION = _env("SPIKE_OB_ALERT_FORMATION", "1") not in ("0", "false", "False")
OB_ALERT_RETOUCH = _env("SPIKE_OB_ALERT_RETOUCH", "1") not in ("0", "false", "False")

# Zone must be at least this thick (entry-to-stop as % of entry) to alert.
# Round-trip cost is ~0.25%, and the median raw OB zone is ~0.21% -- i.e. most
# of them cost more to trade than the whole risk unit is worth. Measured across
# 838k historical blocks, outcomes improve monotonically with zone thickness.
# This is also the main volume control: unfiltered, OB retouches fire ~1000x a
# day across 500 names, which is unreadable.
def _per_tf(raw: str, default: float) -> dict:
    """Accepts either one number for every timeframe, or 'tf=value' pairs."""
    if "=" not in raw:
        return {tf: float(raw or default) for tf in ("5min", "15min", "1h", "1D")}
    out = {}
    for part in raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = float(v)
    return out


# Minimum entry-to-stop distance, as % of entry, for an OB alert to be sent.
# Per timeframe, because the stop is k*ATR and ATR scales with timeframe -- a
# flat gate silently switches the fast timeframes off rather than filtering
# them (at a flat 1.5% the 5min and 15min streams drop to ~0.1 and ~0.6 alerts
# a day, while 1D is untouched).
# Floor rationale: round-trip cost is ~0.25%, so a 0.5% stop already spends
# half an R on costs; below that the trade isn't worth taking regardless of
# the setup. Roughly 38 alerts/day across the F&O universe at these values.
OB_MIN_RISK_PCT_BY_TF = _per_tf(
    _env("SPIKE_OB_MIN_RISK_PCT", "5min=1.0,15min=1.0,1h=0.5,1D=0.5"), 0.5)

# How far into the block price must trade before it counts as a retest.
# 1.0 = the distal edge (must cross the whole block -- the level LuxAlgo
# actually draws on the chart), 0.5 = the ICT mean threshold, 0.0 = any graze
# of the near edge. At 0.0 roughly a quarter of "retests" penetrated under 25%
# of the zone and ~8% under 5%, which is not a retest in any useful sense.
OB_RETOUCH_DEPTH = float(_env("SPIKE_OB_RETOUCH_DEPTH", "1.0"))

# Stop distance in ATRs (measured at the entry bar) beyond the entry level.
# Stops drawn from the OB candle's own geometry are unusable -- see the note
# in ob_detector.entry_stop.
OB_STOP_ATR_MULT = float(_env("SPIKE_OB_STOP_ATR_MULT", "1.0"))
OB_MIN_BARS = int(_env("SPIKE_OB_MIN_BARS", "120"))

# --- crypto scanner (binance_feed.py + crypto_main.py) ---------------------
# Public Binance data only -- no API key is used or needed, so there is no
# credential here to leak and no daily token to expire.
# Fixed list of the durable retail majors. Ranked by TRADE COUNT rather than
# dollar volume: volume alone puts stablecoin pairs (USD1, RLUSD) near the top,
# and those barely move, so a breakout detector on them is meaningless.
CRYPTO_ENABLED = _env("SPIKE_CRYPTO_ENABLED", "1") not in ("0", "false", "False")
CRYPTO_UNIVERSE = [s.strip().upper() for s in _env(
    "SPIKE_CRYPTO_UNIVERSE",
    "BTCUSDT,ETHUSDT,XRPUSDT,SOLUSDT,BNBUSDT,DOGEUSDT,ADAUSDT,LINKUSDT,SUIUSDT,PEPEUSDT"
).split(",") if s.strip()]
CRYPTO_TIMEFRAMES = [t.strip() for t in _env(
    "SPIKE_CRYPTO_TIMEFRAMES", "5min,15min,1h,1D").split(",") if t.strip()]
CRYPTO_DISCORD_WEBHOOK_URL = _env("SPIKE_CRYPTO_DISCORD_WEBHOOK_URL")
# Gates are calibrated separately from equities: crypto ATR as a % of price is
# far larger, so the F&O values would let essentially everything through.
CRYPTO_MIN_RISK_PCT_BY_TF = _per_tf(_env("SPIKE_CRYPTO_MIN_RISK_PCT", "0.5"), 0.5)

# Kite historical_data rate limit: keep comfortably under Kite's ~3 req/sec cap
HISTORICAL_REQUEST_DELAY_SECONDS = float(_env("SPIKE_HIST_DELAY", "0.35"))

# Network timeout for every Kite API call. kiteconnect/requests default to no
# timeout at all -- a single stalled connection (seen in practice: a
# CLOSE-WAIT socket that never got read) then blocks this single-threaded
# loop forever, silently, with no exception and no further log lines.
KITE_TIMEOUT_SECONDS = float(_env("SPIKE_KITE_TIMEOUT", "20"))

# Minimum bars needed before a timeframe is trusted for detection (must clear
# LEVEL_LOOKBACK + BASE_MAX with margin)
MIN_BARS_REQUIRED = LEVEL_LOOKBACK + BASE_MAX + 30
