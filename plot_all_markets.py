"""
Plot all markets from market_index.json as a subplot grid.
Each subplot shares the same layout as plot_market.py:
  left axis  — BTC price (amber) + strike line (red dashed)
  right axis  — best YES ask (blue) + true probability (green)

Usage:
    uv run python plot_all_markets.py
"""

import math
from datetime import timezone
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from plot_market import (
    load_index,
    load_book_states,
    load_btc_in_window,
    compute_true_probs,
    iso_to_ms,
    to_dt,
)

matplotlib.use("macosx")

NCOLS = 3


def main() -> None:
    markets = load_index()
    n = len(markets)
    nrows = math.ceil(n / NCOLS)

    fig, axes = plt.subplots(
        nrows, NCOLS,
        figsize=(7 * NCOLS, 4 * nrows),
        squeeze=False,
    )
    fig.patch.set_facecolor("#0f1117")

    color_btc  = "#f0a500"
    color_ask  = "#4fc3f7"
    color_prob = "#69f0ae"
    color_strike = "#ff4444"

    for idx, mkt in enumerate(markets):
        row, col = divmod(idx, NCOLS)
        ax1 = axes[row][col]
        ax2 = ax1.twinx()

        ticker   = mkt["ticker"]
        K        = mkt["floor_strike"]
        sigma    = mkt["working_sd"]
        open_ms  = iso_to_ms(mkt["open_time"])
        close_ms = iso_to_ms(mkt["close_time"])

        book_ts, book_ask = load_book_states(Path(mkt["book_states_path"]))
        btc_ts, btc_price = load_btc_in_window(open_ms, close_ms)

        book_dt = mdates.date2num([to_dt(t) for t in book_ts])
        btc_dt  = mdates.date2num([to_dt(t) for t in btc_ts])

        # BTC price + strike
        ax1.set_facecolor("#0f1117")
        if btc_price:
            true_probs = compute_true_probs(btc_ts, btc_price, close_ms, K, sigma)
            ax1.plot(btc_dt, btc_price, color=color_btc, linewidth=0.9, zorder=2)
            ax2.plot(btc_dt, true_probs, color=color_prob, linewidth=1.0, label="True prob")
        ax1.axhline(y=K, color=color_strike, linestyle="--", linewidth=0.9, alpha=0.8, zorder=3)

        # Book ask
        if book_ask:
            ax2.plot(book_dt, book_ask, color=color_ask, linewidth=0.7, alpha=0.8, label="Best YES ask")

        # Axes styling
        ax1.set_ylabel("BTC (USD)", color=color_btc, fontsize=7)
        ax1.tick_params(axis="y", labelcolor=color_btc, labelsize=6)
        ax1.tick_params(axis="x", colors="#aaaaaa", labelsize=6)
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax1.grid(True, alpha=0.10, color="#555555")

        ax2.set_ylim(-0.05, 1.05)
        ax2.set_ylabel("Prob / Ask", color="#cccccc", fontsize=7)
        ax2.tick_params(axis="y", labelcolor="#cccccc", labelsize=6)
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.1f}"))

        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone.utc))
        ax1.tick_params(axis="x", rotation=30)

        for spine in [*ax1.spines.values(), *ax2.spines.values()]:
            spine.set_edgecolor("#333")

        # Short ticker label (strip common prefix)
        short = ticker.replace("KXBTC15M-26APR24", "")
        expiry = mkt.get("expiration_value", "")
        result = "YES" if float(expiry) > K else "NO" if expiry else "?"
        color_result = color_prob if result == "YES" else color_strike if result == "NO" else "#aaaaaa"
        ax1.set_title(
            f"{short}  K=${K:,.0f}  [{result}]",
            color=color_result, fontsize=8, pad=4,
        )

    # Hide unused subplots
    for idx in range(n, nrows * NCOLS):
        row, col = divmod(idx, NCOLS)
        axes[row][col].set_visible(False)

    fig.suptitle("Kalshi BTC 15M Markets — 2026-04-24", color="white", fontsize=12, y=1.01)
    plt.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
