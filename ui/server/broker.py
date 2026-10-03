"""Read-only view of the Alpaca paper account: balances, history, positions, orders.

This is the only UI module that imports ``alpaca.trading``. ReadOnlyBroker keeps
the TradingClient private and exposes four read methods, so nothing here can
place, change, cancel or close anything. The paper-key rule is the one
extensions/market_data/alpaca_bars.py already enforces: key IDs must start with "PK".
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
from dotenv import load_dotenv

from extensions.market_data.alpaca_bars import (
    API_KEY_ENV,
    PAPER_KEY_PREFIX,
    SECRET_KEY_ENV,
    MissingAlpacaKeys,
)
from ui.server.paths import REPO_ROOT

# Range picker on the Dashboard chart -> (Alpaca period, timeframe).
HISTORY_RANGES = {
    "1D": ("1D", "5Min"),
    "1W": ("1W", "1H"),
    "1M": ("1M", "1D"),
    "1Y": ("1A", "1D"),
}

# How an order's position_intent maps to the side of the position it touches.
_INTENT_SIDE = {
    "buy_to_open": "Long",
    "sell_to_close": "Long",
    "sell_to_open": "Short",
    "buy_to_close": "Short",
}


def _paper_keys(env: Mapping[str, str]) -> tuple[str, str]:
    api_key = (env.get(API_KEY_ENV) or "").strip()
    secret_key = (env.get(SECRET_KEY_ENV) or "").strip()
    missing = [n for n, v in ((API_KEY_ENV, api_key), (SECRET_KEY_ENV, secret_key)) if not v]
    if missing:
        raise MissingAlpacaKeys(f"set {' and '.join(missing)} to your Alpaca paper keys in .env")
    if not api_key.startswith(PAPER_KEY_PREFIX):
        raise MissingAlpacaKeys(
            f"{API_KEY_ENV} is not a paper key (paper key IDs start with {PAPER_KEY_PREFIX!r}); "
            "the UI reads the paper account only"
        )
    return api_key, secret_key


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


class ReadOnlyBroker:
    """The paper account, read-only. Public surface: account, portfolio_history, positions, orders."""

    def __init__(self, env: Mapping[str, str] | None = None, client: Any = None) -> None:
        if client is None:
            api_key, secret_key = _paper_keys(os.environ if env is None else env)
            client = TradingClient(api_key, secret_key, paper=True, raw_data=True)
        self.__client = client

    def account(self) -> dict:
        raw = self.__client.get_account()
        equity, last_equity = _num(raw.get("equity")), _num(raw.get("last_equity"))
        change = equity - last_equity if equity is not None and last_equity is not None else None
        change_pct = change / last_equity * 100 if change is not None and last_equity else None
        return {
            "account_number": raw.get("account_number"),
            "status": raw.get("status"),
            "currency": raw.get("currency"),
            "paper": True,
            "equity": equity,
            "last_equity": last_equity,
            "buying_power": _num(raw.get("buying_power")),
            "cash": _num(raw.get("cash")),
            "portfolio_value": _num(raw.get("portfolio_value")),
            "daily_change": change,
            "daily_change_pct": change_pct,
        }

    def portfolio_history(self, range_key: str = "1M") -> dict:
        period, timeframe = HISTORY_RANGES[range_key]
        raw = self.__client.get_portfolio_history(
            GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
        )
        stamps = raw.get("timestamp") or []
        equity = raw.get("equity") or []
        pnl = raw.get("profit_loss") or []
        pnl_pct = raw.get("profit_loss_pct") or []
        points = [
            {
                "t": stamps[i],
                "equity": _num(equity[i]) if i < len(equity) else None,
                "pnl": _num(pnl[i]) if i < len(pnl) else None,
                "pnl_pct": _num(pnl_pct[i]) * 100 if i < len(pnl_pct) and _num(pnl_pct[i]) is not None else None,
            }
            for i in range(len(stamps))
        ]
        return {"range": range_key, "timeframe": raw.get("timeframe", timeframe),
                "base_value": _num(raw.get("base_value")), "points": points}

    def positions(self) -> list[dict]:
        rows = []
        for p in self.__client.get_all_positions():
            plpc = _num(p.get("unrealized_plpc"))
            intraday_plpc = _num(p.get("unrealized_intraday_plpc"))
            rows.append({
                "symbol": p.get("symbol"),
                "side": "Long" if p.get("side") == "long" else "Short",
                "qty": _num(p.get("qty")),
                "avg_entry_price": _num(p.get("avg_entry_price")),
                "market_value": _num(p.get("market_value")),
                "current_price": _num(p.get("current_price")),
                "today_pl": _num(p.get("unrealized_intraday_pl")),
                "today_pl_pct": intraday_plpc * 100 if intraday_plpc is not None else None,
                "total_pl": _num(p.get("unrealized_pl")),
                "total_pl_pct": plpc * 100 if plpc is not None else None,
            })
        return rows

    def orders(self, limit: int = 200) -> list[dict]:
        raw = self.__client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.ALL, limit=max(1, min(limit, 500)))
        )
        return [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "position_side": _INTENT_SIDE.get(o.get("position_intent") or ""),
                "side": (o.get("side") or "").capitalize() or None,
                "qty": _num(o.get("qty")),
                "filled_qty": _num(o.get("filled_qty")),
                "filled_avg_price": _num(o.get("filled_avg_price")),
                "status": o.get("status"),
                "filled": o.get("status") == "filled",
                "type": o.get("type") or o.get("order_type"),
                "submitted_at": o.get("submitted_at"),
                "filled_at": o.get("filled_at"),
            }
            for o in raw
        ]


_broker: ReadOnlyBroker | None = None


def get_broker() -> ReadOnlyBroker:
    """The shared broker, built on first use from .env. Raises MissingAlpacaKeys without paper keys."""
    global _broker
    if _broker is None:
        load_dotenv(REPO_ROOT / ".env")
        _broker = ReadOnlyBroker()
    return _broker
