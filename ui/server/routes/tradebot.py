"""Trade page: the Tradebot queue, and jobs that check, ticket, preview and send it.

No route here talks to the broker. Each action starts the same command the scheduler starts, as a job:

- check: ``run_tradebot --stage eod|intraday`` (one item with --item),
- ticket: ``run_tradebot --ticket-only`` writes tickets for the triggered items,
- preview: ``run_tradebot --ticket-only --dry-run`` (levels and size, nothing written), then
  ``paper_orders.sender --tradebot --preview`` (the risk guard on the ticketed items; reads the account by GET),
- send: ``paper_orders.sender --tradebot --send`` only on a confirmed request; without send it is the preview.

Removing an item is DELETE /api/queues/trade/{id} (routes/queues.py), which keeps a ticketed or sent item on
record as removed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel

from extensions.tradebot import store
from paper_orders import market_calendar
from paper_orders.trade_window import MARKET_TZ
from ui.server import jobs
from ui.server.routes import common

router = APIRouter(prefix="/api/tradebot", tags=["tradebot"])
STAGES = ("eod", "intraday")


class CheckBody(BaseModel):
    stage: str
    item: str | None = None


class ItemBody(BaseModel):
    item: str | None = None


class SendBody(BaseModel):
    send: bool = False  # False: the guard preview, nothing sent
    confirm: bool = False  # the page asks before a real send; one without it is refused
    item: str | None = None


def sessions_left(expires_after: str | None, today: date) -> int | None:
    """Trading sessions from today through the item's last session (0 once it is past)."""
    try:
        last = date.fromisoformat(expires_after or "")
    except ValueError:
        return None
    n, d = 0, today
    while d <= last and n < 400:
        n += market_calendar.is_trading_day(d)
        d += timedelta(days=1)
    return n


def _item_args(item: str | None) -> list[str]:
    if item is None:
        return []
    try:
        store.find(store.read(), item)
    except store.StoreError as exc:
        raise common.bad_request(str(exc)) from None
    return ["--item", item]


@router.get("/queue")
def queue() -> list[dict]:
    try:
        items = store.read()["trade"]
    except store.StoreError as exc:
        raise common.bad_request(str(exc)) from None
    today = datetime.now(UTC).astimezone(MARKET_TZ).date()
    out = []
    for item in reversed(items):
        last = (item.get("checks") or [None])[-1]
        out.append({**item, "sessions_left": sessions_left(item.get("expires_after"), today), "last_check": last})
    return out


@router.post("/check")
def check(body: CheckBody) -> dict:
    if body.stage not in STAGES:
        raise common.bad_request(f"stage must be one of {', '.join(STAGES)}")
    args = ["--stage", body.stage, *_item_args(body.item)]
    return common.start(f"tradebot:check-{body.stage}", [jobs.Step("run_tradebot", args)], group="pipeline")


@router.post("/ticket")
def ticket(body: ItemBody) -> dict:
    args = ["--ticket-only", *_item_args(body.item)]
    return common.start("tradebot:ticket", [jobs.Step("run_tradebot", args)], group="pipeline")


@router.post("/preview")
def preview(body: ItemBody) -> dict:
    item = _item_args(body.item)
    steps = [jobs.Step("run_tradebot", ["--ticket-only", "--dry-run", *item]),
             jobs.Step(jobs.SENDER, ["--tradebot", "--preview", *item], always=True)]
    return common.start("tradebot:preview", steps, group="sender")


@router.post("/send")
def send(body: SendBody) -> dict:
    if body.send and not body.confirm:
        raise common.bad_request("sending places paper orders; confirm the send first")
    args = ["--tradebot", "--send" if body.send else "--preview", *_item_args(body.item)]
    return common.start("tradebot:send" if body.send else "tradebot:send-preview", [jobs.Step(jobs.SENDER, args)],
                        group="sender")
