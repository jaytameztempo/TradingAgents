"""Alpaca's asset list, read-only, with a plain HTTP GET to the paper host.

The asset master (``GET /v2/assets``) is served by Alpaca's trading API host,
not the market-data host. This module does not import alpaca-py at all: it
sends one GET to the paper host and nothing else, so it cannot place, change
or cancel an order. Only paper keys are accepted, as for the bar fetcher.

The list is today's listings, not a point-in-time record: a snapshot is saved
with the time it was fetched so a report can say how old its universe is.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import requests

from extensions.market_data.alpaca_bars import (
    API_KEY_ENV,
    PAPER_KEY_PREFIX,
    SECRET_KEY_ENV,
    AlpacaDataError,
    MissingAlpacaKeys,
)

PAPER_HOST = "https://paper-api.alpaca.markets"
ASSETS_PATH = "/v2/assets"
TIMEOUT_SECONDS = 60
SNAPSHOT_VERSION = 1


def paper_headers(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Auth headers from paper keys in the environment. Error messages name the variables, never their values."""
    env = os.environ if env is None else env
    api_key = (env.get(API_KEY_ENV) or "").strip()
    secret_key = (env.get(SECRET_KEY_ENV) or "").strip()
    missing = [name for name, value in ((API_KEY_ENV, api_key), (SECRET_KEY_ENV, secret_key)) if not value]
    if missing:
        raise MissingAlpacaKeys(f"set {' and '.join(missing)} to your Alpaca paper keys (in .env, which git ignores)")
    if not api_key.startswith(PAPER_KEY_PREFIX):
        raise MissingAlpacaKeys(
            f"{API_KEY_ENV} is not a paper key (paper key IDs start with {PAPER_KEY_PREFIX!r}); "
            "this project reads data with paper keys only"
        )
    return {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key, "Accept": "application/json"}


def fetch_assets(env: Mapping[str, str] | None = None, session=None) -> list[dict]:
    """Every asset Alpaca lists, unfiltered, so each universe gate can count what it removes."""
    headers = paper_headers(env)
    http = session or requests
    url = PAPER_HOST + ASSETS_PATH
    try:
        response = http.get(url, headers=headers, timeout=TIMEOUT_SECONDS)
    except requests.exceptions.RequestException as exc:
        raise AlpacaDataError(f"Alpaca assets request failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise AlpacaDataError(f"Alpaca assets request failed ({response.status_code}): {response.text[:200]}")
    try:
        assets = response.json()
    except ValueError as exc:
        raise AlpacaDataError("Alpaca assets response was not JSON") from exc
    if not isinstance(assets, list) or not all(isinstance(a, dict) for a in assets):
        raise AlpacaDataError("Alpaca assets response was not a list of assets")
    return assets


def default_assets_dir() -> Path:
    """~/.tradingagents/assets, beside the baskets and out of git."""
    return Path.home() / ".tradingagents" / "assets"


def save_snapshot(
    assets: list[dict], out_dir: str | Path | None = None, fetched_at: datetime | None = None
) -> Path:
    """Write the asset list with its fetch time. An existing snapshot is never overwritten."""
    fetched_at = fetched_at or datetime.now(UTC)
    folder = Path(out_dir) if out_dir is not None else default_assets_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"assets_{fetched_at:%Y%m%dT%H%M%SZ}.json"
    data = {
        "snapshot_version": SNAPSHOT_VERSION,
        "fetched_at": fetched_at.isoformat(),
        "source": PAPER_HOST + ASSETS_PATH,
        "count": len(assets),
        "assets": assets,
    }
    with path.open("x", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.write("\n")
    return path


def load_snapshot(path: str | Path) -> tuple[datetime, list[dict]]:
    """Read a saved snapshot back as (fetched_at, assets)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("snapshot_version") != SNAPSHOT_VERSION:
        raise ValueError(f"unsupported asset snapshot_version: {data.get('snapshot_version')!r}")
    assets = data["assets"]
    if not isinstance(assets, list) or not all(isinstance(a, dict) for a in assets):
        raise ValueError("asset snapshot does not hold a list of assets")
    return datetime.fromisoformat(data["fetched_at"]), assets
