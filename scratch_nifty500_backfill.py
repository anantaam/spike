"""
Backfill 1-min history for Nifty 500 constituents not already covered by the
F&O universe, so the win-rate and opening/mid-session findings can be tested
on a broader universe. Runs on the server (reuses spike's live Kite session),
writes one CSV per ticker; a separate step imports these into the local
postgres DB used by the historical validation scripts.

Deliberately more conservative on rate limiting (0.45s vs spike's own 0.35s)
since this runs alongside the live scanner's own API usage on the same
account/session.
"""
import sys
import time

import pandas as pd

sys.path.insert(0, "/home/ubuntu/spike")
from spike import kite_session  # noqa: E402

TICKER_FILE = "/home/ubuntu/spike/missing_tickers.txt"
OUT_DIR = "/home/ubuntu/spike/nifty500_backfill"
CHUNK_DAYS = 55
START_DATE = pd.Timestamp("2020-01-01")
DELAY = 0.45

import os
os.makedirs(OUT_DIR, exist_ok=True)


def fetch_ticker(kite, token, ticker):
    to_dt = pd.Timestamp.now()
    frames = []
    cur_to = to_dt
    while cur_to > START_DATE:
        cur_from = max(START_DATE, cur_to - pd.Timedelta(days=CHUNK_DAYS))
        rows = None
        for attempt in range(3):
            try:
                rows = kite.historical_data(token, cur_from, cur_to, "minute")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"    chunk failed permanently ({cur_from.date()} -> {cur_to.date()}): {e}")
                else:
                    time.sleep(2)
        if rows:
            frames.append(pd.DataFrame(rows))
        time.sleep(DELAY)
        cur_to = cur_from
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset="date").sort_values("date")
    return df


def main():
    with open(TICKER_FILE) as f:
        tickers = [line.strip() for line in f if line.strip()]
    print(f"{len(tickers)} tickers to backfill")

    kite = kite_session.login_or_reuse()
    print("Loading NSE instrument tokens...")
    token_map = {}
    for row in kite.instruments("NSE"):
        sym = row.get("tradingsymbol")
        if sym:
            token_map[sym] = row["instrument_token"]
    print(f"{len(token_map)} NSE instruments loaded")

    done = 0
    skipped = []
    t0 = time.time()
    for i, ticker in enumerate(tickers, 1):
        out_path = f"{OUT_DIR}/{ticker}_1min.csv"
        if os.path.exists(out_path):
            done += 1
            continue
        token = token_map.get(ticker)
        if token is None:
            print(f"  [{i}/{len(tickers)}] {ticker}: no instrument token found, skipping")
            skipped.append(ticker)
            continue
        df = fetch_ticker(kite, token, ticker)
        if df is None or df.empty:
            print(f"  [{i}/{len(tickers)}] {ticker}: no data returned, skipping")
            skipped.append(ticker)
            continue
        df.to_csv(out_path, index=False)
        done += 1
        if i % 10 == 0:
            elapsed = time.time() - t0
            print(f"  [{i}/{len(tickers)}] done, {elapsed:.0f}s elapsed, "
                  f"~{elapsed/i*len(tickers):.0f}s total est. ({ticker}: {len(df)} rows)")

    print(f"\nDone. {done} tickers backfilled, {len(skipped)} skipped: {skipped}")


if __name__ == "__main__":
    main()
