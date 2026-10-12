"""Print a read-only snapshot of the Alpaca paper account: open positions and open orders.

Run from the TradingAgents folder:

    python -m paper_orders.snapshot

It loads ALPACA_API_KEY and ALPACA_SECRET_KEY from .env, refuses any key ID that
does not start with "PK", and asks the paper account for two things:

- GET /v2/positions
- GET /v2/orders?status=open&nested=true&limit=500

Nothing is placed, cancelled or changed. This module imports no Alpaca package
and nothing that loads one (not the sender, the UI broker or alpaca_bars), so
Alpaca's trading client is never loaded. It reads the paper REST API with the
standard library, and PaperReader can only make those two GET requests, and
only to the paper URL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Copied from extensions.market_data.alpaca_bars, which can't be imported here: it loads alpaca.data, and
# alpaca.data loads Alpaca's trading client. tests/test_paper_snapshot.py checks these still match.
API_KEY_ENV = "ALPACA_API_KEY"
SECRET_KEY_ENV = "ALPACA_SECRET_KEY"
PAPER_KEY_PREFIX = "PK"

REPO_ROOT = Path(__file__).resolve().parents[1]
PAPER_URL = "https://paper-api.alpaca.markets"
POSITIONS_PATH = "/v2/positions"
OPEN_ORDERS_PATH = "/v2/orders?status=open&nested=true&limit=500"
EXIT_OK, EXIT_BAD_KEYS, EXIT_READ_FAILED = 0, 2, 3


class NotPaperKey(RuntimeError):
    """The Alpaca keys are missing, or the key ID is not a paper key."""


class ReadFailed(RuntimeError):
    """The paper account could not be read."""


# --- the paper reader --------------------------------------------------------

def paper_keys(env: Mapping[str, str]) -> tuple[str, str]:
    """The keys, refused unless both are set and the key ID starts with PK."""
    api_key = (env.get(API_KEY_ENV) or "").strip()
    secret_key = (env.get(SECRET_KEY_ENV) or "").strip()
    missing = [n for n, v in ((API_KEY_ENV, api_key), (SECRET_KEY_ENV, secret_key)) if not v]
    if missing:
        raise NotPaperKey(f"set {' and '.join(missing)} to your Alpaca paper keys in .env")
    if not api_key.startswith(PAPER_KEY_PREFIX):
        raise NotPaperKey(f"{API_KEY_ENV} is not a paper key (paper key IDs start with {PAPER_KEY_PREFIX!r}); "
                          "the snapshot reads the paper account only")
    return api_key, secret_key


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        text = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the status code is enough when the body is unreadable
        return exc.reason or ""
    try:
        return str(json.loads(text).get("message") or text)
    except (ValueError, AttributeError):
        return text[:200]


class PaperReader:
    """The paper account over HTTPS GET. Public surface: positions, open_orders."""

    def __init__(self, api_key: str, secret_key: str,
                 opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 15.0) -> None:
        self.__headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key,
                          "Accept": "application/json"}
        self.__opener = opener
        self.__timeout = timeout

    def positions(self) -> list[dict]:
        return self.__get(POSITIONS_PATH)

    def open_orders(self) -> list[dict]:
        return self.__get(OPEN_ORDERS_PATH)

    def __get(self, path: str) -> list[dict]:
        req = urllib.request.Request(PAPER_URL + path, headers=self.__headers, method="GET")
        try:
            with self.__opener(req, timeout=self.__timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise ReadFailed(f"GET {path}: HTTP {exc.code}: {_error_body(exc)}") from exc
        except OSError as exc:
            raise ReadFailed(f"GET {path}: {exc}") from exc
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ReadFailed(f"GET {path}: the response is not JSON") from exc
        if not isinstance(data, list):
            raise ReadFailed(f"GET {path}: expected a list, got {type(data).__name__}")
        return data


def paper_reader(env: Mapping[str, str], opener: Callable[..., Any] = urllib.request.urlopen) -> PaperReader:
    """A reader on the paper URL, built only from paper keys."""
    api_key, secret_key = paper_keys(env)
    return PaperReader(api_key, secret_key, opener=opener)


def snapshot(reader: Any) -> dict[str, list[dict]]:
    """Open positions and open orders, as the paper account returns them."""
    return {"positions": reader.positions(), "open_orders": reader.open_orders()}


# --- printing ----------------------------------------------------------------

def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, spec: str = ",.2f") -> str:
    n = _num(value)
    return "-" if n is None else format(n, spec)


def _qty(value: Any) -> str:
    n = _num(value)
    if n is None:
        return "-"
    return str(int(n)) if n == int(n) else f"{n:g}"


def _position_line(p: dict) -> str:
    plpc = _num(p.get("unrealized_plpc"))
    pct = f" ({plpc * 100:+.2f}%)" if plpc is not None else ""
    return (f"  {p.get('symbol') or '?':<6}{p.get('side') or '?':<6}qty {_qty(p.get('qty')):>7}  "
            f"avg {_fmt(p.get('avg_entry_price')):>10}  last {_fmt(p.get('current_price')):>10}  "
            f"value {_fmt(p.get('market_value')):>12}  P/L {_fmt(p.get('unrealized_pl'), '+,.2f')}{pct}")


def _order_line(o: dict, indent: str) -> str:
    prices = []
    if o.get("limit_price") is not None:
        prices.append(f"limit {_fmt(o['limit_price'])}")
    if o.get("stop_price") is not None:
        prices.append(f"stop {_fmt(o['stop_price'])}")
    order_class = o.get("order_class") or "simple"
    kind = o.get("type") or o.get("order_type") or "?"
    text = (f"{indent}{o.get('symbol') or '?':<6}{o.get('side') or '?':<5}{kind:<14}"
            f"qty {_qty(o.get('qty'))}, filled {_qty(o.get('filled_qty') or 0)}  "
            f"{' '.join(prices) or 'no price'}  {o.get('time_in_force') or '?'}  {o.get('status') or '?'}")
    if order_class != "simple":
        text += f"  {order_class}"
    return text + f"  id {o.get('client_order_id') or o.get('id') or '?'}"


def _print_positions(rows: list[dict]) -> None:
    print(f"open positions: {len(rows)}")
    if not rows:
        print("  none")
    for p in sorted(rows, key=lambda p: p.get("symbol") or ""):
        print(_position_line(p))


def _print_orders(rows: list[dict]) -> None:
    print(f"open orders: {len(rows)}")
    if not rows:
        print("  none")
    for o in rows:
        print(_order_line(o, "  "))
        for leg in o.get("legs") or []:
            print(_order_line(leg, "      leg "))


def _parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description="Print the Alpaca paper account's open positions and open orders. Read-only.")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         make_reader: Callable[[Mapping[str, str]], Any] = paper_reader) -> int:
    _parser().parse_args(argv)
    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    try:
        reader = make_reader(env)
    except NotPaperKey as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_BAD_KEYS

    print(f"Alpaca paper account ({PAPER_URL}), read-only snapshot")
    try:
        snap = snapshot(reader)
    except ReadFailed as exc:
        print(f"error: could not read the paper account: {exc}", file=sys.stderr)
        return EXIT_READ_FAILED
    _print_positions(snap["positions"])
    _print_orders(snap["open_orders"])
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
