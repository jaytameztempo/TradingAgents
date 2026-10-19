"""Scans page "Send to": research / strategy / trade queues, and permanent Trash."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ui.server import queues

router = APIRouter(prefix="/api", tags=["queues"])


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


class SendBody(BaseModel):
    kind: str  # up, down, long or short
    file: str
    tickers: list[str] = []  # empty: the whole basket


@router.get("/queues")
def get_queues() -> dict:
    return queues.list_queues()


@router.post("/queues/{queue}")
def send_to(queue: str, body: SendBody) -> dict:
    try:
        return queues.send(queue, body.kind, body.file, [t.strip().upper() for t in body.tickers])
    except queues.QueueError as exc:
        raise _bad(exc) from None


@router.delete("/queues/{queue}/{item_id}")
def remove_from(queue: str, item_id: str) -> dict:
    try:
        return {"removed": queues.remove_item(queue, item_id)}
    except queues.QueueError as exc:
        raise _bad(exc) from None


class TrashBody(BaseModel):
    kind: str
    file: str
    ticker: str | None = None  # None: delete the whole scan file


@router.post("/scans/trash")
def trash(body: TrashBody) -> dict:
    """Permanent: deletes the scan file, or removes one ticker from it. The page asks first."""
    try:
        if body.ticker:
            return queues.remove_ticker(body.kind, body.file, body.ticker.strip().upper())
        queues.delete_scan(body.kind, body.file)
        return {"deleted": body.file}
    except queues.QueueError as exc:
        raise _bad(exc) from None
