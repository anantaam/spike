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

# Universe override: comma-separated tickers. Empty = auto-discover from
# kite.instruments() (current NFO futures underlyings), same as ORB does.
UNIVERSE_OVERRIDE = [t.strip().upper() for t in _env("SPIKE_UNIVERSE").split(",") if t.strip()]

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
