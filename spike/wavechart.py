"""The chart that rides along with an order-block alert.

One panel, phone-readable: context-timeframe candles, the wave count in
orange, and the entry/stop/zone from the alert drawn across it. Deliberately
not a full analysis chart -- the point is to answer "does this look like a
wave, and where is the level" in the two seconds before the user decides
whether to open the terminal.

matplotlib is an optional dependency. If it is missing or rendering fails the
alert still goes out, text-only -- a chart is never worth losing an alert for.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

_BULL, _BEAR = "#26a69a", "#ef5350"
_WAVE, _WAVE_DK = "#ff9800", "#e65100"
_ENTRY, _STOP = "#1565c0", "#b71c1c"


def render(ev: dict, wc: dict, cbars) -> str | None:
    """PNG path, or None if a chart could not be produced."""
    if not config.OB_CHART_ENABLED or wc is None:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception as exc:
        logger.warning("matplotlib unavailable -- alerts will be text-only (%s)", exc)
        return None

    try:
        fit = wc["fit"]
        # Show the count plus a little air on the right, nothing more.
        bars = cbars.tail(min(len(cbars), fit["span"] + 40))
        x = mdates.date2num(bars.index.to_pydatetime())

        fig, ax = plt.subplots(figsize=(11, 5.2))
        fig.patch.set_facecolor("#ffffff")
        for xi, (_, r) in zip(x, bars.iterrows()):
            col = _BULL if r.close >= r.open else _BEAR
            ax.plot([xi, xi], [r.low, r.high], color=col, lw=0.8, zorder=1)
            ax.add_patch(plt.Rectangle((xi - 0.3, min(r.open, r.close)), 0.6,
                                       max(abs(r.close - r.open), 1e-6),
                                       facecolor=col, edgecolor=col, zorder=2))

        pts = fit["points"]
        px = [mdates.date2num(q["ts"].to_pydatetime()) for q in pts]
        py = [q["price"] for q in pts]
        ax.plot(px, py, color=_WAVE, lw=2, zorder=4)
        ax.scatter(px, py, s=26, color=_WAVE, zorder=5)
        for xi, yi, lab in zip(px, py, ["P0", "1", "2", "3", "4"]):
            ax.annotate(lab, (xi, yi), textcoords="offset points", ha="center",
                        xytext=(0, 11 if lab in ("1", "3") else -18),
                        color=_WAVE_DK, fontsize=11, fontweight="bold")
        # Wave 5 has no end yet -- dashed, to the last bar, so it reads as a
        # projection rather than a completed leg.
        ax.plot([px[-1], x[-1]], [py[-1], py[3]], color=_WAVE, lw=1.1,
                ls="--", alpha=0.5)

        entry, stop = ev["entry"], ev["stop"]
        lo, hi = sorted((ev["zone_lo"], ev["zone_hi"]))
        ax.axhspan(lo, hi, facecolor=_ENTRY, alpha=0.10, zorder=0)
        ax.axhline(entry, color=_ENTRY, lw=1.4, zorder=3)
        ax.axhline(stop, color=_STOP, lw=1.1, ls=":", zorder=3)
        ax.annotate(f"entry {entry:g}", (x[-1], entry), color=_ENTRY, fontsize=9.5,
                    fontweight="bold", textcoords="offset points", xytext=(-92, 5))
        ax.annotate(f"stop {stop:g}", (x[-1], stop), color=_STOP, fontsize=9.5,
                    textcoords="offset points", xytext=(-88, -13))

        ax.set_title(
            f"{ev['ticker']}   {ev['tf']} {ev['event']} in wave {wc['wave']}"
            f"   ·   {'agrees' if wc['agrees'] else 'opposes'} the leg"
            f"   ·   {wc['where']}   ·   grade {ev.get('grade', '?')}",
            fontsize=11.5, loc="left", pad=11)
        ax.grid(alpha=0.15)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        fig.autofmt_xdate()

        out = Path(tempfile.gettempdir()) / f"spike_{ev['ticker']}_{ev['tf']}.png"
        fig.savefig(out, dpi=100, bbox_inches="tight", facecolor="#ffffff")
        plt.close(fig)
        return str(out)
    except Exception as exc:
        logger.warning("chart render failed for %s/%s (%s)",
                       ev.get("ticker"), ev.get("tf"), exc)
        return None
