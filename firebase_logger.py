"""Firebase Firestore logger for trade results."""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.request
from datetime import datetime, timezone
from typing import Any


def _get_instance_id() -> str:
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/id",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.read().decode()
    except Exception:
        return "local"


def _asset_type(ticker: str) -> str:
    m = re.match(r"KX([A-Z]+)\d", ticker)
    return m.group(1).lower() if m else "unknown"


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


class FirebaseLogger:
    def __init__(self, credentials_path: str | None, project_id: str | None = None):
        self._db = None
        self._instance_id = _get_instance_id()

        if not credentials_path:
            logging.info("Firebase logging disabled (FIREBASE_CREDENTIALS_PATH not set)")
            return
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
            cred = credentials.Certificate(credentials_path)
            opts: dict[str, Any] = {}
            if project_id:
                opts["projectId"] = project_id
            firebase_admin.initialize_app(cred, opts)
            self._db = firestore.client()
            logging.info("Firebase logger ready (instance=%s)", self._instance_id)
        except ImportError:
            logging.warning("firebase-admin not installed; Firebase logging disabled")
        except Exception as exc:
            logging.error("Firebase init failed: %s", exc)

    @property
    def enabled(self) -> bool:
        return self._db is not None

    async def log_bet(
        self,
        ticker: str,
        market_open_ms: int | None,
        side: str,
        ask_price: float,
        win_pct: float,
        edge: float,
        contract_count: int,
        sigma: float,
        bet_time_ms: int,
    ) -> str | None:
        if not self._db:
            return None

        stake = round(contract_count * ask_price, 4)
        doc: dict[str, Any] = {
            "marketTicker": ticker,
            "instanceId": self._instance_id,
            "assetType": _asset_type(ticker),
            "marketOpen": _dt(market_open_ms) if market_open_ms else None,
            "side": side.upper(),
            "askPrice": round(ask_price, 4),
            "winPct": round(win_pct, 4),
            "edge": round(edge, 4),
            "contractCount": contract_count,
            "stake": stake,
            "isOpen": True,
            "soldEarly": None,
            "contractReturn": None,
            "stakeReturn": None,
            "contractProfit": None,
            "stakeProfit": None,
            "returnPct": None,
            "sigma": round(sigma, 4),
            "betTime": _dt(bet_time_ms),
            "closeTime": None,
        }

        db = self._db
        try:
            loop = asyncio.get_event_loop()
            _, ref = await loop.run_in_executor(None, lambda: db.collection("trades").add(doc))
            logging.info("Firebase: logged %s %s → %s", side.upper(), ticker, ref.id)
            return ref.id
        except Exception as exc:
            logging.error("Firebase log_bet failed: %s", exc)
            return None

    async def update_cashout(
        self,
        doc_id: str,
        sell_price: float,
        contract_count: int,
        ask_price: float,
        close_time_ms: int,
    ) -> None:
        if not self._db:
            return
        stake = round(contract_count * ask_price, 4)
        stake_return = round(contract_count * sell_price, 4)
        contract_profit = round(sell_price - ask_price, 4)
        stake_profit = round(stake_return - stake, 4)
        updates: dict[str, Any] = {
            "isOpen": False,
            "soldEarly": True,
            "contractReturn": round(sell_price, 4),
            "stakeReturn": stake_return,
            "contractProfit": contract_profit,
            "stakeProfit": stake_profit,
            "returnPct": round(stake_profit / stake, 4) if stake else 0.0,
            "closeTime": _dt(close_time_ms),
        }
        await self._update(doc_id, updates, "cashout")

    async def update_settlement(
        self,
        doc_id: str,
        won: bool,
        contract_count: int,
        ask_price: float,
        close_time_ms: int,
    ) -> None:
        if not self._db:
            return
        contract_return = 1.0 if won else 0.0
        stake = round(contract_count * ask_price, 4)
        stake_return = round(contract_count * contract_return, 4)
        contract_profit = round(contract_return - ask_price, 4)
        stake_profit = round(stake_return - stake, 4)
        updates: dict[str, Any] = {
            "isOpen": False,
            "soldEarly": False,
            "contractReturn": contract_return,
            "stakeReturn": stake_return,
            "contractProfit": contract_profit,
            "stakeProfit": stake_profit,
            "returnPct": round(stake_profit / stake, 4) if stake else 0.0,
            "closeTime": _dt(close_time_ms),
        }
        await self._update(doc_id, updates, "settlement")

    async def _update(self, doc_id: str, updates: dict[str, Any], label: str) -> None:
        db = self._db
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: db.collection("trades").document(doc_id).update(updates)
            )
            logging.info("Firebase: %s updated for %s", label, doc_id)
        except Exception as exc:
            logging.error("Firebase %s update failed for %s: %s", label, doc_id, exc)
