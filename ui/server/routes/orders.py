"""The order ledger and the reconcile report, read from disk, and a route that starts a reconcile.

No route here talks to the broker. Reconcile runs as a job (``python -m paper_orders.reconcile``), which reads
the paper account by GET only and writes its report under <data home>/status.
"""

from __future__ import annotations

import re

from fastapi import APIRouter

from paper_orders import ledger, reconcile
from ui.server import jobs
from ui.server.routes import common

router = APIRouter(prefix="/api/orders", tags=["orders"])
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@router.get("/ledger")
def get_ledger(month: str | None = None) -> dict:
    if month is not None and not MONTH.fullmatch(month):
        raise common.bad_request("month must be YYYY-MM")
    try:
        lines = ledger.read(month)
    except ledger.LedgerError as exc:
        raise common.bad_request(str(exc)) from None
    return {"month": month, "lines": list(reversed(lines))}


@router.get("/reconcile")
def get_reconcile() -> dict:
    found = reconcile.latest()
    if found is None:
        return {"file": None, "report": None}
    return {"file": found[0].name, "report": found[1]}


@router.post("/reconcile/run")
def run_reconcile() -> dict:
    return common.start("orders:reconcile", [jobs.Step(jobs.RECONCILE, [])], group="reconcile")
