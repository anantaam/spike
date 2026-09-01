"""Elliott wave context for an order-block alert.

Not a trading signal on its own. It answers one question about a retouch that
has already been detected: WHERE in the current wave count did it happen, and
does it push with the leg or against it. Those two fields were the only ones
that moved expectancy in the local payoff study, which is why they are the
only ones computed here.

Ported from the wavelab prototype (fracpivots.py + forming.py) unchanged
except for the live-serving wrapper at the bottom.
"""
from __future__ import annotations

import logging
from itertools import combinations

import pandas as pd

from . import config

logger = logging.getLogger(__name__)


def fractal_pivots(bars: pd.DataFrame, n: int = 1) -> list[dict]:
    """Alternating high/low pivots from N-bar fractals.

    N=1 fractals key off actual local extremes, so their density falls as the
    timeframe rises -- which is what makes "go to a higher timeframe until the
    count spans 100-140 bars" a meaningful operation. An ATR zigzag is
    scale-invariant and cannot do that.
    """
    h, l = bars.high.values, bars.low.values
    # Ties matter. Strict > on BOTH sides makes a flat extreme invisible:
    # ZYDUSLIFE printed 1205.0 on two consecutive days, so neither bar beat its
    # neighbour and the wave-3 peak vanished. >= against the previous bar and >
    # against the next keeps exactly one pivot at the end of a plateau.
    raw = []
    for i in range(n, len(bars) - n):
        if all(h[i] >= h[i - k] for k in range(1, n + 1)) and \
           all(h[i] > h[i + k] for k in range(1, n + 1)):
            raw.append((i, "H", float(h[i])))
        elif all(l[i] <= l[i - k] for k in range(1, n + 1)) and \
             all(l[i] < l[i + k] for k in range(1, n + 1)):
            raw.append((i, "L", float(l[i])))

    out = []
    for idx, kind, price in raw:
        if out and out[-1]["kind"] == kind:
            better = price > out[-1]["price"] if kind == "H" else price < out[-1]["price"]
            if better:
                out[-1] = {"idx": idx, "ts": bars.index[idx], "price": price, "kind": kind}
            continue
        out.append({"idx": idx, "ts": bars.index[idx], "price": price, "kind": kind})
    return out


def _score4(p, direction, atr=None):
    """p = [P0,1,2,3,4]. Wave-3-shortest cannot be judged while 5 is forming."""
    s, notes = 0.0, []
    sgn = 1 if direction == "long" else -1
    d = [sgn * (p[i + 1] - p[i]) for i in range(4)]
    if d[0] <= 0 or d[1] >= 0 or d[2] <= 0 or d[3] >= 0:
        return -1e9, ["not alternating"]
    w1, w2, w3, w4 = d[0], -d[1], d[2], -d[3]
    # A leg shorter than one ATR is not a wave, it is noise. Without this the
    # scorer accepted horizontal lines: the guideline bands are RATIOS, so a
    # tiny wave 2 over a tiny wave 1 lands in the 50-62% band just as neatly.
    if atr and min(w1, w2, w3, w4) < config.WAVE_MIN_LEG_ATR * atr:
        return -1e9, ["a leg is smaller than 1 ATR -- noise, not a wave"]
    if w2 >= w1:
        s -= 40; notes.append("R1 wave2 retraced >100% of wave1")
    overlap = sgn * (p[1] - p[4])
    if overlap > 0:
        pct = overlap / w1 if w1 else 9
        s -= 40 if pct > 0.30 else 12
        notes.append(f"R3 wave4 overlaps wave1 by {pct:.0%}")
    r2, r4 = (w2 / w1 if w1 else 0), (w4 / w3 if w3 else 0)
    s += 18 if 0.50 <= r2 <= 0.62 else (9 if 0.38 <= r2 <= 0.79 else 0)
    s += 18 if 0.38 <= r4 <= 0.50 else (9 if 0.23 <= r4 <= 0.62 else 0)
    if w3 >= 1.62 * w1:
        s += 14; notes.append("wave3 extended >=1.62x")
    if w3 >= w1:
        s += 12
    return s, notes


def _extremes_ok(pts, pv, end, direction):
    """Every labelled point must be the extreme of its own segment.

    An N=1 fractal is merely a bar higher than its two neighbours, which happens
    constantly mid-trend -- far too weak to mean "is a swing high". A wave pivot
    has to be the most extreme candidate between its neighbours, and P0 the
    extreme of the whole count. This constraint does most of the work.
    """
    p0, w1, w2, w3, w4 = pts
    lo_k, hi_k = ("L", "H") if direction == "long" else ("H", "L")
    worse = (lambda a, b: a <= b) if direction == "long" else (lambda a, b: a >= b)

    def extreme(kind, lo_i, hi_i, price, want_low):
        cands = [q for q in pv if q["kind"] == kind and lo_i <= q["idx"] <= hi_i]
        if not cands:
            return False
        best = min(c["price"] for c in cands) if want_low else max(c["price"] for c in cands)
        return abs(best - price) < 1e-9

    low_first = direction == "long"
    span_lows = [q["price"] for q in pv
                 if q["kind"] == lo_k and p0["idx"] <= q["idx"] <= end]
    if span_lows and not worse(p0["price"], min(span_lows) if low_first else max(span_lows)):
        return False
    return (extreme(hi_k, p0["idx"], w2["idx"], w1["price"], not low_first) and
            extreme(lo_k, w1["idx"], w3["idx"], w2["price"], low_first) and
            extreme(hi_k, w2["idx"], w4["idx"], w3["price"], not low_first) and
            extreme(lo_k, w3["idx"], end,       w4["price"], low_first))


def _atr(bars, length=14):
    pc = bars.close.shift(1)
    tr = pd.concat([bars.high - bars.low, (bars.high - pc).abs(),
                    (bars.low - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def find_forming(bars, direction="long", n_frac=1, as_of=None):
    """1-2-3-4 complete, wave 5 forming, P0->as_of inside the degree window."""
    end = len(bars) - 1 if as_of is None else as_of
    atr_s = _atr(bars)
    pv = [q for q in fractal_pivots(bars, n=n_frac) if q["idx"] <= end]
    lo_k, hi_k = ("L", "H") if direction == "long" else ("H", "L")
    out = []
    for p0 in [q for q in pv if q["kind"] == lo_k]:
        span = end - p0["idx"]
        if not (config.WAVE_SPAN_LO <= span <= config.WAVE_SPAN_HI):
            continue
        inner = [q for q in pv if p0["idx"] < q["idx"] <= end]
        highs = [q for q in inner if q["kind"] == hi_k]
        lows = [q for q in inner if q["kind"] == lo_k]
        # Keep every valid interior, not just the top scorer for this P0 --
        # ties are common, and collapsing early lets a stale count mask an
        # equally-scored actionable one. Recency is filtered downstream.
        for i1, i3 in combinations(highs, 2):
            for i2 in lows:
                if not (i1["idx"] < i2["idx"] < i3["idx"]):
                    continue
                for i4 in lows:
                    if i4["idx"] <= i3["idx"]:
                        continue
                    pts = [p0, i1, i2, i3, i4]
                    if not _extremes_ok(pts, pv, end, direction):
                        continue
                    a = atr_s.iloc[p0["idx"]]
                    sc, notes = _score4([q["price"] for q in pts], direction,
                                        None if pd.isna(a) else float(a))
                    if sc > -1e8:
                        out.append(dict(score=sc, direction=direction, points=pts,
                                        notes=notes, span=span, w4_idx=i4["idx"]))
    return sorted(out, key=lambda f: -f["score"])


# ---------------------------------------------------------------- live serving

_cache: dict = {}


def best_fit(cbars: pd.DataFrame, key: str | None = None) -> dict | None:
    """Highest-scoring count whose wave 4 is recent enough to still be live.

    Memoised on the last context-bar timestamp: the count only changes when a
    new context bar closes, but a single scan can raise several OB events on
    the same symbol and re-running the search per event costs 1-2s each.
    """
    if cbars is None or len(cbars) < config.WAVE_SPAN_HI + 5:
        return None
    ck = (key, cbars.index[-1]) if key else None
    if ck is not None and ck in _cache:
        return _cache[ck]

    end = len(cbars) - 1
    best = None
    for direction in ("long", "short"):
        for fit in find_forming(cbars, direction):
            if fit["score"] <= 0 or end - fit["w4_idx"] > config.WAVE_MAX_W4_AGE:
                continue
            if best is None or fit["score"] > best["score"]:
                best = fit
    if ck is not None:
        if len(_cache) > 4000:
            _cache.clear()
        _cache[ck] = best
    return best


def context(cbars: pd.DataFrame, ts, ob_direction: str,
            key: str | None = None) -> dict | None:
    """Where `ts` sits in the live count, and whether the OB pushes with it.

    Returns None when there is no acceptable count -- the alert still fires,
    just without wave tags. Absence of a count is not a veto; it only means we
    have nothing to say about the context.
    """
    fit = best_fit(cbars, key)
    if fit is None:
        return None
    pts = fit["points"]
    legs = [(str(i + 1), pts[i]["ts"], pts[i + 1]["ts"],
             "up" if pts[i + 1]["price"] > pts[i]["price"] else "down")
            for i in range(4)]
    # Wave 5 is still forming, so it has no known end. Its direction is known
    # (same as wave 3), its position is not -- reported as "forming" rather
    # than guessing a termination point we cannot see.
    legs.append(("5", pts[4]["ts"], cbars.index[-1],
                 "up" if fit["direction"] == "long" else "down"))

    for name, t0, t1, leg_dir in legs:
        if not (t0 <= ts <= t1):
            continue
        if name == "5":
            where = "forming"
        else:
            frac = (ts - t0).total_seconds() / max((t1 - t0).total_seconds(), 1)
            where = ("termination" if frac >= 0.8 else
                     "early" if frac <= 0.2 else "mid-leg")
        bull = ob_direction == "bullish"
        agrees = (bull and leg_dir == "up") or (not bull and leg_dir == "down")
        return dict(wave=name, leg_dir=leg_dir, where=where, agrees=agrees,
                    score=round(fit["score"], 1), span=fit["span"],
                    count_dir=fit["direction"], fit=fit)
    return None


def grade(wc: dict | None) -> str:
    """A = agrees, late in the count. B = agrees. C = opposes. ? = no count.

    Straight off the local payoff table: agreeing retouches carried positive
    expectancy in every wave and opposing ones negative, with the gap widest
    in waves 4 and 5. A triage label for reading the feed, not a position size.
    """
    if wc is None:
        return "?"
    if wc["agrees"]:
        return "A" if wc["wave"] in ("4", "5") else "B"
    return "C"
