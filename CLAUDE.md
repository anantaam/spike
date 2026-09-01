# spike — project notes

Live breakout-thrust + volume-confirmed-retest scanner for NSE F&O names, via
Kite Connect. Alerts to Discord only — no order placement. This file is a
running log of what's been built, why, and the open decisions, for continuity
across sessions.

## The pattern being detected

A large candle (or a short run of consecutive large-bodied candles — a
"thrust") breaks a genuine recent high/low on significant volume, price
extends further in that direction, then pulls back and retests the origin
zone — the base it broke out from, not wherever the thrust happened to end —
on visibly declining volume. The retest is a wick-touch back into that zone;
volume decline is measured relative to the pre-thrust baseline average, not
relative to the breakout candle's own (naturally decaying) spike.

## How the detector evolved (in order, each fixing a real bug found by visual inspection against the reference examples)

1. **v1 (rule-based, single-candle)**: rolling-window zone, ATR/volume
   multiplier thresholds, wick-touch retest. Worked on 1-min bars — flagged
   as wrong scope; 1-min is noise for this pattern, 5m/15m/1h/1D matter.
2. Naive volume-decline check (leg volume vs. the breakout candle's own
   volume) passed ~90% of candidates — proved to be a near-no-op, since
   spikes decay on their own regardless of pattern quality. Fixed to compare
   against the pre-breakout baseline average instead (~19% pass rate) — this
   was the single biggest precision improvement.
3. **v2 (multi-timeframe)**: re-ran on 5min/15min/1h (resampled from 1-min)
   and 1D (native daily bars). Visual inspection surfaced two more bugs:
   fast-reversal false positives (price blows straight through the zone in
   the opposite direction, "touches" it only incidentally) and a zone
   sometimes anchored to a stray swing spike instead of a real base.
4. **v3**: added (a) multi-candle thrust support — a breakout doesn't have to
   be one candle, just consecutive large-bodied ones, capped at 5; (b) zone
   anchored to the base *before* the thrust starts, not a fixed window ending
   at whichever candle tripped the threshold (which for multi-candle thrusts
   is often mid-move); (c) a leg-violation check (price must not close beyond
   the zone's far side during the pullback) to kill the fast-reversal false
   positives; (d) a "must break a genuine recent high/low" check — the base
   has to actually sit within ~1.5 ATR of the highest/lowest point in the
   last 60 bars, not just be a random local range partway through a trend.
5. Zone-width tuning: the adaptive base-window function originally had a
   floor bug (always included ≥5 bars regardless of whether they were
   actually flat), letting ramp-up candles leading into the thrust get
   swallowed into "the zone." Fixed to walk backward one bar at a time and
   reject the candidate outright if even the minimum window isn't tight,
   rather than force a width. Settled params: `BASE_MIN,MAX,CAP = 5,40,4.5`,
   `LEVEL_LOOKBACK=60`, `LEVEL_TOL_ATR=1.5` (see `spike/config.py`).

## Historical validation (full F&O universe, 213 tickers)

Detector run across the whole postgres-backed history (5+ years intraday/EOD
in `D:\pgsql`), producing 3,778 clean candidates: 5min 2665, 15min 976, 1h
113, 1D 24.

Pseudo-backtest methodology: entry = retest wick touch price; 100% =
distance from zone edge to the actual extension-leg peak/trough (not zone
height); forward scan uncapped, tracking max favorable excursion until either
a stop trigger or end of data.

**Stop = close-confirmed** (a full candle must close beyond the far zone edge):

| tf | n | moved favorably | reached ≥50% | reached ≥100% | median MFE% | eventually stopped out* |
|---|---|---|---|---|---|---|
| 5min | 2665 | 99.8% | 69.6% | 51.2% | 103.2% | 95.6% |
| 15min | 976 | 99.8% | 71.2% | 53.3% | 112.2% | 93.2% |
| 1h | 113 | 99.1% | 69.9% | 53.1% | 140.1% | 89.4% |
| 1D | 24 | 100.0% | 79.2% | 70.8% | 160.5% | 58.3% |
| overall | 3778 | 99.8% | 70.1% | 51.9% | — | 94.6% |

**Stop = intrabar touch** (a real resting stop-loss order, wick or not) is
meaningfully harsher: overall reached-≥100% drops to 46.5%, reached-≥50% to
65.4%. *"Eventually stopped out" is not mutually exclusive with reaching the
target first — MFE is measured only up to the stop point, so most setups hit
their target before eventually seeing a level violation, given enough time.

Known caveats: this is a pseudo-backtest — no transaction costs, no realistic
slippage, no out-of-sample/walk-forward split. Treat the numbers as
directional, not as a promise.

## Live deployment (EC2, `ubuntu@ec2-65-2-118-242.ap-south-1.compute.amazonaws.com`)

- `/home/ubuntu/spike` — systemd service (`spike.service`), Type=simple,
  Restart=on-failure, matching the box's existing convention (mirrors
  `nifty-spring.service`).
- Originally reused a sibling project's (`orb`) Kite session token and local
  1-min CSVs read-only, since `orb` already existed on the box. Since `orb`
  is being decommissioned, **data import was made fully self-contained**:
  `spike/data_feed.py` backfills and maintains its own 1-min (~75 days) and
  native daily (~450 calendar days, ~300+ trading days) history entirely via
  `kite.historical_data()`, cached under `data_cache/`. Nothing outside
  spike's own directory is read anymore.
- Kite *session* reuse (reading another project's token file) is kept as a
  pure startup optimization, not a dependency — `kite_session.py` falls back
  to an independent TOTP login automatically if no reusable token is found.
- Universe: derived fresh each run from `kite.instruments("NFO")` FUT rows
  (~213 names). 5 are index futures (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY,
  NIFTYNXT50) with no underlying equity instrument token — correctly skipped.
  Considered adding them via their spot index data, but Kite reports **zero
  volume** for spot indices (verified directly), which would make the
  volume-spike/decline conditions permanently false — decided to leave them
  excluded rather than special-case the detector for 5 of 213 tickers.
- Alerts: Discord webhook, matching the box's existing convention (other
  projects there also use Discord, not Telegram).
- GitHub: `github.com/anantaam/spike`, pushed via a one-time manually-entered
  PAT (credential then cached server-side so subsequent commits/pushes don't
  need it again). Two PATs got pasted into chat during setup and were never
  used for exactly that reason — both should be treated as compromised and
  revoked if not already done.
- Timeframes live: `5min,15min,1h,1D`. 1D uses calendar-day scheduling (fires
  once, ~10min after the 15:30 IST close, within a same-day catch-up window)
  rather than the generic minutes-since-midnight boundary logic the other
  timeframes use.
- Went live 2026-08-03. Two setups (IDFCFIRSTB bullish — flagged on both
  5min and 15min at the same 10:45 retest, one underlying event not two —
  and JSWSTEEL bullish, 09:50 retest) were found that day via a manual
  ad-hoc scan, **not** by the live service itself — see the bugs below;
  the service wasn't actually completing scan cycles yet when those fired.

## Production bugs found and fixed (2026-08-03, same day as going live)

A "is it still running?" status check surfaced two bugs, found in sequence:

1. **Silent hang from missing network timeouts.** `kiteconnect`/`requests`
   default to no timeout at all. The service looked "running" (process
   alive, `active (running)` in systemd) for ~2 hours but had done almost
   nothing — confirmed via `ps`: only ~5.5s of CPU time against 2+ hours of
   wall-clock uptime, plus a `CLOSE-WAIT` socket stuck against Kite's API
   (`ss -tnp`). One stalled connection blocks this single-threaded loop
   forever, with no exception and no log line — indistinguishable from
   "nothing to report" without checking process/socket state directly.
   Fixed: explicit `timeout=20s` on every `KiteConnect(...)` construction and
   every raw `requests` call in the independent-login flow
   (`spike/kite_session.py`, `KITE_TIMEOUT_SECONDS` in `spike/config.py`).
   Also added a heartbeat log every 5 minutes (`spike/main.py`) so a future
   stall shows up as "heartbeats stopped" instead of ambiguous silence.
2. **Off-by-one in the candle-boundary math — the more fundamental bug.**
   Even after the timeout fix, no scan cycle ever fired. `_boundary()` floors
   `now` to the timeframe grid, which gives the *close of the most recently
   completed candle* (since one candle's close equals the next one's open).
   The code was adding `tf_minutes` on top of that, which instead computes
   the close of the *currently-forming* candle — a time always in the
   future relative to `now`, so the readiness check could never pass. This
   means the live service most likely never completed a real scan cycle at
   any point before the fix, timeout bug or not. Fixed by using `_boundary`'s
   return value directly as `close_time`, no addition.

Verified fixed same day: post-fix, all three intraday timeframes correctly
scanned their most-recently-closed candle exactly once
(`scanned tf=5min at 13:40:00`, `tf=15min at 13:30:00`, `tf=1h at 13:00:00`).

## Position sizing / trade management (framework discussed, not yet implemented)

Not personalized financial advice — a systematic framework derived from the
backtest stats above, for whatever capital/risk tolerance is actually used.

Core insight: reached-≥50% (70%) notably exceeds reached-≥100% (52%), and
eventually-stopped-out is very high (~94-95% intraday) — arguing for
**staggered exits over one-shot**, since holding full size for a single
target either exits too early on setups that stall between 50-100%, or gives
back gains on the ~45-54% that never reach 100% at all.

Suggested framework (R = entry-to-stop distance, sized off the *touch-stop*
numbers, not close-stop, since a real resting stop is stricter):
- Scale out ~40% at the 50%-of-extension mark (~70% probability), ~40% at
  the 100%-of-extension mark (~52%), ~20% runner with a trailing stop
  (median MFE 100-160%+ means the tail is worth capturing, but which trade
  is the tail isn't knowable in advance).
- Move stop to breakeven once the first tranche is banked.
- Per-trade risk as a small fixed fraction of capital (a common systematic
  convention is 0.25-1%, but that's a personal call).
- Cap *aggregate* concurrent risk across open positions — F&O breakout
  signals on the same day often correlate with a broader market move rather
  than being independent, as illustrated by two same-direction signals
  firing on the same first live day.
- Don't double-size a signal that fires on multiple timeframes for the same
  underlying event (e.g. IDFCFIRSTB 5min+15min at the same retest timestamp)
  — that's one trade, not two.

## What this is not (yet)

No order placement, no automated position sizing, no risk controls in code.
Deliberate: alert-only lets signal quality get validated against real market
conditions before trusting any capital to automation.

## Elliott wave context on order-block alerts (2026-09-01)

Every OB alert now carries three extra facts, and a chart. Tagging only — the
wave count never suppresses an alert, it only annotates one. If no count is
found the alert fires exactly as before, graded `?`.

**What gets tagged.** Which wave the retouch landed in (1-5), whether the
block pushes *with* the current leg or *against* it, and where in that leg it
sat (early / mid-leg / termination; wave 5 is reported as `forming`, because
it has no known end and guessing one would be fabrication).

**Why only those.** They were the only fields that moved expectancy in the
local payoff study (`wavelab/payoff.py`, F&O daily, 5.5y): direction agreement
flips the sign in *every* wave — wave 3 agrees +0.31R vs opposes -0.40R, wave
4 +0.64 vs -0.26, wave 5 +1.40 (n=15) vs -0.04 at a 3R target. Best bucket is
agrees + termination (median MFE 2.46R); worst is opposes + termination
(0.50R). OB alone across 838k setups was -0.01R — i.e. nothing.

**Grades** are a triage label for skimming the feed, not a position size:
`A` agrees and in wave 4/5, `B` agrees, `C` opposes, `?` no count.

**Opposing setups flip the named side.** If a bullish block is retouched
inside a *down* leg, the alert names the SHORT-on-break trade, not the long
bounce — the failed-auction case. Caveat, stated in the alert's own terms and
worth repeating here: the payoff numbers above are all measured on the
**bounce**. The break trade has never been measured. Grade C entries are
geometry, not a validated edge.

**Wave-context timeframe, per segment** (`config.WAVE_CONTEXT_TF`, override
with `SPIKE_WAVE_CONTEXT_TF="crypto=12h"`):

| segment | tf | why |
|---|---|---|
| equity | 1D | matches the prototype the payoff study ran on |
| crypto | 1D | same calendar degree; Binance serves 999 daily bars, no auth |
| commodity | 4h | MCX contracts roll monthly — a front-month contract has only ~90-120 daily bars and a third printed **zero volume** before it became liquid, so a 100-140 *daily* count would be fitted to untraded bars. 4h off the 1-min cache gives ~180-280 real bars inside one contract's liquid life. Smaller degree than the equity counts; read accordingly. |

Coverage measured on the live universe the day it shipped: equity 19/40
names, commodity 4/16 contracts, crypto 3/10 pairs. Crypto on 12h would have
given 5/10 and 4h 4/10 — deliberately **not** chased. Every timeframe can
produce a 100-140 bar span by construction, so coverage cannot select the
timeframe; the degree actually being traded does. Tuning until more counts
appear is the same overfit as sweeping pivot depth, which produced nothing.

**Cost.** The pivot search is ~1-2s per symbol, so it runs lazily — only for
symbols that actually raised an OB event — and is memoised on the last
context-bar timestamp, i.e. once per symbol per day. Charts add matplotlib
(Agg, headless) and ~0.3s per alert.

**Failure behaviour.** Every layer degrades rather than blocks: no count →
untagged alert; chart render fails → text-only alert; Discord rejects the
multipart upload → automatic retry as plain text. An alert is never lost for
want of a picture.

Files: `spike/wave.py` (ported from `wavelab/fracpivots.py` + `forming.py`),
`spike/wavechart.py`, `_tag_wave()` in `main.py`/`crypto_main.py`,
`_wave_lines()` in `notifier.py`. Self-check: `test_wave.py` (needs
`../wavelab/data`), server smoke test: `smoke_wave.py`.
