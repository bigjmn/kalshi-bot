#!/usr/bin/env python3
"""
Scan data/markets_ref for UUID-named subfolders, look up each market's
strike price from the Kalshi API, and write a sorted index to
data/markets_ref/market_index.json.
"""

import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path("data/markets_ref")
OUT = BASE / "market_index.json"
API_BASE = "https://api.elections.kalshi.com/trade-api/v2/markets"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def get_market_ticker(market_id_dir: Path) -> str | None:
    bs = market_id_dir / "book_states.jsonl"
    if not bs.exists():
        return None
    with bs.open() as f:
        for line in f:
            obj = json.loads(line)
            ticker = obj.get("market_ticker")
            if ticker:
                return ticker
    return None


def fetch_market(ticker: str, retries: int = 4, backoff: float = 2.0) -> dict | None:
    url = f"{API_BASE}/{ticker}"
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r.json().get("market", r.json())
        except Exception as e:
            if attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                print(f"  retry {attempt+1}/{retries-1} for {ticker} after {wait:.0f}s ({e})")
                time.sleep(wait)
            else:
                print(f"  ERROR fetching {ticker}: {e}")
    return None


def iso_to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def btc_sigma_in_window(btc_file: Path, open_ms: int, close_ms: int) -> float:
    """
    Estimate ABM sigma in USD/sqrt(second) for BTC prices in [open_ms, close_ms].
    sigma = std(price_diffs) / sqrt(mean_dt_seconds)
    Returns 0.0 if fewer than 2 records found.
    """
    records: list[tuple[int, float]] = []
    with btc_file.open() as f:
        for line in f:
            obj = json.loads(line)
            t = obj.get("ts_ms_local") or obj.get("timestamp")
            if t is None:
                continue
            if open_ms <= t <= close_ms:
                records.append((t, obj["last_price"][0]))
    records.sort()
    if len(records) < 2:
        return 0.0
    timestamps = [r[0] for r in records]
    prices = [r[1] for r in records]
    diffs = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    mean_dt_s = (timestamps[-1] - timestamps[0]) / 1000.0 / (len(timestamps) - 1)
    if mean_dt_s <= 0.0:
        return 0.0
    n = len(diffs)
    mean_d = sum(diffs) / n
    std_diffs = math.sqrt(sum((d - mean_d) ** 2 for d in diffs) / (n - 1))
    return round(std_diffs / math.sqrt(mean_dt_s), 6)


def main() -> None:
    uuid_dirs = sorted(
        d for d in BASE.iterdir()
        if d.is_dir() and UUID_RE.match(d.name)
    )
    print(f"Found {len(uuid_dirs)} market-id subfolders")

    entries = []
    for d in uuid_dirs:
        market_id = d.name
        ticker = get_market_ticker(d)
        if not ticker:
            print(f"  {market_id}: no ticker found, skipping")
            continue

        print(f"  {market_id} -> {ticker}")
        mkt = fetch_market(ticker)
        if mkt is None:
            continue

        entries.append(
            {
                "ticker": ticker,
                "market_id": market_id,
                "floor_strike": mkt.get("floor_strike"),
                "open_time": mkt.get("open_time"),
                "close_time": mkt.get("close_time"),
                "status": mkt.get("status"),
                "expiration_value": mkt.get("expiration_value"),
                "book_states_path": str(d / "book_states.jsonl"),
                "events_path": str(d / "events.jsonl"),
            }
        )
        time.sleep(0.1)  # be polite to the API

    # Sort by open_time ascending
    entries.sort(key=lambda e: e.get("open_time") or "")

    # Compute working_sd: std dev of BTC prices during the PREVIOUS market's window.
    btc_file = BASE / "btc_reference.jsonl"
    for i, entry in enumerate(entries):
        if i == 0:
            entry["working_sd"] = 8.0
        else:
            prev = entries[i - 1]
            open_ms = iso_to_ms(prev["open_time"])
            close_ms = iso_to_ms(prev["close_time"])
            sd = btc_sigma_in_window(btc_file, open_ms, close_ms)
            entry["working_sd"] = sd
            print(f"  {entry['ticker']}: prev window sigma={sd:.6f} USD/sqrt(s)")

    OUT.write_text(json.dumps(entries, indent=2))
    print(f"\nWrote {len(entries)} entries to {OUT}")


if __name__ == "__main__":
    main()
