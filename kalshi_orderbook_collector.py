#!/usr/bin/env python3
"""
Kalshi BTC 15-minute order book + public trades collector.

This version:
- loads config from a .env file
- discovers active BTC 15-minute markets via the public /markets endpoint
- does NOT require market tickers in environment variables
- authenticates only for the WebSocket stream
- maintains an in-memory aggregated book per market
- writes normalized JSONL logs to data/markets_ref/{ticker}/ subfolders

uv setup
--------
uv init
uv add websockets aiohttp cryptography python-dotenv

Example .env
------------
KALSHI_KEY_ID=your_api_key_id
KALSHI_PRIVATE_KEY_PATH=./kalshi-key.pem
KALSHI_ENV=prod
KALSHI_OUTPUT_DIR=./data
KALSHI_REST_SEED=true
KALSHI_SNAPSHOT_INTERVAL_SEC=1.0
KALSHI_DISCOVERY_LOOKAHEAD_MIN=180
KALSHI_DISCOVERY_STATUS=open
KALSHI_SERIES_TICKER=KXBTC15M
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, cast

import aiohttp
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dotenv import load_dotenv
from websockets.exceptions import ConnectionClosed

load_dotenv()
getcontext().prec = 28


def parse_kalshi_time(value: str | None) -> int:
    if not value:
        return 0
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(dt.timestamp())


def parse_kalshi_time_ms(value: str | None) -> int:
    return parse_kalshi_time(value) * 1000


def market_close_ts(m: dict[str, Any]) -> int:
    for key in ("close_time", "expiration_time", "expected_expiration_time", "latest_expiration_time"):
        ts = parse_kalshi_time(m.get(key))
        if ts:
            return ts
    return 0


def D(x: str | int | float | Decimal | None) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"Could not parse Decimal from {x!r}") from e


@dataclass
class KalshiConfig:
    key_id: str
    private_key_path: Path
    environment: str = "prod"
    output_dir: Path = Path("./data")
    rest_seed: bool = True
    snapshot_interval_sec: float = 1.0
    reconnect_base_sec: float = 1.0
    reconnect_max_sec: float = 30.0
    discovery_lookahead_min: int = 180
    discovery_status: str = "open"
    series_ticker: str = "KXBTC15M"
    market_tickers: list[str] = field(default_factory=list)

    @property
    def ws_url(self) -> str:
        if self.environment == "demo":
            return "wss://demo-api.kalshi.co/trade-api/ws/v2"
        return "wss://api.elections.kalshi.com/trade-api/ws/v2"

    @property
    def rest_base_url(self) -> str:
        if self.environment == "demo":
            return "https://demo-api.kalshi.co/trade-api/v2"
        return "https://api.elections.kalshi.com/trade-api/v2"

    @property
    def markets_ref_dir(self) -> Path:
        return self.output_dir / "markets_ref"


class KalshiSigner:
    def __init__(self, key_id: str, private_key_path: Path):
        self.key_id = key_id
        loaded_key = serialization.load_pem_private_key(
            private_key_path.read_bytes(),
            password=None,
        )
        if not isinstance(loaded_key, rsa.RSAPrivateKey):
            raise TypeError("Expected an RSA private key for Kalshi signing")
        self.private_key = cast(rsa.RSAPrivateKey, loaded_key)

    def create_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        path_no_query = path.split("?", 1)[0]
        msg_string = timestamp + method.upper() + path_no_query
        signature = self.private_key.sign(
            msg_string.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }


@dataclass
class BookSide:
    levels: dict[Decimal, Decimal] = field(default_factory=dict)

    def set_snapshot(self, rows: Iterable[Iterable[str]]) -> None:
        self.levels.clear()
        for row in rows:
            if len(row) != 2:
                continue
            price = D(row[0])
            size = D(row[1])
            if size > 0:
                self.levels[price] = size

    def apply_delta(self, price: Decimal, delta: Decimal) -> Decimal:
        new_size = self.levels.get(price, Decimal("0")) + delta
        if new_size <= 0:
            self.levels.pop(price, None)
            return Decimal("0")
        self.levels[price] = new_size
        return new_size

    def best_bid(self) -> Optional[Decimal]:
        if not self.levels:
            return None
        return max(self.levels)

    def top_n(self, n: int = 5) -> list[tuple[str, str]]:
        prices = sorted(self.levels.keys(), reverse=True)[:n]
        return [(str(p), str(self.levels[p])) for p in prices]


@dataclass
class MarketBook:
    market_ticker: str
    market_id: Optional[str] = None
    seq: Optional[int] = None
    yes: BookSide = field(default_factory=BookSide)
    no: BookSide = field(default_factory=BookSide)
    last_ts_ms: Optional[int] = None

    def apply_snapshot(self, msg: dict[str, Any], seq: Optional[int]) -> None:
        self.market_id = msg.get("market_id", self.market_id)
        self.seq = seq
        self.yes.set_snapshot(msg.get("yes_dollars_fp", []))
        self.no.set_snapshot(msg.get("no_dollars_fp", []))

    def apply_delta(self, msg: dict[str, Any], seq: Optional[int]) -> dict[str, Any]:
        side_name = msg["side"]
        price = D(msg["price_dollars"])
        delta = D(msg["delta_fp"])
        side = self.yes if side_name == "yes" else self.no
        new_size = side.apply_delta(price, delta)
        self.market_id = msg.get("market_id", self.market_id)
        self.seq = seq
        self.last_ts_ms = msg.get("ts_ms", self.last_ts_ms)
        return {
            "market_ticker": self.market_ticker,
            "market_id": self.market_id,
            "seq": self.seq,
            "side": side_name,
            "price_dollars": str(price),
            "delta_fp": str(delta),
            "new_size_fp": str(new_size),
            "ts": msg.get("ts"),
            "ts_ms": msg.get("ts_ms"),
        }

    def best_yes_bid(self) -> Optional[Decimal]:
        return self.yes.best_bid()

    def best_no_bid(self) -> Optional[Decimal]:
        return self.no.best_bid()

    def best_yes_ask(self) -> Optional[Decimal]:
        best_no_bid = self.best_no_bid()
        return None if best_no_bid is None else Decimal("1") - best_no_bid

    def best_no_ask(self) -> Optional[Decimal]:
        best_yes_bid = self.best_yes_bid()
        return None if best_yes_bid is None else Decimal("1") - best_yes_bid

    def mid_yes(self) -> Optional[Decimal]:
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    def microprice_yes(self) -> Optional[Decimal]:
        bid = self.best_yes_bid()
        ask = self.best_yes_ask()
        if bid is None or ask is None:
            return None
        bid_sz = self.yes.levels.get(bid, Decimal("0"))
        no_bid = self.best_no_bid()
        ask_sz = self.no.levels.get(no_bid, Decimal("0")) if no_bid is not None else Decimal("0")
        denom = bid_sz + ask_sz
        if denom <= 0:
            return None
        return (ask * bid_sz + bid * ask_sz) / denom

    def imbalance_yes(self) -> Optional[Decimal]:
        bid = self.best_yes_bid()
        no_bid = self.best_no_bid()
        if bid is None or no_bid is None:
            return None
        bid_sz = self.yes.levels.get(bid, Decimal("0"))
        ask_sz = self.no.levels.get(no_bid, Decimal("0"))
        denom = bid_sz + ask_sz
        if denom <= 0:
            return None
        return (bid_sz - ask_sz) / denom

    def snapshot_record(self, now_ms: int, depth: int = 5) -> dict[str, Any]:
        return {
            "type": "book_state",
            "ts_ms_local": now_ms,
            "market_ticker": self.market_ticker,
            "market_id": self.market_id,
            "seq": self.seq,
            "best_yes_bid": str(self.best_yes_bid()) if self.best_yes_bid() is not None else None,
            "best_yes_ask": str(self.best_yes_ask()) if self.best_yes_ask() is not None else None,
            "best_no_bid": str(self.best_no_bid()) if self.best_no_bid() is not None else None,
            "best_no_ask": str(self.best_no_ask()) if self.best_no_ask() is not None else None,
            "mid_yes": str(self.mid_yes()) if self.mid_yes() is not None else None,
            "microprice_yes": str(self.microprice_yes()) if self.microprice_yes() is not None else None,
            "imbalance_yes": str(self.imbalance_yes()) if self.imbalance_yes() is not None else None,
            "yes_top": self.yes.top_n(depth),
            "no_top": self.no.top_n(depth),
            "last_exchange_ts_ms": self.last_ts_ms,
        }


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        self._lock = asyncio.Lock()

    async def write(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        async with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()


async def discover_markets(
    config: KalshiConfig,
    *,
    series_ticker: Optional[str] = None,
    status: str = "open",
    min_close_ts: Optional[int] = None,
    max_close_ts: Optional[int] = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor: Optional[str] = None

    async with aiohttp.ClientSession() as session:
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            if series_ticker:
                params["series_ticker"] = series_ticker
            if status:
                params["status"] = status
            if min_close_ts is not None:
                params["min_close_ts"] = min_close_ts
            if max_close_ts is not None:
                params["max_close_ts"] = max_close_ts

            url = config.rest_base_url + "/markets"
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()

            results.extend(payload.get("markets", []))
            cursor = payload.get("cursor") or ""
            if not cursor:
                break

    return results


def choose_btc_15m_markets(markets: list[dict[str, Any]], now_ts: Optional[int] = None) -> list[dict[str, Any]]:
    now_ts = now_ts or int(time.time())

    def looks_like_btc(m: dict[str, Any]) -> bool:
        hay = " ".join(
            str(m.get(k, "")) for k in ("ticker", "title", "subtitle", "yes_sub_title", "no_sub_title")
        ).lower()
        return any(token in hay.split() for token in ("bitcoin", " btc", "btc ", "btc", "btc-", "-btc"))

    filtered = [m for m in markets if looks_like_btc(m) and market_close_ts(m) >= now_ts]
    filtered.sort(key=market_close_ts)
    return filtered


async def discover_btc_15m_markets(config: KalshiConfig) -> list[dict[str, Any]]:
    now_ts = int(time.time())
    max_close_ts = now_ts + 60 * config.discovery_lookahead_min
    markets = await discover_markets(
        config,
        series_ticker=config.series_ticker,
        status=config.discovery_status,
        min_close_ts=now_ts - 60,
        max_close_ts=max_close_ts,
        limit=1000,
    )
    result = choose_btc_15m_markets(markets, now_ts=now_ts)
    if not result:
        raise RuntimeError(
            "Auto-discovery found no matching BTC markets. Check KALSHI_SERIES_TICKER or widen the lookahead window."
        )
    return result


async def discover_btc_15m_tickers(config: KalshiConfig) -> list[str]:
    markets = await discover_btc_15m_markets(config)
    return [m["ticker"] for m in markets if "ticker" in m]


def build_open_ts_map(markets: list[dict[str, Any]]) -> dict[int, str]:
    """Map open_ts_ms -> ticker so the price tracker can route writes by 15M candle."""
    result: dict[int, str] = {}
    for m in markets:
        ticker = m.get("ticker")
        open_time = m.get("open_time")
        if ticker and open_time:
            open_ts_ms = parse_kalshi_time_ms(open_time)
            if open_ts_ms:
                result[open_ts_ms] = ticker
    return result


class KalshiCollector:
    def __init__(
        self,
        config: KalshiConfig,
        on_book_state: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self.signer = KalshiSigner(config.key_id, config.private_key_path)
        self.books: dict[str, MarketBook] = {
            ticker: MarketBook(market_ticker=ticker) for ticker in config.market_tickers
        }
        self.active_tickers: set[str] = set(config.market_tickers)
        self.ws = None
        self.stop_event = asyncio.Event()
        self._on_book_state = on_book_state

        # Maps ticker -> folder name (market_id once known, ticker as fallback).
        self._ticker_to_folder: dict[str, str] = {}
        self._event_writers: dict[str, JsonlWriter] = {}
        self._book_writers: dict[str, JsonlWriter] = {}
        self._global_writer = JsonlWriter(config.markets_ref_dir / "_global" / "events.jsonl")
        self._message_id = 1

    def _folder_for(self, ticker: str, market_id: str | None) -> str:
        """Return and cache the folder name for a ticker (market_id when known)."""
        if market_id and ticker not in self._ticker_to_folder:
            self._ticker_to_folder[ticker] = market_id
        return self._ticker_to_folder.get(ticker, ticker)

    def _event_writer(self, folder: str) -> JsonlWriter:
        if folder not in self._event_writers:
            path = self.config.markets_ref_dir / folder / "events.jsonl"
            self._event_writers[folder] = JsonlWriter(path)
        return self._event_writers[folder]

    def _book_writer(self, folder: str) -> JsonlWriter:
        if folder not in self._book_writers:
            path = self.config.markets_ref_dir / folder / "book_states.jsonl"
            self._book_writers[folder] = JsonlWriter(path)
        return self._book_writers[folder]

    async def close(self) -> None:
        for w in self._event_writers.values():
            w.close()
        for w in self._book_writers.values():
            w.close()
        self._global_writer.close()

    async def seed_from_rest(self) -> None:
        if not self.config.rest_seed:
            return
        async with aiohttp.ClientSession() as session:
            tasks = [self._seed_market(session, ticker) for ticker in self.config.market_tickers]
            await asyncio.gather(*tasks)

    async def _seed_market(self, session: aiohttp.ClientSession, ticker: str) -> None:
        path = f"/trade-api/v2/markets/{ticker}/orderbook"
        headers = self.signer.create_headers("GET", path)
        url = self.config.rest_base_url + f"/markets/{ticker}/orderbook"
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except Exception as e:
            logging.warning("REST seed failed for %s: %s", ticker, e)
            return

        msg = payload.get("orderbook") or payload.get("msg") or payload
        book = self.books[ticker]
        yes_rows = msg.get("yes_dollars_fp") or msg.get("yes") or []
        no_rows = msg.get("no_dollars_fp") or msg.get("no") or []
        book.apply_snapshot(
            {"market_id": msg.get("market_id"), "yes_dollars_fp": yes_rows, "no_dollars_fp": no_rows},
            seq=None,
        )
        now_ms = int(time.time() * 1000)
        folder = self._folder_for(ticker, book.market_id)
        await self._event_writer(folder).write(
            {
                "type": "rest_seed_snapshot",
                "ts_ms_local": now_ms,
                "market_ticker": ticker,
                "market_id": book.market_id,
                "yes_dollars_fp": yes_rows,
                "no_dollars_fp": no_rows,
            }
        )
        logging.info("Seeded %s from REST -> folder %s", ticker, folder)

    def _next_message_id(self) -> int:
        value = self._message_id
        self._message_id += 1
        return value

    async def subscribe(self, websocket, tickers: list[str]) -> None:
        if not tickers:
            return
        for channels in (["orderbook_delta"], ["trade"]):
            msg = {
                "id": self._next_message_id(),
                "cmd": "subscribe",
                "params": {"channels": channels, "market_tickers": tickers},
            }
            await websocket.send(json.dumps(msg))
        await self._global_writer.write(
            {
                "type": "subscription_update",
                "ts_ms_local": int(time.time() * 1000),
                "action": "subscribe",
                "market_tickers": tickers,
            }
        )

    async def refresh_market_tickers(self) -> list[str]:
        markets = await discover_btc_15m_markets(self.config)
        discovered = [m["ticker"] for m in markets if "ticker" in m]
        new_tickers = [t for t in discovered if t not in self.active_tickers]
        for ticker in new_tickers:
            self.books.setdefault(ticker, MarketBook(market_ticker=ticker))
            self.active_tickers.add(ticker)
        self.config.market_tickers = sorted(self.active_tickers)
        return new_tickers

    async def market_roll_loop(self) -> None:
        backoff = 25.0
        while not self.stop_event.is_set():
            try:
                new_tickers = await self.refresh_market_tickers()
                if new_tickers and self.ws is not None:
                    await self.subscribe(self.ws, new_tickers)
                    logging.info("Subscribed to new market tickers: %s", ", ".join(new_tickers))
                backoff = 25.0  # reset on success
            except Exception:
                logging.exception("Failed to refresh market tickers — retrying in %.0fs", backoff)
                backoff = min(backoff * 2, 300.0)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass

    async def snapshot_loop(self) -> None:
        while not self.stop_event.is_set():
            now_ms = int(time.time() * 1000)
            for ticker, book in self.books.items():
                folder = self._folder_for(ticker, book.market_id)
                record = book.snapshot_record(now_ms=now_ms, depth=5)
                await self._book_writer(folder).write(record)
                if self._on_book_state is not None:
                    self._on_book_state(record)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.config.snapshot_interval_sec)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        await self.seed_from_rest()
        snapshot_task = asyncio.create_task(self.snapshot_loop())
        market_roll_task = asyncio.create_task(self.market_roll_loop())
        try:
            await self._run_ws_forever()
        finally:
            self.stop_event.set()
            await snapshot_task
            await market_roll_task
            await self.close()

    async def _run_ws_forever(self) -> None:
        attempt = 0
        while not self.stop_event.is_set():
            headers = self.signer.create_headers("GET", "/trade-api/ws/v2")
            try:
                logging.info("Connecting to %s", self.config.ws_url)
                async with websockets.connect(
                    self.config.ws_url,
                    additional_headers=headers,
                    max_size=8 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    logging.info("Connected")
                    attempt = 0
                    self.ws = ws
                    await self.subscribe(ws, sorted(self.active_tickers))
                    async for raw in ws:
                        await self.handle_message(raw)
            except (ConnectionClosed, OSError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.ws = None
                if self.stop_event.is_set():
                    break
                attempt += 1
                delay = min(
                    self.config.reconnect_max_sec,
                    self.config.reconnect_base_sec * (2 ** (attempt - 1)),
                )
                delay *= 0.75 + 0.5 * random.random()
                logging.warning("Disconnected: %s. Reconnecting in %.2fs", e, delay)
                await asyncio.sleep(delay)
            except Exception:
                self.ws = None
                logging.exception("Fatal collector error")
                await asyncio.sleep(2)

    async def handle_message(self, raw: str) -> None:
        now_ms = int(time.time() * 1000)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logging.warning("Skipping non-JSON message: %r", raw[:200])
            return

        msg_type = data.get("type")
        seq = data.get("seq")
        msg = data.get("msg", {})

        if msg_type == "subscribed":
            await self._global_writer.write({"type": "subscribed", "ts_ms_local": now_ms, **data})
            return

        if msg_type == "error":
            await self._global_writer.write({"type": "ws_error", "ts_ms_local": now_ms, **data})
            logging.error("WS error: %s", data)
            return

        if msg_type == "orderbook_snapshot":
            ticker = msg["market_ticker"]
            book = self.books.setdefault(ticker, MarketBook(market_ticker=ticker))
            book.apply_snapshot(msg, seq)
            folder = self._folder_for(ticker, book.market_id)
            await self._event_writer(folder).write(
                {
                    "type": "orderbook_snapshot",
                    "ts_ms_local": now_ms,
                    "seq": seq,
                    "market_ticker": ticker,
                    "market_id": book.market_id,
                    "yes_dollars_fp": msg.get("yes_dollars_fp", []),
                    "no_dollars_fp": msg.get("no_dollars_fp", []),
                }
            )
            return

        if msg_type == "orderbook_delta":
            ticker = msg["market_ticker"]
            book = self.books.setdefault(ticker, MarketBook(market_ticker=ticker))
            delta_record = book.apply_delta(msg, seq)
            delta_record.update({"type": "orderbook_delta", "ts_ms_local": now_ms})
            if "client_order_id" in msg:
                delta_record["client_order_id"] = msg["client_order_id"]
            folder = self._folder_for(ticker, book.market_id)
            await self._event_writer(folder).write(delta_record)
            return

        if msg_type == "trade":
            ticker = msg.get("market_ticker", "")
            market_id = msg.get("market_id")
            trade_record = {
                "type": "trade",
                "ts_ms_local": now_ms,
                "trade_id": msg.get("trade_id"),
                "market_ticker": ticker,
                "market_id": market_id,
                "yes_price_dollars": msg.get("yes_price_dollars"),
                "no_price_dollars": msg.get("no_price_dollars"),
                "count_fp": msg.get("count_fp"),
                "taker_side": msg.get("taker_side"),
                "ts": msg.get("ts"),
                "ts_ms": msg.get("ts_ms"),
            }
            if ticker:
                folder = self._folder_for(ticker, market_id)
                await self._event_writer(folder).write(trade_record)
            else:
                await self._global_writer.write(trade_record)
            return

        await self._global_writer.write({"type": "other", "ts_ms_local": now_ms, "payload": data})


def load_config_from_env() -> KalshiConfig:
    key_id = os.environ["KALSHI_KEY_ID"]
    private_key_path = Path(os.environ["KALSHI_PRIVATE_KEY_PATH"])
    environment = os.getenv("KALSHI_ENV", "prod").strip().lower()
    if environment not in {"prod", "demo"}:
        raise ValueError("KALSHI_ENV must be 'prod' or 'demo'")

    output_dir = Path(os.getenv("KALSHI_OUTPUT_DIR", "./data"))
    rest_seed = os.getenv("KALSHI_REST_SEED", "true").strip().lower() in {"1", "true", "yes", "y"}
    snapshot_interval_sec = float(os.getenv("KALSHI_SNAPSHOT_INTERVAL_SEC", "1.0"))
    discovery_lookahead_min = int(os.getenv("KALSHI_DISCOVERY_LOOKAHEAD_MIN", "180"))
    discovery_status = os.getenv("KALSHI_DISCOVERY_STATUS", "open").strip() or "open"
    series_ticker = os.getenv("KALSHI_SERIES_TICKER", "KXBTC15M").strip() or "KXBTC15M"

    return KalshiConfig(
        key_id=key_id,
        private_key_path=private_key_path,
        environment=environment,
        output_dir=output_dir,
        rest_seed=rest_seed,
        snapshot_interval_sec=snapshot_interval_sec,
        discovery_lookahead_min=discovery_lookahead_min,
        discovery_status=discovery_status,
        series_ticker=series_ticker,
    )


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config_from_env()
    markets = await discover_btc_15m_markets(config)
    config.market_tickers = [m["ticker"] for m in markets if "ticker" in m]
    logging.info("Initial market tickers: %s", ", ".join(config.market_tickers))
    collector = KalshiCollector(config)

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, collector.stop_event.set)
            except NotImplementedError:
                pass

    await collector.run()


if __name__ == "__main__":
    asyncio.run(amain())
