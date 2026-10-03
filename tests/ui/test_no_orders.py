"""The UI can read the paper account but has no way to place, change, cancel or close an order."""

import ast
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from ui.server import broker  # noqa: E402
from ui.server.app import app  # noqa: E402

UI = Path(__file__).resolve().parents[2] / "ui"
ORDER_CALLS = re.compile(
    r"submit_order|close_position|close_all_positions|cancel_order|replace_order|OrderRequest|exercise_options"
)


def _source_files():
    for pattern in ("server/**/*.py", "web/src/**/*.ts", "web/src/**/*.tsx"):
        yield from UI.glob(pattern)


@pytest.mark.unit
def test_no_ui_source_names_an_order_call():
    offenders = [
        f"{path.relative_to(UI)}:{n}: {line.strip()}"
        for path in _source_files()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if ORDER_CALLS.search(line)
    ]
    assert offenders == []


@pytest.mark.unit
def test_only_broker_module_imports_the_trading_client():
    importers = []
    for path in UI.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            if any(m.startswith("alpaca.trading") for m in modules):
                importers.append(path.relative_to(UI).as_posix())
    assert set(importers) == {"server/broker.py"}


@pytest.mark.unit
def test_read_only_broker_exposes_only_read_methods():
    public = {name for name in dir(broker.ReadOnlyBroker) if not name.startswith("_")}
    assert public == {"account", "portfolio_history", "positions", "orders"}


@pytest.mark.unit
def test_broker_routes_are_all_get():
    broker_paths = {p: ops for p, ops in app.openapi()["paths"].items() if p.startswith("/api/broker")}
    assert set(broker_paths) == {"/api/broker/account", "/api/broker/history",
                                 "/api/broker/positions", "/api/broker/orders"}
    assert {method for ops in broker_paths.values() for method in ops} == {"get"}


@pytest.mark.unit
def test_live_keys_are_refused():
    with pytest.raises(broker.MissingAlpacaKeys):
        broker.ReadOnlyBroker(env={"ALPACA_API_KEY": "AKLIVE123", "ALPACA_SECRET_KEY": "s"})
    with pytest.raises(broker.MissingAlpacaKeys):
        broker.ReadOnlyBroker(env={})
