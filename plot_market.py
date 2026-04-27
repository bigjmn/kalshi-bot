"""
Plot best_yes_ask, true YES probability, and BTC price for a given market ticker.

Reads market metadata (strike, working_sd, book_states path) from
data/markets_ref/market_index.json. BTC prices come from the shared
data/markets_ref/btc_reference.jsonl, sliced to the market window.

Usage:
    uv run python plot_market.py <market_ticker>
    uv run python plot_market.py KXBTC15M-26APR241830-30
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from true_prob import yes_probability

matplotlib.use("macosx")

BASE = Path("data/markets_ref")
INDEX_FILE = BASE / "market_index.json"
BTC_FILE = BASE / "btc_reference.jsonl"


# ── data loaders ────────────────────────────────────────────────────────────

def load_index() -> list[dict]:
    with INDEX_FILE.open() as f:
        return json.load(f)


def find_market(ticker: str) -> dict:
    for entry in load_index():
        if entry["ticker"] == ticker:
            return entry
    raise ValueError(f"Ticker {ticker!r} not found in {INDEX_FILE}")


def iso_to_ms(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def load_book_states(path: Path) -> tuple[list[float], list[float]]:
    ts, ask = [], []
    with open(path) as f:
        for line in f:
            obj = json.loads(line)
            v = obj.get("best_yes_ask")
            if v is not None:
                ts.append(obj["ts_ms_local"] / 1000)
                ask.append(float(v))
    return ts, ask


def load_btc_in_window(open_ms: float, close_ms: float) -> tuple[list[float], list[float]]:
    """Load BTC records in [open_ms - 30s, close_ms + 30s]."""
    lo, hi = open_ms - 30_000, close_ms + 30_000
    ts, price = [], []
    with BTC_FILE.open() as f:
        for line in f:
            obj = json.loads(line)
            t = obj.get("ts_ms_local") or obj.get("timestamp")
            if t is None:
                continue
            if lo <= t <= hi:
                ts.append(t / 1000)
                price.append(obj["last_price"][0])
    # sort by time
    paired = sorted(zip(ts, price))
    if not paired:
        return [], []
    ts, price = zip(*paired)
    return list(ts), list(price)


# ── true probability series ──────────────────────────────────────────────────

def _trapezoid(pts: list[tuple[float, float]], t_lo: float, t_hi: float) -> float:
    """Trapezoid integral of pts (sorted t-ascending) clipped to [t_lo, t_hi]."""
    A = 0.0
    for i in range(len(pts) - 1):
        s0, x0 = pts[i]
        s1, x1 = pts[i + 1]
        # clip to [t_lo, t_hi]
        a, b = max(s0, t_lo), min(s1, t_hi)
        if a >= b:
            continue
        # linear interpolation for x at a and b
        frac0 = (a - s0) / (s1 - s0)
        frac1 = (b - s0) / (s1 - s0)
        xa = x0 + frac0 * (x1 - x0)
        xb = x0 + frac1 * (x1 - x0)
        A += (xa + xb) / 2.0 * (b - a)
    return A


def compute_true_probs(
    btc_ts: list[float],
    btc_price: list[float],
    T_ms: float,
    K: float,
    sigma: float,
    window_s: float = 60.0,
) -> list[float]:
    """
    Compute yes_probability at each BTC timestamp.

    Bug fixes vs. naive implementation:
    1. The trapezoid for A_t is anchored at window_open_sec using the last
       known BTC price before the window — without this, A_t = 0 on the first
       tick inside the window, giving m = X_t * tau/W << X_t (false dip to 0).
    2. Post-close BTC prices are not added to window_pts — without this,
       they inflate A_t and produce a false 1.0 after close.
    """
    T_sec = T_ms / 1000.0
    window_open_sec = T_sec - window_s

    probs = []
    # All BTC samples from window_open onward; anchored at window_open_sec.
    window_pts: list[tuple[float, float]] = []
    last_price: float | None = None   # last price seen before the window
    settled_prob: float | None = None  # cached deterministic answer after T

    for t_sec, price in zip(btc_ts, btc_price):
        t_ms = t_sec * 1000.0
        tau = T_sec - t_sec

        # ── before settlement window ────────────────────────────────────────
        if tau >= window_s:
            last_price = price
            probs.append(yes_probability(t_ms, T_ms, price, K, sigma, window_s=window_s))
            continue

        # ── after market close ──────────────────────────────────────────────
        if tau <= 0.0:
            if settled_prob is None:
                # Finalize A_t only up to T_sec (don't include post-close prices)
                A_final = _trapezoid(window_pts, window_open_sec, T_sec)
                settled_prob = 1.0 if (A_final / window_s) > K else 0.0
            probs.append(settled_prob)
            continue

        # ── inside settlement window (0 < tau < window_s) ──────────────────
        if not window_pts:
            # Anchor integral at window_open_sec with the last known price.
            # Without this, A_t = 0 for the first tick and m = X_t*tau/W << X_t.
            anchor_price = last_price if last_price is not None else price
            window_pts.append((window_open_sec, anchor_price))

        window_pts.append((t_sec, price))

        A_t = _trapezoid(window_pts, window_open_sec, t_sec)
        probs.append(yes_probability(t_ms, T_ms, price, K, sigma, A_t=A_t, window_s=window_s))

    return probs


# ── plot ─────────────────────────────────────────────────────────────────────

def to_dt(ts_sec: float) -> datetime:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: plot_market.py <market_ticker>")
        sys.exit(1)

    ticker = sys.argv[1]
    mkt = find_market(ticker)

    K = mkt["floor_strike"]
    sigma = mkt["working_sd"]
    open_ms = iso_to_ms(mkt["open_time"])
    close_ms = iso_to_ms(mkt["close_time"])
    T_ms = close_ms  # settlement at market close

    print(f"Market : {ticker}")
    print(f"Strike : {K}")
    print(f"Sigma  : {sigma}")
    print(f"Window : {mkt['open_time']} → {mkt['close_time']}")

    book_ts, book_ask = load_book_states(Path(mkt["book_states_path"]))
    btc_ts, btc_price = load_btc_in_window(open_ms, close_ms)

    if not btc_price:
        print("No BTC data in window.")
        sys.exit(1)

    true_probs = compute_true_probs(btc_ts, btc_price, T_ms, K, sigma)

    book_dt = mdates.date2num([to_dt(t) for t in book_ts])
    btc_dt = mdates.date2num([to_dt(t) for t in btc_ts])

    # ── figure ───────────────────────────────────────────────────────────────
    fig, ax1 = plt.subplots(figsize=(15, 6))
    fig.patch.set_facecolor("#0f1117")
    ax1.set_facecolor("#0f1117")

    # Left axis: BTC price
    color_btc = "#f0a500"
    ax1.plot(btc_dt, btc_price, color=color_btc, linewidth=1.1, label="BTC price (USD)", zorder=2)
    ax1.axhline(y=K, color="#ff4444", linestyle="--", linewidth=1.2, alpha=0.85, label=f"Strike ${K:,.2f}", zorder=3)
    ax1.set_ylabel("BTC Price (USD)", color=color_btc)
    ax1.tick_params(axis="y", labelcolor=color_btc)
    ax1.tick_params(axis="x", colors="#aaaaaa")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    # Right axis: best_yes_ask + true probability (both 0–1)
    ax2 = ax1.twinx()
    color_ask = "#4fc3f7"
    color_prob = "#69f0ae"

    ax2.plot(book_dt, book_ask, color=color_ask, linewidth=0.9, alpha=0.85, label="Best YES ask")
    ax2.plot(btc_dt, true_probs, color=color_prob, linewidth=1.2, label="True prob (ABM)")
    ax2.set_ylabel("Probability / Ask", color="#cccccc")
    ax2.tick_params(axis="y", labelcolor="#cccccc")
    ax2.set_ylim(-0.05, 1.05)
    ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.2f}"))

    # X axis
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=timezone.utc))
    fig.autofmt_xdate(rotation=45)

    # Grid & legend
    ax1.grid(True, alpha=0.12, color="#555555")
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(
        lines1 + lines2,
        labels1 + labels2,
        loc="upper left",
        facecolor="#1e2130",
        edgecolor="#444",
        labelcolor="white",
        fontsize=9,
    )

    for spine in [*ax1.spines.values(), *ax2.spines.values()]:
        spine.set_edgecolor("#333")

    plt.title(
        f"{ticker}  |  strike ${K:,.2f}  |  σ={sigma:.2f} $/√s",
        color="white",
        pad=12,
    )
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
