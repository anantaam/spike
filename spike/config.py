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

# Local 1-min history: default points at ORB's local cache on this box (read-only).
# Falls back to spike's own CACHE_DIR (backfilled via Kite historical_data) for
# any ticker/timeframe where that isn't present or isn't deep enough.
EXTERNAL_DATA_DIR = _env("SPIKE_EXTERNAL_DATA_DIR", "/home/ubuntu/orb/data")

DISCORD_WEBHOOK_URL = _env("SPIKE_DISCORD_WEBHOOK_URL")

# Which timeframes to scan live. 1D needs its own deeper backfill (see data_feed) --
# leave it out by default until that backfill has actually been run once.
TIMEFRAMES = [tf.strip() for tf in _env("SPIKE_TIMEFRAMES", "5min,15min,1h").split(",") if tf.strip()]

# Universe override: comma-separated tickers. Empty = auto-discover from
# kite.instruments() (current NFO futures underlyings), same as ORB does.
UNIVERSE_OVERRIDE = [t.strip().upper() for t in _env("SPIKE_UNIVERSE").split(",") if t.strip()]

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

# Kite historical_data rate limit: keep comfortably under Kite's ~3 req/sec cap
HISTORICAL_REQUEST_DELAY_SECONDS = float(_env("SPIKE_HIST_DELAY", "0.35"))

# Minimum bars needed before a timeframe is trusted for detection (must clear
# LEVEL_LOOKBACK + BASE_MAX with margin)
MIN_BARS_REQUIRED = LEVEL_LOOKBACK + BASE_MAX + 30
