#!/usr/bin/env python3
"""
Single entry point: runs the Kalshi orderbook collector, BTC price tracker,
and trading bot concurrently.

Output layout:
  data/markets_ref/btc_reference.jsonl          — single BTC price log
  data/markets_ref/{market_id}/events.jsonl     — per-market WS events
  data/markets_ref/{market_id}/book_states.jsonl
  data/trade_log.jsonl                          — order log (bot)

Usage:
    uv run python main.py
"""

import asyncio
import logging
import os
import signal

from kalshi_orderbook_collector import (
    KalshiCollector,
    discover_btc_15m_markets,
    load_config_from_env,
)
from firebase_logger import FirebaseLogger
from price_tracker import BtcPriceTracker
from trading_bot import DEFAULT_KELLY_FRACTION, KalshiTrader


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config_from_env()
    markets = await discover_btc_15m_markets(config)
    config.market_tickers = [m["ticker"] for m in markets if "ticker" in m]
    logging.info("Tracking %d markets", len(config.market_tickers))

    kelly_fraction = float(os.getenv("KALSHI_KELLY_FRACTION", str(DEFAULT_KELLY_FRACTION)))
    firebase = FirebaseLogger(
        credentials_path=os.getenv("FIREBASE_CREDENTIALS_PATH"),
        project_id=os.getenv("FIREBASE_PROJECT_ID"),
    )
    trader = KalshiTrader(config, kelly_fraction=kelly_fraction, firebase=firebase)

    collector = KalshiCollector(config, on_book_state=trader.on_book_state)
    price_tracker = BtcPriceTracker(
        markets_ref_dir=config.markets_ref_dir,
        stop_event=collector.stop_event,
        on_btc_price=trader.on_btc_price,
    )

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, collector.stop_event.set)
            except NotImplementedError:
                pass

    await asyncio.gather(
        collector.run(),
        price_tracker.run(),
        trader.run(collector.stop_event),
    )


if __name__ == "__main__":
    asyncio.run(amain())
