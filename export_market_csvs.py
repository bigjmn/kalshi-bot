"""
Export one CSV per market to data/markets_ref/csvs/.

Columns:
  timestamp_utc  — ISO-8601 UTC
  ts_ms          — Unix milliseconds
  btc_price      — BTC last price (from btc_reference.jsonl)
  true_prob      — ABM yes-probability at that timestamp
  best_yes_ask   — Kalshi order-book best YES ask (0–1)
  strike         — floor_strike for the market (constant)
  sigma          — working_sd used (USD/sqrt(s), constant)

BTC and book-ask ticks arrive at different timestamps; rows are the
union of both series, with blanks where a source has no observation.

Usage:
    uv run python export_market_csvs.py
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

from plot_market import (
    load_index,
    load_book_states,
    load_btc_in_window,
    compute_true_probs,
    iso_to_ms,
    to_dt,
)

OUT_DIR = Path("data/markets_ref/csvs")


def fmt_ts(ts_sec: float) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).isoformat()


def export_market(mkt: dict, out_dir: Path) -> Path:
    ticker   = mkt["ticker"]
    K        = mkt["floor_strike"]
    sigma    = mkt["working_sd"]
    open_ms  = iso_to_ms(mkt["open_time"])
    close_ms = iso_to_ms(mkt["close_time"])

    btc_ts, btc_price = load_btc_in_window(open_ms, close_ms)
    book_ts, book_ask = load_book_states(Path(mkt["book_states_path"]))

    true_probs = (
        compute_true_probs(btc_ts, btc_price, close_ms, K, sigma)
        if btc_price else []
    )

    # Build lookup dicts keyed by ts_ms (rounded to nearest ms)
    btc_rows:  dict[int, tuple[float, float]] = {}  # ts_ms -> (price, prob)
    for ts, price, prob in zip(btc_ts, btc_price, true_probs):
        btc_rows[round(ts * 1000)] = (price, prob)

    ask_rows: dict[int, float] = {}  # ts_ms -> ask
    for ts, ask in zip(book_ts, book_ask):
        ask_rows[round(ts * 1000)] = ask

    all_ts_ms = sorted(set(btc_rows) | set(ask_rows))

    out_path = out_dir / f"{ticker}.csv"
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp_utc", "ts_ms",
            "btc_price", "true_prob", "best_yes_ask",
            "strike", "sigma",
        ])
        for ts_ms in all_ts_ms:
            ts_sec = ts_ms / 1000.0
            btc_p, prob = btc_rows[ts_ms] if ts_ms in btc_rows else ("", "")
            ask         = ask_rows[ts_ms]  if ts_ms in ask_rows  else ""
            writer.writerow([
                fmt_ts(ts_sec), ts_ms,
                btc_p, prob, ask,
                K, sigma,
            ])

    return out_path


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    markets = load_index()
    for mkt in markets:
        path = export_market(mkt, OUT_DIR)
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
