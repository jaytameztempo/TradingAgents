"""Check this machine is ready to run BATS unattended on the Alpaca paper account. Read-only.

Run from the TradingAgents folder (scripts/start_bats.ps1 runs it before every start and logs the result):

    python -m extensions.scripts.check_setup

Each line is PASS, WARN or FAIL:

- paper keys: ALPACA_API_KEY and ALPACA_SECRET_KEY are set and the key ID starts with PK,
- paper account: GET /v2/account answers (paper URL only, paper keys only),
- trading window: one is saved on the Settings page (without it auto mode stays locked),
- risk settings and tradebot settings: the saved sections are valid (an invalid one blocks every entry),
- data folder: not inside a OneDrive-synced folder (FAIL: the scheduler and the Tradebot refuse to run there),
- repo and .venv: not inside a OneDrive-synced folder (WARN: sync can lock files the server is importing),
- scheduler state: scheduler_state.json, when present, is readable.

Exit code 1 when any line is FAIL, else 0. Nothing is written and no order is placed; Alpaca's trading client is
never loaded.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from extensions.tradebot import config as tradebot_config, store
from paper_orders import guard
from paper_orders.snapshot import NotPaperKey, ReadFailed, paper_keys, paper_reader
from paper_orders.trade_window import SettingsError, load_settings, settings_path

REPO_ROOT = Path(__file__).resolve().parents[2]
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True)
class Line:
    result: str
    name: str
    detail: str


def check(env: Mapping[str, str], make_reader: Callable[[Mapping[str, str]], Any] = paper_reader,
          repo_root: Path = REPO_ROOT) -> list[Line]:
    out: list[Line] = []

    try:
        paper_keys(env)
        out.append(Line(PASS, "paper keys", "set; the key ID starts with PK (paper)"))
        keys_ok = True
    except NotPaperKey as exc:
        out.append(Line(FAIL, "paper keys", str(exc)))
        keys_ok = False
    if keys_ok:
        try:
            account = make_reader(env).account()
            out.append(Line(PASS, "paper account", f"equity {account.get('equity')}, status {account.get('status')}"))
        except (NotPaperKey, ReadFailed) as exc:
            out.append(Line(FAIL, "paper account", str(exc)))
    else:
        out.append(Line(FAIL, "paper account", "not checked: no paper keys"))

    try:
        saved = load_settings()
        settings_ok = True
    except SettingsError as exc:
        out.append(Line(FAIL, "settings file", str(exc)))
        saved, settings_ok = {}, False
    if settings_ok:
        out.append(Line(PASS if saved.get("trading") else WARN, "trading window",
                        "saved" if saved.get("trading") else
                        f"none saved in {settings_path()}: the sender is not gated by time and auto mode is locked"))
        for name, validate in (("risk settings", guard.validate_risk), ("tradebot settings", tradebot_config.validate)):
            section = name.split()[0]
            try:
                value = validate(saved.get(section) or {})
                extra = f"kill switch {'ON' if value.get('kill_switch') else 'off'}" if section == "risk" else \
                    f"mode {value['mode']}"
                out.append(Line(PASS, name, extra))
            except SettingsError as exc:
                out.append(Line(FAIL, name, f"{exc}; every entry is blocked until it is fixed"))

    where = store.synced_folder()
    out.append(Line(FAIL, "data folder", f"{store.data_home()} is inside {where}; the scheduler and the Tradebot "
                                         "refuse to run") if where else
               Line(PASS, "data folder", f"{store.data_home()} (not synced)"))
    for name, path in (("repo", repo_root), (".venv", repo_root / ".venv")):
        synced = store.synced_folder(path)
        out.append(Line(WARN, name, f"{path} is inside {synced}; sync can lock files the server imports. Move the "
                                    "repo out of OneDrive") if synced else Line(PASS, name, f"{path} (not synced)"))

    state = settings_path().with_name("scheduler_state.json")
    if state.exists():
        try:
            json.loads(state.read_text(encoding="utf-8"))
            out.append(Line(PASS, "scheduler state", str(state)))
        except (OSError, ValueError) as exc:
            out.append(Line(WARN, "scheduler state", f"{state} cannot be read ({exc}); the scheduler starts fresh"))
    else:
        out.append(Line(PASS, "scheduler state", "none yet (first start)"))
    return out


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None,
         make_reader: Callable[[Mapping[str, str]], Any] = paper_reader) -> int:
    if env is None:
        load_dotenv(REPO_ROOT / ".env")
        env = os.environ
    lines = check(env, make_reader)
    for line in lines:
        print(f"{line.result:<5} {line.name:<18} {line.detail}")
    fails = sum(1 for line in lines if line.result == FAIL)
    warns = sum(1 for line in lines if line.result == WARN)
    print(f"setup check: {fails} fail, {warns} warn")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
