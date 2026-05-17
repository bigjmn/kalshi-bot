"""
Async BTC settlement price tracker.

Polls the Kalshi public BTC reference endpoint every second and appends records
to a single file: data/markets_ref/btc_reference.jsonl.

Each record includes ts_ms_local (wall-clock time of the poll) so it can be
time-joined to any market's book_states by the plot tooling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

import aiohttp

BTC_URL = (
    "https://kalshi-public-docs.s3.amazonaws.com/external/crypto/btc_current.json"
    "?allowRequestEvenIfPageIsHidden=true"
)
POLL_INTERVAL_SEC = 1.0
MAX_CONSECUTIVE_ERRORS = 30


class BtcPriceTracker:
    def __init__(
        self,
        markets_ref_dir: Path,
        stop_event: asyncio.Event,
        on_btc_price: Callable[[float, int], None] | None = None,
    ):
        path = markets_ref_dir / "btc_reference.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._stop = stop_event
        self._on_btc_price = on_btc_price

    def _write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    async def run(self) -> None:
        next_tick = time.monotonic()
        prev_maturity_ts: int = 0
        consecutive_errors: int = 0

        async with aiohttp.ClientSession() as session:
            while not self._stop.is_set():
                try:
                    async with session.get(
                        BTC_URL, timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        resp.raise_for_status()
                        payload = await resp.json(content_type=None)

                    maturity_ts_ms: int = payload["maturity_ts_ms"]
                    if maturity_ts_ms != prev_maturity_ts:
                        prev_maturity_ts = maturity_ts_ms
                        last_price = payload["timeseries"]["second"][-1:]
                        ts_ms_local = int(time.time() * 1000)
                        self._write(
                            {
                                "ts_ms_local": ts_ms_local,
                                "timestamp": maturity_ts_ms,
                                "last_price": last_price,
                            }
                        )
                        if self._on_btc_price is not None:
                            self._on_btc_price(last_price[0], ts_ms_local)

                    if consecutive_errors > 0:
                        logging.info("BTC tracker recovered after %d errors", consecutive_errors)
                    consecutive_errors = 0

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    consecutive_errors += 1
                    if consecutive_errors == 1:
                        logging.warning("BTC tracker error: %s", e)
                    elif consecutive_errors % 10 == 0:
                        logging.error(
                            "BTC tracker has failed %d consecutive times — "
                            "bot has no price data and WILL NOT TRADE: %s",
                            consecutive_errors, e,
                        )
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        raise RuntimeError(
                            f"BTC tracker failed {consecutive_errors} consecutive times, forcing restart"
                        )

                next_tick += POLL_INTERVAL_SEC
                sleep_time = max(0.0, next_tick - time.monotonic())
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_time)
                    break
                except asyncio.TimeoutError:
                    pass

        self.close()
