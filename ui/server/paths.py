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


def settings_path() -> Path:
    """Shared settings (automation, trading window): TRADINGAGENTS_SETTINGS_PATH, else data_home()/settings.json."""
    override = os.environ.get("TRADINGAGENTS_SETTINGS_PATH")
    return Path(override) if override else data_home() / "settings.json"


def env_path() -> Path:
    """The .env the scripts and research runs read (BATS_ENV_PATH lets tests use a temp file)."""
    override = os.environ.get("BATS_ENV_PATH")
    return Path(override) if override else REPO_ROOT / ".env"


def tickets_dir() -> Path:
    return data_home() / "tickets"


def baskets_dir() -> Path:
    return data_home() / "baskets"


def regimes_dir() -> Path:
    return data_home() / "regimes"


def routes_dir() -> Path:
    return data_home() / "routes"


def scanbot_dir() -> Path:
    """ScanBot reports: financials, long/short sideways channel scans and the rest of the funnel."""
    return data_home() / "scanbot"


def plans_dir() -> Path:
    return data_home() / "plans"


def results_dir() -> Path:
    """TradingAgents' results_dir: TRADINGAGENTS_RESULTS_DIR, else ~/.tradingagents/logs."""
    override = os.environ.get("TRADINGAGENTS_RESULTS_DIR")
    return Path(override) if override else data_home() / "logs"
