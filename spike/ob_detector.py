"""Order Block detector -- a second, independent pattern alongside detector.py.

Port of the LuxAlgo ICT Concepts OB logic: a swing high/low is broken, and the
most extreme candle inside that leg becomes the "order block" -- the zone the
move originated from. Price returning into that zone is the trade trigger.

Two events are emitted, deliberately:
  FORMATION  the swing broke and the zone is now marked. Nothing to trade yet,
             but this is the lead time to check the Elliott wave count before
             price comes back.
  RETOUCH    price traded back into the zone -- the entry trigger.

A block is never discarded for going untouched; it stays live until price
actually invalidates it (closes through the far side, then reclaims it), the
same lifecycle the chart version uses.

Unlike detector.py this is a sequential state machine rather than a vectorised
scan -- the swing state alternates and each swing fires at most once, which
can't be expressed as a mask.
"""
import numpy as np
import pandas as pd

from . import config as cfg


def _atr_avgvol(df):
    prev_close = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - prev_close).abs(),
                    (df.low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean().values, df.volume.rolling(20).mean().values


def _find_bull(df, swing_len, use_body):
    """Bullish blocks only; bearish comes from running this on a mirrored frame."""
    o, h, l, c, v = (df.open.values, df.high.values, df.low.values,
                     df.close.values, df.volume.values)
    atr, avgvol = _atr_avgvol(df)
    n = len(df)

    zone_top = np.maximum(o, c) if use_body else h
    zone_bot = np.minimum(o, c) if use_body else l
    body_bot = np.minimum(o, c)        # the breaker test is body-based

    done, live = [], []
    state = None                       # 0 = last swing was a high, 1 = a low
    swing_x, swing_y = -1, np.nan
    crossed = True                     # nothing to break until a swing exists

    for i in range(swing_len, n):
        # --- swing state machine (alternating, most-recent-only) ---
        cand = i - swing_len
        if h[cand] > h[cand + 1:i + 1].max():
            if state != 0:             # only the TRANSITION records a swing
                state = 0
                swing_x, swing_y = cand, h[cand]
                crossed = False
        elif l[cand] < l[cand + 1:i + 1].min():
            state = 1

        # --- age every live block against this bar, before creating today's ---
        still_live = []
        for z in live:
            if z["retouch_i"] is None and l[i] <= z["zone_hi"]:
                z["retouch_i"] = i                  # ENTRY event
            if z["breaker_i"] is None:
                if body_bot[i] < z["zone_lo"]:
                    z["breaker_i"] = i              # zone failed
            elif c[i] > z["zone_hi"]:
                z["invalid_i"] = i                  # reclaimed -> retire
                done.append(z)
                continue
            still_live.append(z)
        live = still_live

        # --- break of the live swing high (FORMATION) ---
        if crossed or swing_x < 0 or c[i] <= swing_y:
            continue
        crossed = True                 # one-shot: this swing can't fire again
        # avgvol can be a legitimate 0 (illiquid name, or a session with no
        # trades in the whole 20-bar window) -- isnan alone doesn't catch that,
        # and dividing by it yields inf/nan plus a RuntimeWarning per bar.
        if np.isnan(atr[i]) or np.isnan(avgvol[i]) or avgvol[i] <= 0:
            continue

        lo_j, hi_j = swing_x + 1, i - 1
        if hi_j < lo_j:
            continue
        j = lo_j + int(np.argmin(zone_bot[lo_j:hi_j + 1]))
        z_lo, z_hi = zone_bot[j], zone_top[j]
        if z_hi <= z_lo:
            continue

        live.append(dict(
            swing_i=swing_x, form_i=i, ob_i=j, zone_lo=float(z_lo), zone_hi=float(z_hi),
            vol_spike=float(v[i] / avgvol[i]), leg_bars=i - swing_x,
            retouch_i=None, breaker_i=None, invalid_i=None,
        ))

    done.extend(live)
    return done


def _mirror(df):
    return pd.DataFrame({"open": -df.open, "high": -df.low, "low": -df.high,
                         "close": -df.close, "volume": df.volume}, index=df.index)


def find_blocks(df: pd.DataFrame, swing_len: int | None = None,
                use_body: bool | None = None) -> list[dict]:
    """All order blocks in the frame, both directions, each with its lifecycle."""
    swing_len = cfg.OB_SWING_LEN if swing_len is None else swing_len
    use_body = cfg.OB_USE_BODY if use_body is None else use_body

    out = []
    for z in _find_bull(df, swing_len, use_body):
        z["direction"] = "bullish"
        out.append(z)
    for z in _find_bull(_mirror(df), swing_len, use_body):
        z["zone_lo"], z["zone_hi"] = -z["zone_hi"], -z["zone_lo"]
        z["direction"] = "bearish"
        out.append(z)
    return out


def _event(df, ticker, tf, z, kind):
    idx = df.index
    zone_lo, zone_hi = z["zone_lo"], z["zone_hi"]
    bull = z["direction"] == "bullish"
    # entry at the zone edge price first meets, stop beyond the far edge
    entry, stop = (zone_hi, zone_lo) if bull else (zone_lo, zone_hi)
    risk_pct = abs(entry - stop) / entry * 100 if entry else float("nan")
    return dict(
        pattern="OB", event=kind, ticker=ticker, tf=tf, direction=z["direction"],
        swing_ts=str(idx[z["swing_i"]]), formation_ts=str(idx[z["form_i"]]),
        ob_ts=str(idx[z["ob_i"]]),
        retouch_ts=str(idx[z["retouch_i"]]) if z["retouch_i"] is not None else None,
        zone_lo=round(zone_lo, 2), zone_hi=round(zone_hi, 2),
        entry=round(entry, 2), stop=round(stop, 2),
        risk_pct=round(risk_pct, 2), vol_spike=round(z["vol_spike"], 2),
        leg_bars=int(z["leg_bars"]),
    )


def latest_events(df: pd.DataFrame, ticker: str, tf: str) -> list[dict]:
    """Live entry point: events whose triggering bar is the LAST bar in df.

    Returns formation and/or retouch events that just fired. A zone thinner
    than OB_MIN_RISK_PCT is skipped -- with round-trip costs around 0.25%, a
    zone that tight has less edge than it costs to trade, and most raw OB
    zones are far tighter than that.
    """
    if len(df) < cfg.OB_MIN_BARS:
        return []
    last = len(df) - 1
    events = []
    for z in find_blocks(df):
        if cfg.OB_ALERT_FORMATION and z["form_i"] == last:
            events.append(_event(df, ticker, tf, z, "formation"))
        if cfg.OB_ALERT_RETOUCH and z["retouch_i"] == last:
            events.append(_event(df, ticker, tf, z, "retouch"))
    return [e for e in events if e["risk_pct"] >= cfg.OB_MIN_RISK_PCT]
