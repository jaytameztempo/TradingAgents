"""Paper account, read-only. Every route here is a GET; there is no order, close or cancel endpoint."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException

from extensions.market_data.alpaca_bars import MissingAlpacaKeys
from ui.server import broker

router = APIRouter(prefix="/api/broker", tags=["broker"])


def _read(call: Callable[[broker.ReadOnlyBroker], Any]) -> Any:
    try:
        return call(broker.get_broker())
    except MissingAlpacaKeys as exc:
        raise HTTPException(status_code=503, detail={"code": "missing_keys", "message": str(exc)}) from None
    except Exception as exc:  # network or Alpaca API failure: show it, never retry an action
        raise HTTPException(status_code=502, detail={"code": "broker_error", "message": str(exc)}) from None


@router.get("/account")
def account() -> dict:
    return _read(lambda b: b.account())


@router.get("/history")
def history(range: str = "1M") -> dict:  # noqa: A002 - query parameter name
    if range not in broker.HISTORY_RANGES:
        raise HTTPException(status_code=400, detail=f"range must be one of {', '.join(broker.HISTORY_RANGES)}")
    return _read(lambda b: b.portfolio_history(range))


@router.get("/positions")
def positions() -> list[dict]:
    return _read(lambda b: b.positions())


@router.get("/orders")
def orders(limit: int = 200) -> list[dict]:
    return _read(lambda b: b.orders(limit))
