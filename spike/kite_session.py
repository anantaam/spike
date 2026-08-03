"""Reuse another project's persisted Kite session (read-only) if one is
available and fresh; otherwise log in independently. Never assumes a sibling
project exists -- that's just the fast path on a box that already has one."""
import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pyotp
import requests
from kiteconnect import KiteConnect

from . import config

logger = logging.getLogger(__name__)


def _try_reuse(token_path, api_key) -> KiteConnect | None:
    try:
        with open(token_path) as f:
            data = json.load(f)
        generated_at = datetime.fromisoformat(data["generated_at"])
        age_hours = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600.0
        if age_hours > config.KITE_TOKEN_MAX_AGE_HOURS:
            logger.info("Token at %s is stale (%.1fh) -- skipping", token_path, age_hours)
            return None
        if api_key and data.get("api_key") not in (None, api_key):
            logger.info("Token at %s is for a different api_key -- skipping", token_path)
            return None
        kite = KiteConnect(api_key=data["api_key"], timeout=config.KITE_TIMEOUT_SECONDS)
        kite.set_access_token(data["access_token"])
        kite.profile()  # validate it's actually live before trusting it
        logger.info("Reused Kite session from %s (age %.1fh)", token_path, age_hours)
        return kite
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.info("Could not reuse token at %s (%s)", token_path, exc)
        return None


def _independent_login() -> KiteConnect:
    """Full TOTP-based headless login. Only runs if no reusable token was found
    anywhere and SPIKE has its own KITE_* creds configured."""
    missing = [name for name, val in [
        ("KITE_API_KEY", config.KITE_API_KEY), ("KITE_API_SECRET", config.KITE_API_SECRET),
        ("KITE_USER_ID", config.KITE_USER_ID), ("KITE_PASSWORD", config.KITE_PASSWORD),
        ("KITE_TOTP_SECRET", config.KITE_TOTP_SECRET),
    ] if not val]
    if missing:
        raise RuntimeError(
            f"No reusable Kite token found and these env vars are missing for an "
            f"independent login: {', '.join(missing)}"
        )

    api_key, api_secret = config.KITE_API_KEY, config.KITE_API_SECRET
    user_id, password, totp_secret = config.KITE_USER_ID, config.KITE_PASSWORD, config.KITE_TOTP_SECRET

    pin = pyotp.TOTP(totp_secret).now()
    twofa = f"{int(pin):06d}" if len(pin) <= 5 else pin
    kite = KiteConnect(api_key=api_key, timeout=config.KITE_TIMEOUT_SECONDS)
    s = requests.Session()
    to = config.KITE_TIMEOUT_SECONDS

    r = s.get(kite.login_url(), allow_redirects=False, timeout=to)
    loc = r.headers["location"]
    sess_id = parse_qs(urlparse(loc).query)["sess_id"][0]
    s.get(loc, timeout=to)
    s.get("https://kite.zerodha.com/api/connect/session",
          params={"sess_id": sess_id, "api_key": api_key}, timeout=to)
    r = s.post("https://kite.zerodha.com/api/login",
               data={"user_id": user_id, "password": password, "type": "user_id"}, timeout=to)
    request_id = r.json()["data"]["request_id"]
    s.post("https://kite.zerodha.com/api/twofa",
           data={"user_id": user_id, "request_id": request_id, "twofa_value": twofa,
                 "twofa_type": "totp", "skip_session": "true"}, timeout=to)
    r = s.get("https://kite.zerodha.com/connect/finish",
              params={"api_key": api_key, "sess_id": sess_id}, allow_redirects=False, timeout=to)
    request_token = parse_qs(urlparse(r.headers["location"]).query)["request_token"][0]
    data = kite.generate_session(request_token, api_secret=api_secret)
    kite.set_access_token(data["access_token"])

    payload = {"access_token": data["access_token"], "api_key": api_key,
               "generated_at": datetime.now(timezone.utc).isoformat()}
    tmp = str(config.OWN_TOKEN_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, config.OWN_TOKEN_FILE)
    logger.info("Independent Kite login succeeded; token persisted to %s", config.OWN_TOKEN_FILE)
    return kite


def login_or_reuse() -> KiteConnect:
    """Try the configured shared token first (e.g. ORB's), then spike's own
    previously-persisted token, then fall back to a fresh independent login."""
    for path in (config.KITE_TOKEN_FILE, config.OWN_TOKEN_FILE):
        kite = _try_reuse(path, config.KITE_API_KEY)
        if kite is not None:
            return kite
    return _independent_login()
