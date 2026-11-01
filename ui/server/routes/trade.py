"""Trade page order entry: write manual paper tickets, and start paper_orders.sender on one of them.

No route here talks to the broker. Sending starts ``python -m paper_orders.sender --manual <name>`` as a
job, with ``--send`` only when the page confirmed it; the sender checks the paper key, the paper URL and the
trading window, and never sends one client order ID twice. Run it again later to place the OCO exit once
the entry has filled.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ui.server import jobs, manual_tickets, paths
from ui.server.routes import common

router = APIRouter(prefix="/api/trade", tags=["trade"])


class TicketBody(BaseModel):
    item_id: str  # a row of the trade queue: symbol, side and date come from it
    entry: float
    stop: float
    target: float
    quantity: int


class SendBody(BaseModel):
    send: bool = False  # False: a dry run that prints the orders and sends nothing
    # The page asks before a real send; one without it is refused.
    confirm: bool = False


@router.get("/manual")
def list_tickets() -> list[dict]:
    return manual_tickets.list_all()


@router.post("/manual")
def create_ticket(body: TicketBody) -> dict:
    try:
        return manual_tickets.create(body.item_id, body.entry, body.stop, body.target, body.quantity)
    except (manual_tickets.TicketError, ValueError) as exc:
        raise common.bad_request(str(exc)) from None


@router.delete("/manual/{name}")
def delete_ticket(name: str) -> dict:
    try:
        manual_tickets.delete(name)
    except manual_tickets.TicketError as exc:
        raise common.bad_request(str(exc)) from None
    return {"deleted": name}


@router.post("/manual/{name}/send")
def send_ticket(name: str, body: SendBody) -> dict:
    try:
        manual_tickets.check_name(name)
    except manual_tickets.TicketError as exc:
        raise common.bad_request(str(exc)) from None
    if body.send and not body.confirm:
        raise common.bad_request("sending places a paper order; confirm the send first")
    args = ["--manual", name, "--ticket-dir", str(paths.tickets_dir())] + (["--send"] if body.send else [])
    job = common.start("trade:manual-send" if body.send else "trade:manual-preview",
                       [jobs.Step(jobs.SENDER, args)], group="sender")
    if body.send:
        manual_tickets.record_send(name, job["id"])
    return job
