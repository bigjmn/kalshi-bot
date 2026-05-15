"""
Kalshi two-sided trading bot.

Places up to one YES and one NO limit order per market ticker when:
    p_yes - best_yes_ask  >=  EDGE_THRESHOLD   → buy YES
    p_no  - best_no_ask   >=  EDGE_THRESHOLD   → buy NO

Kelly sizing:
    f = kelly_fraction * (p - a) / (1 - a)
    contracts = floor(f * balance_dollars / a),  min 1
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from kalshi_orderbook_collector import (
    KalshiConfig,
    KalshiSigner,
    discover_btc_15m_markets,
    parse_kalshi_time_ms,
)
from firebase_logger import FirebaseLogger
from true_prob import yes_probability

WINDOW_S: float = 60.0
EDGE_THRESHOLD: float = 0.2
DEFAULT_KELLY_FRACTION: float = 1.0
DEFAULT_SIGMA_FALLBACK: float = 10.0
BALANCE_PRINT_INTERVAL_SEC: float = 300.0
STATUS_LOG_INTERVAL_SEC: float = 20.0
CASHOUT_DELTA: float | None = 0.25


def btc_sigma_recent(btc_file: Path, lookback_ms: int = 7_200_000, step_s: float = 60.0) -> float:
    """
    ABM sigma in USD/sqrt(s) from recent BTC tick data.

    Downsamples to `step_s`-second intervals before computing diffs. This is
    critical because Kalshi's reference price is a smoothed TWAP index whose
    consecutive-tick diffs severely underestimate long-range volatility. Using
    60-second intervals removes autocorrelation and captures the true random-walk
    sigma at the scale that matters for settlement.
    """
    now = int(time.time() * 1000)
    cutoff = now - lookback_ms
    step_ms = int(step_s * 1000)

    raw: list[tuple[int, float]] = []
    with btc_file.open() as f:
        for line in f:
            obj = json.loads(line)
            t = obj.get("ts_ms_local") or obj.get("timestamp")
            if t is None or t < cutoff or t > now:
                continue
            raw.append((t, obj["last_price"][0]))
    raw.sort()
    if not raw:
        return 0.0

    # Downsample: one price per step_s bucket (first record wins)
    sampled: list[tuple[int, float]] = []
    bucket = raw[0][0] // step_ms
    for t, p in raw:
        b = t // step_ms
        if b != bucket or not sampled:
            sampled.append((t, p))
            bucket = b

    if len(sampled) < 2:
        return 0.0

    diffs = [sampled[i + 1][1] - sampled[i][1] for i in range(len(sampled) - 1)]
    dt_s = [(sampled[i + 1][0] - sampled[i][0]) / 1000.0 for i in range(len(sampled) - 1)]
    mean_dt_s = sum(dt_s) / len(dt_s)
    if mean_dt_s <= 0.0:
        return 0.0
    n = len(diffs)
    mean_d = sum(diffs) / n
    std_diffs = math.sqrt(sum((d - mean_d) ** 2 for d in diffs) / (n - 1))
    return round(std_diffs / math.sqrt(mean_dt_s), 6)


def _trapezoid(pts: list[tuple[float, float]], t_lo: float, t_hi: float) -> float:
    A = 0.0
    for i in range(len(pts) - 1):
        s0, x0 = pts[i]
        s1, x1 = pts[i + 1]
        a, b = max(s0, t_lo), min(s1, t_hi)
        if a >= b:
            continue
        frac0 = (a - s0) / (s1 - s0)
        frac1 = (b - s0) / (s1 - s0)
        xa = x0 + frac0 * (x1 - x0)
        xb = x0 + frac1 * (x1 - x0)
        A += (xa + xb) / 2.0 * (b - a)
    return A


@dataclass
class MarketState:
    ticker: str
    K: float
    T_ms: float
    sigma: float

    window_open_sec: float = field(init=False)
    window_pts: list[tuple[float, float]] = field(default_factory=list)
    last_price_before_window: float | None = None
    latest_yes_ask: float | None = None
    latest_no_ask: float | None = None
    has_yes_bet: bool = False
    has_no_bet: bool = False
    market_open_ms: int | None = None
    yes_entry_price: float | None = None
    no_entry_price: float | None = None
    yes_position_count: int = 0
    no_position_count: int = 0
    yes_win_pct: float | None = None
    no_win_pct: float | None = None
    yes_edge: float | None = None
    no_edge: float | None = None
    yes_bet_time_ms: int | None = None
    no_bet_time_ms: int | None = None
    yes_sigma: float | None = None
    no_sigma: float | None = None
    yes_firebase_doc_id: str | None = None
    no_firebase_doc_id: str | None = None

    def __post_init__(self) -> None:
        self.window_open_sec = self.T_ms / 1000.0 - WINDOW_S

    @property
    def T_sec(self) -> float:
        return self.T_ms / 1000.0

    def is_expired(self, now_ms: int) -> bool:
        return now_ms > self.T_ms

    def tau_sec(self, now_ms: int) -> float:
        return (self.T_ms - now_ms) / 1000.0

    def update_btc(self, price: float, ts_ms: int) -> None:
        t_sec = ts_ms / 1000.0
        tau = self.T_sec - t_sec
        if tau >= WINDOW_S:
            self.last_price_before_window = price
            return
        if tau <= 0.0:
            return
        if not self.window_pts:
            anchor = self.last_price_before_window if self.last_price_before_window is not None else price
            self.window_pts.append((self.window_open_sec, anchor))
        self.window_pts.append((t_sec, price))

    def compute_A_t(self, t_sec: float) -> float:
        if not self.window_pts:
            return 0.0
        return _trapezoid(self.window_pts, self.window_open_sec, t_sec)


class KalshiTrader:
    def __init__(
        self,
        config: KalshiConfig,
        kelly_fraction: float = DEFAULT_KELLY_FRACTION,
        firebase: FirebaseLogger | None = None,
    ):
        self.config = config
        self.kelly_fraction = kelly_fraction
        self.signer = KalshiSigner(config.key_id, config.private_key_path)
        self._states: dict[str, MarketState] = {}
        self._balance_cents: int = 0
        self._pending_orders: set[tuple[str, str]] = set()
        self._firebase = firebase or FirebaseLogger(None)

        log_path = config.output_dir / "trade_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._trade_log = log_path.open("a", encoding="utf-8")

        logging.info("KalshiTrader ready (edge=%.2f kelly=%.2f)", EDGE_THRESHOLD, kelly_fraction)

    # ── callbacks ───────────────────────────────────────────────────────────

    def on_btc_price(self, price: float, ts_ms: int) -> None:
        for state in self._states.values():
            if not state.is_expired(ts_ms):
                state.update_btc(price, ts_ms)

    def on_book_state(self, record: dict[str, Any]) -> None:
        ticker = record.get("market_ticker")
        if not ticker:
            return
        state = self._states.get(ticker)
        if state is None:
            return

        for field_name, attr in (("best_yes_ask", "latest_yes_ask"), ("best_no_ask", "latest_no_ask")):
            val = record.get(field_name)
            if val is not None:
                try:
                    setattr(state, attr, float(val))
                except ValueError:
                    pass

        p_yes = self._compute_true_prob(ticker)
        if p_yes is None:
            return
        if p_yes < 0.001 or p_yes > 0.999:
            return
        p_no = 1.0 - p_yes

        if not state.has_yes_bet and (ticker, "yes") not in self._pending_orders:
            ask = state.latest_yes_ask
            if ask is not None and (p_yes - ask) >= EDGE_THRESHOLD:
                logging.info("YES edge: %s  p=%.4f  ask=%.4f  edge=+%.4f", ticker, p_yes, ask, p_yes - ask)
                self._pending_orders.add((ticker, "yes"))
                asyncio.create_task(self._handle_signal(ticker, p_yes, ask, "yes"))

        if not state.has_no_bet and (ticker, "no") not in self._pending_orders:
            ask = state.latest_no_ask
            if ask is not None and (p_no - ask) >= EDGE_THRESHOLD:
                logging.info("NO edge:  %s  p=%.4f  ask=%.4f  edge=+%.4f", ticker, p_no, ask, p_no - ask)
                self._pending_orders.add((ticker, "no"))
                asyncio.create_task(self._handle_signal(ticker, p_no, ask, "no"))

        if CASHOUT_DELTA is not None:
            for side, has_bet, entry_price, opp_ask in (
                ("yes", state.has_yes_bet, state.yes_entry_price, state.latest_no_ask),
                ("no",  state.has_no_bet,  state.no_entry_price,  state.latest_yes_ask),
            ):
                current_ask = state.latest_yes_ask if side == "yes" else state.latest_no_ask
                if (has_bet and entry_price is not None and current_ask is not None
                        and opp_ask is not None
                        and current_ask >= entry_price + CASHOUT_DELTA
                        and (ticker, f"{side}_sell") not in self._pending_orders):
                    logging.info("Cashout trigger: %s %s  entry=%.4f  now=%.4f",
                                 side.upper(), ticker, entry_price, current_ask)
                    self._pending_orders.add((ticker, f"{side}_sell"))
                    asyncio.create_task(self._handle_cashout(ticker, side))

    # ── probability ──────────────────────────────────────────────────────────

    def _compute_true_prob(self, ticker: str) -> float | None:
        state = self._states.get(ticker)
        if state is None:
            return None
        now_ms = int(time.time() * 1000)
        tau = state.tau_sec(now_ms)
        if tau <= 0.0:
            return None
        if state.window_pts:
            current_price = state.window_pts[-1][1]
        elif state.last_price_before_window is not None:
            current_price = state.last_price_before_window
        else:
            return None
        try:
            if tau >= WINDOW_S:
                return yes_probability(float(now_ms), state.T_ms, current_price,
                                       state.K, state.sigma, window_s=WINDOW_S)
            else:
                if not state.window_pts:
                    return None
                A_t = state.compute_A_t(now_ms / 1000.0)
                return yes_probability(float(now_ms), state.T_ms, current_price,
                                       state.K, state.sigma, A_t=A_t, window_s=WINDOW_S)
        except (ValueError, ZeroDivisionError) as exc:
            logging.warning("yes_probability error for %s: %s", ticker, exc)
            return None

    # ── sizing ───────────────────────────────────────────────────────────────

    def _kelly_contracts(self, p: float, a: float, balance_cents: int) -> int:
        if a <= 0.0 or a >= 1.0:
            return 1
        f_scaled = self.kelly_fraction * (p - a) / (1.0 - a)
        if f_scaled <= 0.0:
            return 1
        return max(1, math.floor(f_scaled * (balance_cents / 100.0) / a))

    # ── API calls ────────────────────────────────────────────────────────────

    async def fetch_balance(self) -> int:
        headers = self.signer.create_headers("GET", "/trade-api/v2/portfolio/balance")
        url = self.config.rest_base_url + "/portfolio/balance"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
                    self._balance_cents = int(data["balance"])
            except Exception as exc:
                logging.error("fetch_balance failed: %s", exc)
        return self._balance_cents

    async def _place_order(self, ticker: str, price_cents: int, count: int, side: str, action: str = "buy") -> bool:
        path = "/trade-api/v2/portfolio/orders"
        headers = self.signer.create_headers("POST", path)
        price_key = "yes_price" if side == "yes" else "no_price"
        body = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "type": "limit",
            "count": count,
            price_key: price_cents,
        }
        record: dict[str, Any] = {
            "ts_ms_local": int(time.time() * 1000),
            "ticker": ticker,
            "side": side,
            "action": action,
            "price_cents": price_cents,
            "count": count,
            "status": "unknown",
            "http_status": None,
            "response": None,
        }
        success = False
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(self.config.rest_base_url + path[len("/trade-api/v2"):],
                                        headers=headers, json=body,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    raw = await resp.text()
                    record["http_status"] = resp.status
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = raw
                    record["response"] = payload
                    if resp.ok:
                        record["status"] = "ok"
                        success = True
                        logging.info("Order placed: %s %s %s  count=%d  price=%d¢",
                                     action.upper(), side.upper(), ticker, count, price_cents)
                    else:
                        record["status"] = "error"
                        logging.error("Order rejected: %s %s  status=%d  %s",
                                      side.upper(), ticker, resp.status, payload)
            except Exception as exc:
                record["status"] = "exception"
                record["error"] = str(exc)
                logging.error("Order exception for %s %s: %s", side.upper(), ticker, exc)

        self._trade_log.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._trade_log.flush()
        return success

    # ── signal handler ───────────────────────────────────────────────────────

    async def _handle_signal(self, ticker: str, p: float, ask: float, side: str) -> None:
        try:
            state = self._states.get(ticker)
            if state is None:
                return
            if (side == "yes" and state.has_yes_bet) or (side == "no" and state.has_no_bet):
                return

            current_ask = state.latest_yes_ask if side == "yes" else state.latest_no_ask
            if current_ask is None or (p - current_ask) < EDGE_THRESHOLD:
                logging.info("Edge evaporated for %s %s", side.upper(), ticker)
                return

            price_cents = max(1, min(99, round(current_ask * 100)))
            balance_cents = await self.fetch_balance()
            if balance_cents <= 0:
                logging.warning("Zero balance, skipping %s %s", side.upper(), ticker)
                return

            count = self._kelly_contracts(p, current_ask, balance_cents)
            logging.info("Placing %s: %s  p=%.4f  ask=%.4f  edge=+%.4f  count=%d  balance=$%.2f",
                         side.upper(), ticker, p, current_ask, p - current_ask,
                         count, balance_cents / 100.0)
            bet_time_ms = int(time.time() * 1000)
            success = await self._place_order(ticker, price_cents, count, side)
            if success:
                state = self._states.get(ticker)
                if state is not None:
                    edge = p - current_ask
                    if side == "yes":
                        state.yes_entry_price = current_ask
                        state.yes_position_count = count
                        state.yes_win_pct = p
                        state.yes_edge = edge
                        state.yes_bet_time_ms = bet_time_ms
                        state.yes_sigma = state.sigma
                    else:
                        state.no_entry_price = current_ask
                        state.no_position_count = count
                        state.no_win_pct = p
                        state.no_edge = edge
                        state.no_bet_time_ms = bet_time_ms
                        state.no_sigma = state.sigma
                    doc_id = await self._firebase.log_bet(
                        ticker=ticker,
                        market_open_ms=state.market_open_ms,
                        side=side,
                        ask_price=current_ask,
                        win_pct=p,
                        edge=edge,
                        contract_count=count,
                        sigma=state.sigma,
                        bet_time_ms=bet_time_ms,
                    )
                    if side == "yes":
                        state.yes_firebase_doc_id = doc_id
                    else:
                        state.no_firebase_doc_id = doc_id
        finally:
            state = self._states.get(ticker)
            if state is not None:
                if side == "yes":
                    state.has_yes_bet = True
                else:
                    state.has_no_bet = True
            self._pending_orders.discard((ticker, side))

    # ── cashout handler ──────────────────────────────────────────────────────

    async def _handle_cashout(self, ticker: str, side: str) -> None:
        try:
            state = self._states.get(ticker)
            if state is None:
                return
            entry_price = state.yes_entry_price if side == "yes" else state.no_entry_price
            count = state.yes_position_count if side == "yes" else state.no_position_count
            if entry_price is None or count == 0:
                return
            current_ask = state.latest_yes_ask if side == "yes" else state.latest_no_ask
            opp_ask = state.latest_no_ask if side == "yes" else state.latest_yes_ask
            if current_ask is None or opp_ask is None or CASHOUT_DELTA is None:
                return
            if current_ask < entry_price + CASHOUT_DELTA:
                logging.info("Cashout edge evaporated for %s %s", side.upper(), ticker)
                return
            # Sell at the opposite side's ask complement (= our bid)
            sell_price_cents = max(1, min(99, round((1.0 - opp_ask) * 100)))
            logging.info("Cashing out %s: %s  entry=%.4f  now=%.4f  count=%d  sell=%d¢",
                         side.upper(), ticker, entry_price, current_ask, count, sell_price_cents)
            success = await self._place_order(ticker, sell_price_cents, count, side, action="sell")
            if success:
                state = self._states.get(ticker)
                if state is not None:
                    doc_id = state.yes_firebase_doc_id if side == "yes" else state.no_firebase_doc_id
                    await self._firebase.update_cashout(
                        doc_id=doc_id or "",
                        sell_price=sell_price_cents / 100.0,
                        contract_count=count,
                        ask_price=entry_price,
                        close_time_ms=int(time.time() * 1000),
                    )
                    if side == "yes":
                        state.yes_entry_price = None
                        state.yes_position_count = 0
                        state.has_yes_bet = False
                        state.yes_firebase_doc_id = None
                    else:
                        state.no_entry_price = None
                        state.no_position_count = 0
                        state.has_no_bet = False
                        state.no_firebase_doc_id = None
        finally:
            self._pending_orders.discard((ticker, f"{side}_sell"))

    # ── settlement ───────────────────────────────────────────────────────────

    async def _fetch_market_result(self, ticker: str) -> str | None:
        """Returns 'yes', 'no', or None if not yet settled."""
        path = f"/trade-api/v2/markets/{ticker}"
        headers = self.signer.create_headers("GET", path)
        url = self.config.rest_base_url + f"/markets/{ticker}"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.ok:
                        data = await resp.json(content_type=None)
                        result = (data.get("market") or data).get("result")
                        return result if result in ("yes", "no") else None
            except Exception as exc:
                logging.error("_fetch_market_result failed for %s: %s", ticker, exc)
        return None

    async def _settle_market(self, ticker: str, state: MarketState) -> None:
        close_time_ms = int(time.time() * 1000)
        has_yes = state.has_yes_bet and state.yes_firebase_doc_id is not None and state.yes_entry_price is not None
        has_no = state.has_no_bet and state.no_firebase_doc_id is not None and state.no_entry_price is not None
        if not has_yes and not has_no:
            return
        result = await self._fetch_market_result(ticker)
        if result is None:
            logging.warning("Settlement not yet available for %s; Firebase record left open", ticker)
            return
        if has_yes and state.yes_firebase_doc_id and state.yes_entry_price is not None:
            await self._firebase.update_settlement(
                doc_id=state.yes_firebase_doc_id,
                won=(result == "yes"),
                contract_count=state.yes_position_count,
                ask_price=state.yes_entry_price,
                close_time_ms=close_time_ms,
            )
        if has_no and state.no_firebase_doc_id and state.no_entry_price is not None:
            await self._firebase.update_settlement(
                doc_id=state.no_firebase_doc_id,
                won=(result == "no"),
                contract_count=state.no_position_count,
                ask_price=state.no_entry_price,
                close_time_ms=close_time_ms,
            )

    # ── market loading ───────────────────────────────────────────────────────

    async def _load_market_states(self) -> None:
        markets = await discover_btc_15m_markets(self.config)
        btc_file = self.config.markets_ref_dir / "btc_reference.jsonl"

        sigma = DEFAULT_SIGMA_FALLBACK
        if btc_file.exists():
            computed = btc_sigma_recent(btc_file)
            if computed > 0.0:
                sigma = computed

        for m in markets:
            ticker = m.get("ticker")
            floor_strike = m.get("floor_strike")
            close_time = m.get("close_time")
            open_time = m.get("open_time")
            if not ticker or floor_strike is None or not close_time:
                continue

            T_ms = parse_kalshi_time_ms(close_time)
            open_ms = parse_kalshi_time_ms(open_time) if open_time else None

            if ticker not in self._states:
                self._states[ticker] = MarketState(
                    ticker=ticker,
                    K=float(floor_strike),
                    T_ms=float(T_ms),
                    sigma=sigma,
                    market_open_ms=open_ms,
                )
                logging.info("Loaded: %s  K=%.2f  sigma=%.4f", ticker, float(floor_strike), sigma)
            else:
                self._states[ticker].sigma = sigma

        logging.info("Loaded %d market states  sigma=%.4f", len(self._states), sigma)

    # ── status loop ──────────────────────────────────────────────────────────

    async def _status_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=STATUS_LOG_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            if stop_event.is_set():
                break
            try:
                now_ms = int(time.time() * 1000)
                active = [(t, s) for t, s in list(self._states.items()) if not s.is_expired(now_ms)]
                if not active:
                    await self._load_market_states()
                    now_ms = int(time.time() * 1000)
                    active = [(t, s) for t, s in list(self._states.items()) if not s.is_expired(now_ms)]
                for ticker, state in active:
                    if state.window_pts:
                        price = state.window_pts[-1][1]
                    elif state.last_price_before_window is not None:
                        price = state.last_price_before_window
                    else:
                        price = None
                    p_yes = self._compute_true_prob(ticker)
                    if price is not None and p_yes is not None:
                        tau = state.tau_sec(now_ms)
                        logging.info(
                            "Status: %s  BTC=$%.2f  K=%.2f  sigma=%.4f  tau=%.0fs  "
                            "p_yes=%.4f  p_no=%.4f  yes_ask=%.4f  no_ask=%.4f",
                            ticker, price, state.K, state.sigma, tau,
                            p_yes, 1.0 - p_yes,
                            state.latest_yes_ask if state.latest_yes_ask is not None else float("nan"),
                            state.latest_no_ask if state.latest_no_ask is not None else float("nan"),
                        )
                    else:
                        logging.info("Status: %s  BTC=N/A  p_yes=N/A", ticker)
            except Exception as exc:
                logging.warning("Status loop error: %s", exc)

    # ── run loop ─────────────────────────────────────────────────────────────

    async def run(self, stop_event: asyncio.Event) -> None:
        await self._load_market_states()
        await self.fetch_balance()
        logging.info("Balance: $%.2f", self._balance_cents / 100.0)
        asyncio.create_task(self._status_loop(stop_event))

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=BALANCE_PRINT_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
            if not stop_event.is_set():
                await self.fetch_balance()
                logging.info("Balance: $%.2f", self._balance_cents / 100.0)
                now_ms = int(time.time() * 1000)
                for ticker, state in [(t, s) for t, s in self._states.items() if s.is_expired(now_ms)]:
                    await self._settle_market(ticker, state)
                    del self._states[ticker]
                    logging.info("Pruned expired market: %s", ticker)
                await self._load_market_states()

        self._trade_log.close()
