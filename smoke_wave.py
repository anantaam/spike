"""Server-side smoke test: does wave tagging actually produce counts on the
live cache, for all three segments, and does a chart render headless?

Reads the cache directly rather than going through Kite, so it is safe to run
while the service is up. Prints coverage, which is the number that matters:
if almost nothing carries a count the feature is cosmetic.
"""
import sys
from pathlib import Path

import pandas as pd

from spike import config, data_feed, wave, wavechart

CACHE = config.CACHE_DIR


def fake_event(sym, tf, cbars, wc):
    px = float(cbars.close.iloc[-1])
    return dict(ticker=sym, tf=tf, event="retouch",
                direction="bullish" if wc["leg_dir"] == "up" else "bearish",
                zone_lo=round(px * 0.99, 2), zone_hi=round(px * 1.01, 2),
                entry=round(px, 2), stop=round(px * 0.985, 2), risk_pct=1.5,
                move_pct=0, break_lookback=0, grade=wave.grade(wc),
                formation_ts=str(cbars.index[-5]), retouch_ts=str(cbars.index[-1]))


def probe(label, series, tf):
    hits = charted = 0
    for sym, cbars in series:
        if cbars is None or len(cbars) < config.WAVE_SPAN_HI + 5:
            continue
        wc = wave.context(cbars, cbars.index[-1], "bullish", key=sym)
        if wc is None:
            continue
        hits += 1
        if hits == 1:
            ev = fake_event(sym, tf, cbars, wc)
            p = wavechart.render(ev, wc, cbars)
            charted = 1 if p else 0
            print(f"  {label} sample: {sym} wave {wc['wave']} {wc['where']} "
                  f"span={wc['span']} score={wc['score']} grade={wave.grade(wc)}")
            print(f"  {label} chart : {p}")
    print(f"  {label}: {hits}/{len(series)} carry a count, chart_ok={charted}")
    return hits


def load(path):
    try:
        d = pd.read_csv(path, parse_dates=["date"], index_col="date").sort_index()
        return d if len(d) else None
    except Exception:
        return None


def main():
    print("== equity (daily) ==")
    eq = [(p.name.split("_")[0], load(p))
          for p in sorted(CACHE.glob("*_daily.csv"))[:40]]
    probe("equity", eq, "1h")

    print("== commodity (4h from 1-min) ==")
    mcx = []
    for p in sorted(CACHE.glob("*FUT_1min.csv")):
        d = load(p)
        if d is None:
            continue
        mcx.append((p.name.split("_")[0],
                    data_feed.resample(d, config.WAVE_CONTEXT_TF["commodity"], 0)))
    if not mcx:
        print("  commodity: no *FUT 1-min cache yet")
    else:
        print(f"  commodity bar counts: "
              f"{ {s: len(b) for s, b in mcx} }")
        probe("commodity", mcx, "1h")

    print("== crypto (daily from Binance) ==")
    from spike import binance_feed
    cr = [(s, binance_feed.fetch_with_retry(s, "1D"))
          for s in config.CRYPTO_UNIVERSE[:5]]
    probe("crypto", cr, "1h")
    return 0


if __name__ == "__main__":
    sys.exit(main())
