"""
Historical validation for the 9 MCX commodities, same methodology as the
equity validation (backtest_pauseonly.py): production detector (imported
directly from spike.detector, not a copy) finds clean candidates, then a
pseudo-backtest tracks forward MFE against the actual extension-leg size,
bucketed at 50%/100%, under both close-confirmed and touch stop conventions.

Two separate runs, for a real constraint (not a choice): Kite's continuous=
True historical data only works for interval='day' on MCX -- there is no
continuous/stitched intraday mode, and a single contract's own non-continuous
history only spans its few months of listed life (verified: CRUDEOIL's
current contract starts trading ~2026-04-22, expires 2026-08-19).

  1. DAILY: continuous 'day' data, ~5 years, one call per ticker -- same
     depth as the equity validation, statistically meaningful.
  2. INTRADAY (5min/15min/1h): non-continuous 1-min data for whichever
     contract is CURRENTLY active per name, chunked in 55-day windows --
     capped at that single contract's own trading life (a few months).
     This is what's actually live, but the sample is necessarily much
     thinner than the daily run or the equity validation.

Must run on the server (spike/.env credentials + live Kite session live
there only).
"""
import time

import numpy as np
import pandas as pd

from spike import config, data_feed, detector, kite_session

INTRADAY_TFS = ["5min", "15min", "1h"]
INTRADAY_LOOKBACK_DAYS = 200   # generous upper bound; Kite just returns less if the contract is younger
DAILY_LOOKBACK_DAYS = 1800     # ~5 years, matches the equity validation window


def fetch_chunked(kite, token, from_dt, to_dt, interval, chunk_days):
    frames = []
    cur_to = to_dt
    while cur_to > from_dt:
        cur_from = max(from_dt, cur_to - pd.Timedelta(days=chunk_days))
        try:
            rows = kite.historical_data(token, cur_from, cur_to, interval)
        except Exception as exc:
            print(f"    chunk failed ({cur_from.date()} -> {cur_to.date()}): {exc}")
            rows = []
        if rows:
            frames.append(pd.DataFrame(rows))
        time.sleep(config.HISTORICAL_REQUEST_DELAY_SECONDS)
        cur_to = cur_from
    if not frames:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="date").sort_values("date")
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    return df.set_index("date")[["open", "high", "low", "close", "volume"]]


def backtest_group(df, rows):
    """Exact same methodology as backtest_pauseonly.py's process_group."""
    idx = df.index
    h, l, c = df.high.values, df.low.values, df.close.values
    n = len(df)
    out = []
    for r in rows:
        te = idx.searchsorted(pd.Timestamp(r["thrust_end"]))
        rv = idx.searchsorted(pd.Timestamp(r["reversion_ts"]))
        if te >= n or rv >= n or rv <= te:
            continue
        ext_seg_hi, ext_seg_lo = h[te:rv], l[te:rv]
        if len(ext_seg_hi) == 0:
            continue
        bull = r["direction"] == "bullish"
        extension_size = (ext_seg_hi.max() - r["zone_hi"]) if bull else (r["zone_lo"] - ext_seg_lo.min())
        if extension_size <= 0:
            continue
        entry = l[rv] if bull else h[rv]
        fwd_h, fwd_l, fwd_c = h[rv + 1:], l[rv + 1:], c[rv + 1:]
        if len(fwd_c) == 0:
            continue
        if bull:
            bad_close, bad_touch = fwd_c < r["zone_lo"], fwd_l <= r["zone_lo"]
        else:
            bad_close, bad_touch = fwd_c > r["zone_hi"], fwd_h >= r["zone_hi"]

        def mfe_for(bad):
            stopped = bool(bad.any())
            cutoff = int(np.argmax(bad)) + 1 if stopped else len(fwd_c)
            mfe = (fwd_h[:cutoff].max() - entry) if bull else (entry - fwd_l[:cutoff].min())
            return mfe, stopped

        mfe_close, stopped_close = mfe_for(bad_close)
        mfe_touch, stopped_touch = mfe_for(bad_touch)
        pct_close, pct_touch = mfe_close / extension_size, mfe_touch / extension_size
        out.append(dict(
            ticker=r["ticker"], tf=r["tf"], direction=r["direction"],
            mfe_pct_close_stop=pct_close, stopped_out_close=stopped_close,
            moved_favorably_close=pct_close > 0, reached_50_close=pct_close >= 0.5,
            reached_100_close=pct_close >= 1.0,
            mfe_pct_touch_stop=pct_touch, stopped_out_touch=stopped_touch,
            moved_favorably_touch=pct_touch > 0, reached_50_touch=pct_touch >= 0.5,
            reached_100_touch=pct_touch >= 1.0,
        ))
    return out


def report(label, results):
    if not results:
        print(f"\n=== {label}: 0 candidates ===")
        return
    out = pd.DataFrame(results)
    print(f"\n=== {label}: {len(out)} candidates ===")
    for variant in ["close", "touch"]:
        summary = out.groupby("tf").agg(
            n=(f"mfe_pct_{variant}_stop", "size"),
            moved_favorably_pct=(f"moved_favorably_{variant}", "mean"),
            reached_50_pct=(f"reached_50_{variant}", "mean"),
            reached_100_pct=(f"reached_100_{variant}", "mean"),
            stopped_out_pct=(f"stopped_out_{variant}", "mean"),
            median_mfe_pct=(f"mfe_pct_{variant}_stop", "median"),
        )
        for col in summary.columns:
            if col != "n":
                summary[col] = (summary[col] * 100).round(1)
        print(f"-- stop={variant} --")
        print(summary.to_string())
    out.to_csv(f"commodity_backtest_{label.lower()}.csv", index=False)


def main():
    kite = kite_session.login_or_reuse()
    commodity_map = data_feed.discover_commodity_contracts(kite)
    print("active contracts:", commodity_map)
    to_dt = pd.Timestamp.now()

    # ---- 1. DAILY: continuous, ~5 years ----
    daily_results = []
    for name, sym in commodity_map.items():
        token = data_feed._instrument_token_cache[sym]
        try:
            rows = kite.historical_data(token, to_dt - pd.Timedelta(days=DAILY_LOOKBACK_DAYS), to_dt,
                                          "day", continuous=True)
        except Exception as exc:
            print(f"  {sym}: daily continuous fetch failed ({exc})")
            continue
        if not rows:
            continue
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df.set_index("date")[["open", "high", "low", "close", "volume"]]
        print(f"  {sym}: {len(df)} daily bars ({df.index.min().date()} -> {df.index.max().date()})")
        setups = detector.find_setups(df, sym, "1D")
        clean = detector.clean(setups)
        print(f"    candidates raw={len(setups)} clean={len(clean)}")
        daily_results.extend(backtest_group(df, clean.to_dict("records")))
        time.sleep(config.HISTORICAL_REQUEST_DELAY_SECONDS)

    report("DAILY", daily_results)

    # ---- 2. INTRADAY: non-continuous, current contract's own life only ----
    intraday_results = []
    for name, sym in commodity_map.items():
        token = data_feed._instrument_token_cache[sym]
        df1 = fetch_chunked(kite, token, to_dt - pd.Timedelta(days=INTRADAY_LOOKBACK_DAYS), to_dt,
                             "minute", chunk_days=55)
        if df1.empty:
            print(f"  {sym}: no 1-min history available")
            continue
        print(f"  {sym}: {len(df1)} 1-min bars ({df1.index.min()} -> {df1.index.max()})")
        for tf in INTRADAY_TFS:
            tdf = data_feed.resample(df1, tf)
            setups = detector.find_setups(tdf, sym, tf)
            clean = detector.clean(setups)
            print(f"    {tf}: candidates raw={len(setups)} clean={len(clean)}")
            intraday_results.extend(backtest_group(tdf, clean.to_dict("records")))

    report("INTRADAY", intraday_results)


if __name__ == "__main__":
    main()
