"""Discord webhook alerts -- same convention as the other projects on this box."""
import json
import logging
import urllib.request

from . import config

logger = logging.getLogger(__name__)


def _level_note(bars: int) -> str:
    """How significant the level just cleared was. Both detectors only require
    beating a short local extreme -- a 10-bar swing for order blocks, 60 bars
    for the thrust detector -- so the bar count is what separates "cleared a
    minor swing" from "cleared a level that had stood for months"."""
    if bars <= 0:
        return "no prior level cleared"
    if bars >= 500:
        return "broke a 500+ bar high/low"
    return f"broke a {bars}-bar high/low"


def _post(msg: str, webhook: str | None = None) -> None:
    webhook = webhook or config.DISCORD_WEBHOOK_URL
    if not webhook:
        logger.warning("SPIKE_DISCORD_WEBHOOK_URL not set -- not sent: %s", msg)
        return
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps({"content": msg[:1900]}).encode(),
            # Discord/Cloudflare 403s the default urllib User-Agent
            headers={"Content-Type": "application/json", "User-Agent": "spike-scanner/1.0"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        logger.warning("Discord post failed: %s", exc)


def send_ob_alert(ev: dict, webhook: str | None = None) -> None:
    """Order block formation/retouch. Formation is the cue to check the wave
    count; retouch is the actual entry trigger -- so they're visually distinct."""
    arrow = "▲" if ev["direction"] == "bullish" else "▼"
    if ev["event"] == "formation":
        head = f"📦 **{ev['ticker']}** [{ev['tf']}] {arrow} OB FORMED — check wave count"
        when = f"swing {ev['swing_ts']} -> broke {ev['formation_ts']}"
    else:
        head = f"🎯 **{ev['ticker']}** [{ev['tf']}] {arrow} OB RETOUCH — entry trigger"
        when = f"formed {ev['formation_ts']}, retouched {ev['retouch_ts']}"
    vol = f", vol_spike {ev['vol_spike']}x" if ev.get("vol_spike") is not None else ""
    _post(
        f"{head}\n"
        f"zone: {ev['zone_lo']}-{ev['zone_hi']}  entry: {ev['entry']}  stop: {ev['stop']}  "
        f"risk: {ev['risk_pct']}%\n"
        f"{_level_note(ev.get('break_lookback', 0))}, move {ev['move_pct']}%\n"
        f"{when}{vol}",
        webhook,
    )


def send_alert(signal: dict, webhook: str | None = None) -> None:
    arrow = "▲" if signal["direction"] == "bullish" else "▼"
    # always state opening vs mid-session explicitly -- reach-100% 45.5% vs 57.7%,
    # and the opening candle is the exact spot Kite/TradingView data most often disagree
    tags = " 🌅OPENING" if signal.get("is_opening_thrust") else " 🕒MID SESSION"
    if signal.get("high_probability"):
        tags += " ⭐HIGH PROBABILITY"
    msg = (
        f"{arrow} **{signal['ticker']}** [{signal['tf']}] {signal['direction'].upper()} retest{tags}\n"
        f"zone: {signal['zone_lo']}-{signal['zone_hi']}  entry: {signal['entry']}  "
        f"stop: {signal['stop']}  target: {signal['target']}\n"
        f"{_level_note(signal.get('break_lookback', 0))}\n"
        f"thrust {signal['thrust_start']} -> {signal['thrust_end']}, "
        f"retest {signal['retest_ts']}, vol_spike {signal['vol_spike']}x, "
        f"thrust/zone {signal['thrust_to_zone_ratio']}x"
    )
    _post(msg, webhook)
