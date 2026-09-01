"""Self-check for the wave-context tagging path.

Runs the real detector output through wave.context -> notifier, with the
Discord post stubbed, and asserts the pieces that would silently degrade:
the count must be found, the wave/agreement tags must reach the message, and
an opposing retouch must flip the named side to the break trade.

    python test_wave.py            # needs ../wavelab/data for the daily bars
"""
import sys
from pathlib import Path

import pandas as pd

from spike import notifier, ob_detector, wave, wavechart

DATA = Path(__file__).resolve().parent.parent / "wavelab" / "data"

sent = []
notifier._post = lambda msg, hook=None, chart=None: sent.append((msg, chart))


def bars(sym, kind):
    return pd.read_csv(DATA / f"{sym}_{kind}.csv",
                       parse_dates=["date"], index_col="date").sort_index()


def intraday(sym, mins):
    return (bars(sym, "1min").between_time("09:15", "15:29")
            .resample(f"{mins}min", offset="15min", label="left", closed="left")
            .agg({"open": "first", "high": "max", "low": "min",
                  "close": "last", "volume": "sum"}).dropna(subset=["open"]))


def main():
    if not DATA.exists():
        print(f"SKIP: no local data at {DATA}")
        return 0

    sym, agree_seen, oppose_seen = "ZYDUSLIFE", False, False
    daily, hourly = bars(sym, "daily"), intraday(sym, 60)

    fit = wave.best_fit(daily, key=sym)
    assert fit is not None, "no wave count found on a chart known to carry one"
    assert 100 <= fit["span"] <= 140, f"span {fit['span']} outside the degree window"
    assert len(fit["points"]) == 5

    for z in ob_detector.find_blocks(hourly):
        if z["retouch_i"] is None:
            continue
        entry, stop = ob_detector.entry_stop(z)
        ts = hourly.index[z["retouch_i"]]
        wc = wave.context(daily, ts, z["direction"], key=sym)
        if wc is None:
            continue
        ev = dict(ticker=sym, tf="1h", event="retouch", direction=z["direction"],
                  zone_lo=round(z["zone_lo"], 1), zone_hi=round(z["zone_hi"], 1),
                  entry=round(entry, 1), stop=round(stop, 1),
                  risk_pct=round(abs(entry - stop) / entry * 100, 2),
                  move_pct=z.get("move_pct", 0), break_lookback=z.get("break_lookback", 0),
                  formation_ts=str(hourly.index[z["form_i"]]), retouch_ts=str(ts),
                  grade=wave.grade(wc),
                  wave_context={k: wc[k] for k in
                                ("wave", "leg_dir", "where", "agrees", "score", "span")})
        chart = wavechart.render(ev, wc, daily)
        notifier.send_ob_alert(ev, chart=chart)
        msg = sent[-1][0]
        assert f"wave {wc['wave']} of 5" in msg
        # The failed-auction case: an opposing retouch must name the BREAK
        # trade on the far side, not the bounce. This is the assertion that
        # catches a regression in _wave_lines flipping the wrong way.
        if wc["agrees"]:
            agree_seen = True
            assert "HOLD" in msg and "agrees with" in msg
        else:
            oppose_seen = True
            assert "FAIL" in msg and "opposes" in msg
            want = "SHORT" if z["direction"] == "bullish" else "LONG"
            assert f"→ {want} ·" in msg, msg

    assert sent, "no tagged events produced"
    assert agree_seen and oppose_seen, "fixture must cover both hold and fail"
    print(f"OK  {len(sent)} tagged alerts, both agree and oppose covered")
    print(f"OK  chart rendered: {sent[-1][1]}")
    print("\n--- last message ---\n" + sent[-1][0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
