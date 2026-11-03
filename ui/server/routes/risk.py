"""Risk page data, the kill switch, and the flatten job.

No route here talks to the broker. The kill switch is a setting (the "risk" section of settings.json) that
``paper_orders.guard`` reads before every entry; turning it on stops every entry from the next sender run on.
Flatten turns the kill switch on first, then starts ``python -m paper_orders.flatten`` as a job, with
``--send`` only on a confirmed request; that job cancels every open order and closes every position on the
paper account. It runs in its own job group, so a running sender job never holds it up.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from paper_orders import risk_state
from paper_orders.trade_window import SettingsError
from ui.server import jobs, settings
from ui.server.routes import common

router = APIRouter(prefix="/api/risk", tags=["risk"])


class KillSwitchBody(BaseModel):
    on: bool
    reason: str = ""


class FlattenBody(BaseModel):
    send: bool = False  # False: a dry run that lists what would be cancelled and closed
    confirm: bool = False  # the page asks first; a send without it is refused
    reason: str = "flatten from the UI"


def _set_kill_switch(on: bool, reason: str) -> dict:
    section = settings.risk()
    section.update(kill_switch=on, kill_switch_at=datetime.now(UTC).isoformat(timespec="seconds"),
                   kill_switch_reason=reason or ("turned on from the UI" if on else "turned off from the UI"))
    return settings.save_section("risk", section)


@router.get("")
def get_risk() -> dict:
    try:
        risk, error = settings.risk(), None
    except SettingsError as exc:
        risk, error = None, str(exc)
    try:
        state, state_error = risk_state.load(), None
    except risk_state.RiskStateError as exc:
        state, state_error = None, str(exc)
    return {"risk": risk, "error": error, "state": state, "state_error": state_error,
            "kill_switch": bool(risk and risk["kill_switch"]) if risk else None}


@router.put("/kill-switch")
def put_kill_switch(body: KillSwitchBody) -> dict:
    try:
        return _set_kill_switch(body.on, body.reason.strip())
    except SettingsError as exc:
        raise common.bad_request(str(exc)) from None


@router.post("/flatten")
def flatten(body: FlattenBody) -> dict:
    if body.send and not body.confirm:
        raise common.bad_request("flatten cancels every open order and closes every position; confirm it first")
    reason = body.reason.strip() or "flatten from the UI"
    if body.send:
        try:
            _set_kill_switch(True, reason)
        except SettingsError as exc:
            raise common.bad_request(f"cannot turn the kill switch on: {exc}; nothing changed") from None
    args = ["--reason", reason] + (["--send"] if body.send else [])
    return common.start("risk:flatten" if body.send else "risk:flatten-preview", [jobs.Step(jobs.FLATTEN, args)],
                        group="flatten")
