"""Where the UI finds the repo and the files the extension scripts write."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIST = REPO_ROOT / "ui" / "web" / "dist"

# Tests point this at a temp folder; normally it is ~/.tradingagents, the
# default every extension script and TradingAgents itself writes under.
HOME_ENV = "TRADINGAGENTS_UI_HOME"


def data_home() -> Path:
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".tradingagents")


def baskets_dir() -> Path:
    return data_home() / "baskets"


def regimes_dir() -> Path:
    return data_home() / "regimes"


def routes_dir() -> Path:
    return data_home() / "routes"


def plans_dir() -> Path:
    return data_home() / "plans"


def results_dir() -> Path:
    """TradingAgents' results_dir: TRADINGAGENTS_RESULTS_DIR, else ~/.tradingagents/logs."""
    override = os.environ.get("TRADINGAGENTS_RESULTS_DIR")
    return Path(override) if override else data_home() / "logs"
