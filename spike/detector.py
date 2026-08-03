"""Breakout-thrust + volume-confirmed retest detector.

This is the validated logic from the historical study, unchanged: a thrust
(one or more consecutive large-bodied candles) breaks a genuine recent
high/low on volume, extends further, then retests the origin zone (anchored
to the base immediately before the thrust, not wherever the thrust happened
to end) on declining volume.

`find_setups` scans a whole frame (used for backfill/testing). `latest_signal`
is the live entry point: it only reports a setup if the retest confirmed on
the most recent bar, since that's the only thing a live scan should alert on.
"""
import numpy as np
import pandas as pd

from . import config as cfg


def _indicators(df):
    df = df.copy()
    prev_close = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - prev_close).abs(),
                    (df.low - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["avg_vol"] = df.volume.rolling(20).mean()
    return df


def _find_runs(strong):
    runs, n, i = [], len(strong), 0
    while i < n:
        if strong[i]:
            j = i
            while j + 1 < n and strong[j + 1]:
                j += 1
            if j - i + 1 <= cfg.MAX_RUN:
                runs.append((i, j))
            i = j + 1
        else:
            i += 1
    return runs


def _adaptive_base(h, l, atr, s):
    if s - 1 < 0:
        return None
    hi, lo, start = h[s - 1], l[s - 1], s - 1
    for k in range(2, cfg.BASE_MAX + 1):
        i = s - k
        if i < 0:
            break
        new_hi, new_lo = max(hi, h[i]), min(lo, l[i])
        if (new_hi - new_lo) > cfg.BASE_CAP * atr[s - 1]:
            break
        hi, lo, start = new_hi, new_lo, i
    if s - start < cfg.BASE_MIN:
        return None
    return start, s


def _find_setups_one_side(df, ticker, tf):
    o, h, l, c, v = df.open.values, df.high.values, df.low.values, df.close.values, df.volume.values
    atr, avgvol = df.atr.values, df.avg_vol.values
    idx = df.index
    n = len(df)

    body = c - o
    strong = np.zeros(n, dtype=bool)
    valid = ~np.isnan(atr)
    strong[valid] = (body[valid] > cfg.STRONG_K * atr[valid])
    runs = _find_runs(strong)

    out = []
    for s, e in runs:
        if s < cfg.BASE_MAX or np.isnan(atr[s]) or np.isnan(avgvol[s]):
            continue
        total_move = c[e] - o[s]
        if total_move <= cfg.LARGE_K * atr[s]:
            continue
        if v[s:e + 1].mean() <= cfg.VOL_SPIKE_K * avgvol[s]:
            continue

        base = _adaptive_base(h, l, atr, s)
        if base is None:
            continue
        b0, b1 = base
        zone_hi, zone_lo = h[b0:b1].max(), l[b0:b1].min()
        zheight = zone_hi - zone_lo
        if zheight <= 0:
            continue

        level_start = max(0, s - cfg.LEVEL_LOOKBACK)
        recent_high = h[level_start:s].max()
        if zone_hi < recent_high - cfg.LEVEL_TOL_ATR * atr[s - 1]:
            continue
        if c[e] <= recent_high:
            continue

        j_end = min(e + cfg.MAX_WAIT, n - 1)
        extended, reversion_j = False, None
        for j in range(e + 1, j_end + 1):
            if h[j] >= zone_hi + cfg.MIN_EXTENSION * zheight:
                extended = True
            if extended and l[j] <= zone_hi:
                reversion_j = j
                break
        if reversion_j is None:
            continue

        leg_vol = v[e + 1:reversion_j + 1]
        if len(leg_vol) < 3:
            continue
        leg_close = c[e + 1:reversion_j + 1]
        far_violation = bool((leg_close < zone_lo).any())
        baseline_relative = leg_vol.mean() / avgvol[s]
        slope = np.polyfit(np.arange(len(leg_vol)), leg_vol, 1)[0] / max(leg_vol.mean(), 1)

        out.append(dict(
            ticker=ticker, tf=tf, thrust_start=idx[s], thrust_end=idx[e],
            run_len=e - s + 1, breakout_ts=idx[e], reversion_ts=idx[reversion_j],
            bars_to_revert=reversion_j - e, zone_lo=float(zone_lo), zone_hi=float(zone_hi),
            base_bars=b1 - b0, move_atr=total_move / atr[s], vol_spike=v[s:e + 1].mean() / avgvol[s],
            baseline_relative=baseline_relative, vol_slope=slope, far_violation=far_violation,
        ))
    return pd.DataFrame(out)


def _mirror(df):
    return pd.DataFrame({"open": -df.open, "high": -df.low, "low": -df.high,
                         "close": -df.close, "volume": df.volume}, index=df.index)


def find_setups(df: pd.DataFrame, ticker: str, tf: str) -> pd.DataFrame:
    """Scan a whole frame for setups. Returns the raw hits -- callers should
    apply the same clean filter used historically:
        baseline_relative < DECLINE_BASELINE_THRESH & vol_slope < 0 & ~far_violation
    """
    df = _indicators(df)
    up = _find_setups_one_side(df, ticker, tf)
    if not up.empty:
        up["direction"] = "bullish"

    mdf = _indicators(_mirror(df))
    dn = _find_setups_one_side(mdf, ticker, tf)
    if not dn.empty:
        dn[["zone_lo", "zone_hi"]] = -dn[["zone_hi", "zone_lo"]].values
        dn["direction"] = "bearish"

    return pd.concat([up, dn], ignore_index=True) if not (up.empty and dn.empty) else pd.DataFrame()


def clean(setups: pd.DataFrame) -> pd.DataFrame:
    if setups.empty:
        return setups
    return setups[(setups.baseline_relative < cfg.DECLINE_BASELINE_THRESH) &
                   (setups.vol_slope < 0) & (~setups.far_violation)]


def latest_signal(df: pd.DataFrame, ticker: str, tf: str) -> dict | None:
    """Live entry point: returns a single signal dict only if a clean setup's
    retest bar is the LAST bar in df (i.e. it just confirmed), else None."""
    if len(df) < cfg.MIN_BARS_REQUIRED:
        return None
    setups = clean(find_setups(df, ticker, tf))
    if setups.empty:
        return None
    last_ts = df.index[-1]
    hit = setups[setups.reversion_ts == last_ts]
    if hit.empty:
        return None
    row = hit.iloc[-1]
    zone_height = row.zone_hi - row.zone_lo
    if row.direction == "bullish":
        entry = df.loc[last_ts, "low"]
        stop = row.zone_lo
        extension = df.loc[row.thrust_end:row.reversion_ts, "high"].max() - row.zone_hi
        target = entry + extension
    else:
        entry = df.loc[last_ts, "high"]
        stop = row.zone_hi
        extension = row.zone_lo - df.loc[row.thrust_end:row.reversion_ts, "low"].min()
        target = entry - extension
    return dict(
        ticker=ticker, tf=tf, direction=row.direction, retest_ts=str(last_ts),
        thrust_start=str(row.thrust_start), thrust_end=str(row.thrust_end),
        zone_lo=round(float(row.zone_lo), 2), zone_hi=round(float(row.zone_hi), 2),
        entry=round(float(entry), 2), stop=round(float(stop), 2), target=round(float(target), 2),
        extension_size=round(float(extension), 2), vol_spike=round(float(row.vol_spike), 2),
    )
