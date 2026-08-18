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

LEVEL_SCAN_CAP = 500   # bars; upper bound on the "how long had this level stood" walk


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
            # apex = furthest the move ran before coming back. Frozen at the
            # retouch: afterwards the block is in the trade, not the run-up.
            if z["retouch_i"] is None:
                z["apex"] = max(z["apex"], h[i])
                if l[i] <= z["trigger"]:
                    z["retouch_i"] = i              # ENTRY event
                    # ATR as of the entry bar -- the stop is sized off current
                    # volatility, not off the OB candle's own geometry.
                    z["atr_at_entry"] = float(atr[i]) if not np.isnan(atr[i]) else np.nan
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
        if np.isnan(atr[i]):
            continue
        # Volume is optional here: an order block is purely structural (swing
        # break + extreme candle in the leg), so it works fine without it.
        # Spot indices report volume=0 for every bar, and illiquid names can
        # have a genuinely empty 20-bar window -- in both cases avgvol is 0 or
        # NaN, which is not a reason to skip the block, only a reason to have
        # no vol_spike to report. (The thrust-retest detector is different --
        # volume spike and decline are core conditions there, so it correctly
        # finds nothing on indices.)
        has_vol = not np.isnan(avgvol[i]) and avgvol[i] > 0

        lo_j, hi_j = swing_x + 1, i - 1
        if hi_j < lo_j:
            continue
        j = lo_j + int(np.argmin(zone_bot[lo_j:hi_j + 1]))
        z_lo, z_hi = zone_bot[j], zone_top[j]
        if z_hi <= z_lo:
            continue

        # How far into the zone price must come before it counts as a retest.
        # 0.0 = the proximal edge (any graze of the top), 1.0 = the distal edge
        # (price must cross the whole block -- the line LuxAlgo actually draws).
        # The proximal edge fires on the shallowest wobble: on a 2.7%-tall zone
        # a bar dipping 0.5 points in was being recorded as a full retest.
        trigger = z_hi - cfg.OB_RETOUCH_DEPTH * (z_hi - z_lo)

        # How long the swing just broken had stood unchallenged. The swing test
        # itself only looks back swing_len (10) bars, so clearing it can mean a
        # minor local high or a genuinely long-standing one -- very different
        # events, and the alert should distinguish them. Capped to keep this
        # from becoming a full-history walk on a quiet name.
        k, floor = swing_x - 1, max(0, swing_x - 1 - LEVEL_SCAN_CAP)
        while k >= floor and h[k] < swing_y:
            k -= 1
        break_lookback = swing_x - 1 - k

        live.append(dict(
            swing_i=swing_x, form_i=i, ob_i=j, zone_lo=float(z_lo), zone_hi=float(z_hi),
            break_lookback=int(break_lookback),
            trigger=float(trigger),
            # stop sits beyond the OB candle's WICK, not its body -- with the
            # entry now at the distal edge, a stop at the body bottom would be
            # taken out by the very bar that triggers the entry.
            ob_low=float(l[j]), ob_high=float(h[j]),
            vol_spike=float(v[i] / avgvol[i]) if has_vol else None,
            leg_bars=i - swing_x, apex=float(h[i]),
            # ATR at the break, so a FORMATION alert can quote a sensible stop
            # before any retouch exists. Superseded by atr_at_entry once price
            # actually comes back.
            atr_at_form=float(atr[i]),
            retouch_i=None, breaker_i=None, invalid_i=None, atr_at_entry=np.nan,
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
        z["ob_low"], z["ob_high"] = -z["ob_high"], -z["ob_low"]
        z["apex"] = -z["apex"]          # a mirrored high is a real low
        z["trigger"] = -z["trigger"]
        z["direction"] = "bearish"
        out.append(z)
    return out


def entry_stop(z: dict, atr_mult: float | None = None) -> tuple[float, float]:
    """Entry is wherever the retest triggers. The stop is ATR-based, placed a
    multiple of current volatility beyond it.

    Anything derived from the OB candle itself -- the zone floor, or the wick
    past it -- turned out to be far too tight to trade: median stops of
    0.10-0.57%, against ~0.25% round-trip costs, and on 5min the wick version
    collapsed to zero risk 63% of the time because the chosen candle usually
    opens or closes right at its extreme. Volatility doesn't have that
    problem, and it can't be degenerate.

    Falls back to the wick if ATR is unavailable (too few bars of history).
    """
    mult = cfg.OB_STOP_ATR_MULT if atr_mult is None else atr_mult
    entry = z["trigger"]
    # Prefer ATR at the retouch; before that exists (a FORMATION alert) fall
    # back to ATR at the break. Without this the formation path dropped to the
    # wick stop, which is degenerate on most blocks -- 96% of them failed the
    # risk gate, so formation alerts never fired at all.
    atr = z.get("atr_at_entry", np.nan)
    if atr is None or np.isnan(atr):
        atr = z.get("atr_at_form", np.nan)
    bull = z["direction"] == "bullish"
    if atr is None or np.isnan(atr) or atr <= 0:
        return entry, (z["ob_low"] if bull else z["ob_high"])
    return entry, (entry - mult * atr) if bull else (entry + mult * atr)


def move_pct(z: dict) -> float:
    """How far the breakout ran past the block before coming back, as a % of
    the entry price -- i.e. the size of the extension leg. This is the 'how
    much room was there' measure, distinct from zone thickness."""
    if z["direction"] == "bullish":
        entry, run = z["zone_hi"], z["apex"] - z["zone_hi"]
    else:
        entry, run = z["zone_lo"], z["zone_lo"] - z["apex"]
    return run / entry * 100 if entry else float("nan")


def _event(df, ticker, tf, z, kind):
    idx = df.index
    zone_lo, zone_hi = z["zone_lo"], z["zone_hi"]
    entry, stop = entry_stop(z)
    risk_pct = abs(entry - stop) / entry * 100 if entry else float("nan")
    return dict(
        pattern="OB", event=kind, ticker=ticker, tf=tf, direction=z["direction"],
        swing_ts=str(idx[z["swing_i"]]), formation_ts=str(idx[z["form_i"]]),
        ob_ts=str(idx[z["ob_i"]]),
        retouch_ts=str(idx[z["retouch_i"]]) if z["retouch_i"] is not None else None,
        zone_lo=round(zone_lo, 2), zone_hi=round(zone_hi, 2),
        entry=round(entry, 2), stop=round(stop, 2),
        risk_pct=round(risk_pct, 2),
        apex=round(z["apex"], 2), move_pct=round(move_pct(z), 2),
        break_lookback=int(z.get("break_lookback", 0)),
        vol_spike=round(z["vol_spike"], 2) if z.get("vol_spike") is not None else None,
        leg_bars=int(z["leg_bars"]),
    )


def latest_events(df: pd.DataFrame, ticker: str, tf: str) -> list[dict]:
    """Live entry point: events whose triggering bar is the LAST bar in df.

    Returns formation and/or retouch events that just fired. Anything whose
    entry-to-stop distance is under this timeframe's floor is dropped -- at a
    ~0.25% round-trip cost, a stop that tight spends most of its R getting in
    and out.
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
    floor = cfg.OB_MIN_RISK_PCT_BY_TF.get(tf, 0.5)
    return [e for e in events if e["risk_pct"] >= floor]
