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


def _post(msg: str, webhook: str | None = None, chart: str | None = None) -> bool:
    """True only if Discord actually accepted the message.

    Returns a result rather than swallowing everything, so a caller that needs
    to report "sent" can say so truthfully. The scan loops still ignore it --
    a failed post must not abort a scan -- but anything that prints a delivery
    count has no excuse for guessing.
    """
    webhook = webhook or config.DISCORD_WEBHOOK_URL
    if not webhook:
        logger.warning("SPIKE_DISCORD_WEBHOOK_URL not set -- not sent: %s", msg)
        return False
    if chart and _post_with_chart(msg, webhook, chart):
        return True
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps({"content": msg[:1900]}).encode(),
            # Discord/Cloudflare 403s the default urllib User-Agent
            headers={"Content-Type": "application/json", "User-Agent": "spike-scanner/1.0"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:
        logger.warning("Discord post failed: %s", exc)
        return False


def _post_with_chart(msg: str, webhook: str, chart: str) -> bool:
    """Multipart upload so the chart renders inline under the text.

    Returns False on any failure so the caller falls back to a plain text
    post -- an alert without its picture still beats no alert. requests is
    already a dependency and handles multipart in one line; hand-rolling it
    over urllib would be a page of boundary bookkeeping for nothing.
    """
    try:
        import requests
        with open(chart, "rb") as fh:
            resp = requests.post(
                webhook,
                data={"payload_json": json.dumps({"content": msg[:1900]})},
                files={"file": ("chart.png", fh, "image/png")},
                headers={"User-Agent": "spike-scanner/1.0"}, timeout=20)
        if resp.status_code < 300:
            return True
        logger.warning("Discord chart post rejected (%s) -- retrying text-only",
                       resp.status_code)
    except Exception as exc:
        logger.warning("Discord chart post failed (%s) -- retrying text-only", exc)
    return False


def _wave_lines(ev: dict) -> str:
    """The wave-context block, or nothing at all when there is no count.

    Two facts drive it: which wave the retouch landed in, and whether the block
    pushes with the leg or against it. When it opposes, the tradeable read is
    the BREAK of the zone rather than the bounce off it -- so the side named
    here flips. That is the failed-auction case, and it is stated in words
    because a bare direction arrow cannot carry it.
    """
    wc = ev.get("wave_context")
    if not wc:
        return ""
    bull = ev["direction"] == "bullish"
    if wc["agrees"]:
        side, plan = ("LONG" if bull else "SHORT"), "expect the zone to HOLD"
        stance = "agrees with"
    else:
        side, plan = ("SHORT" if bull else "LONG"), "expect the zone to FAIL"
        stance = "opposes"
    return (f"**wave {wc['wave']} of 5** · leg running {wc['leg_dir']} · {wc['where']}\n"
            f"retouch {stance} the leg → {side} · {plan}\n")


def send_ob_alert(ev: dict, webhook: str | None = None,
                  chart: str | None = None) -> bool:
    """Order block formation/retouch. Formation is the cue to check the wave
    count; retouch is the actual entry trigger -- so they're visually distinct."""
    arrow = "▲" if ev["direction"] == "bullish" else "▼"
    grade = f"  `[{ev['grade']}]`" if ev.get("grade") else ""
    if ev["event"] == "formation":
        head = f"📦 **{ev['ticker']}** [{ev['tf']}] {arrow} OB FORMED — check wave count{grade}"
        when = f"swing {ev['swing_ts']} -> broke {ev['formation_ts']}"
    else:
        head = f"🎯 **{ev['ticker']}** [{ev['tf']}] {arrow} OB RETOUCH — entry trigger{grade}"
        when = f"formed {ev['formation_ts']}, retouched {ev['retouch_ts']}"
    vol = f", vol_spike {ev['vol_spike']}x" if ev.get("vol_spike") is not None else ""
    return _post(
        f"{head}\n"
        f"{_wave_lines(ev)}"
        f"zone: {ev['zone_lo']}-{ev['zone_hi']}  entry: {ev['entry']}  stop: {ev['stop']}  "
        f"risk: {ev['risk_pct']}%\n"
        f"{_level_note(ev.get('break_lookback', 0))}, move {ev['move_pct']}%\n"
        f"{when}{vol}",
        webhook, chart,
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
