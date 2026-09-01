"""Re-post already-fired OB alerts in the wave-tagged format.

The events are recovered from the journal rather than re-detected, so what
goes out is exactly what fired -- same zone, same entry, same timestamps. A
re-run of the detector would silently use whatever the cache looks like now
and could produce a different set.

    ./venv/bin/python replay_eod.py --tf 1D --dry      # show, post nothing
    ./venv/bin/python replay_eod.py --tf 1D            # actually post

Replayed messages carry a REPLAY header so they cannot be mistaken for a new
trigger firing now.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from datetime import date

import pandas as pd

from spike import config, notifier, wave, wavechart

# Events were logged with repr(), so numpy scalars appear as np.float64(1.2).
# literal_eval refuses those; unwrapping to a bare float is exact.
_NP = re.compile(r"np\.(?:float64|int64)\(([^)]*)\)")
_LINE = re.compile(r"INFO spike\.main: OB (\{.*\})\s*$")


def events(since: str, until: str, unit: str) -> list[dict]:
    out = subprocess.run(
        ["journalctl", "-u", unit, "--since", since, "--until", until, "--no-pager"],
        capture_output=True, text=True, check=True).stdout
    evs = []
    for line in out.splitlines():
        m = _LINE.search(line)
        if m:
            evs.append(ast.literal_eval(_NP.sub(r"\1", m.group(1))))
    return evs


def daily(ticker: str):
    p = config.CACHE_DIR / f"{ticker}_daily.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"], index_col="date").sort_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1D")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--from-time", default="15:30:00")
    ap.add_argument("--to-time", default="16:30:00")
    ap.add_argument("--unit", default="spike.service")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    evs = [e for e in events(f"{a.date} {a.from_time}", f"{a.date} {a.to_time}", a.unit)
           if e.get("tf") == a.tf]
    if not evs:
        print("no events found in that window")
        return 1

    # Refuse to run blind. A missing webhook is only a warning inside _post,
    # so without this the script happily "sends" every alert into nothing and
    # reports success -- which is exactly what happened the first time.
    if not a.dry and not config.DISCORD_WEBHOOK_URL:
        print("ERROR: no equity webhook configured. Check .env / SPIKE_ENV_FILE.",
              file=sys.stderr)
        return 2

    real_post = notifier._post
    posted = []

    def tagged_post(msg, webhook=None, chart=None):
        msg = f"🔁 **REPLAY** — {a.date} {a.tf} scan, re-sent with wave context\n{msg}"
        ok = True if a.dry else real_post(msg, webhook, chart)
        posted.append((msg, chart, ok))
        return ok

    notifier._post = tagged_post

    for ev in evs:
        cbars = daily(ev["ticker"])
        ts = pd.Timestamp(ev.get("retouch_ts") or ev["formation_ts"])
        wc = wave.context(cbars, ts, ev["direction"], key=ev["ticker"]) if cbars is not None else None
        ev["grade"] = wave.grade(wc)
        chart = None
        if wc is not None:
            ev["wave_context"] = {k: wc[k] for k in
                                  ("wave", "leg_dir", "where", "agrees", "score", "span")}
            chart = wavechart.render(ev, wc, cbars)
        notifier.send_ob_alert(ev, chart=chart)

    for msg, chart, ok in posted:
        print("=" * 70)
        print(msg)
        print(f"[chart: {chart}]  [{'ok' if ok else 'NOT DELIVERED'}]")
    ok_n = sum(1 for *_, ok in posted if ok)
    print("=" * 70)
    print(f"{'WOULD POST' if a.dry else 'DELIVERED'} {ok_n}/{len(posted)} alert(s); "
          f"{sum(1 for _, c, ok in posted if c and ok)} with charts")
    return 0 if ok_n == len(posted) else 1


if __name__ == "__main__":
    sys.exit(main())
