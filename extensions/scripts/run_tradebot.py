"""Check the Tradebot queue's entry triggers, and ticket what triggered. It never sends an order.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_tradebot --stage eod                   # after the close
    python -m extensions.scripts.run_tradebot --stage intraday              # during the session
    python -m extensions.scripts.run_tradebot --stage eod --ticket          # and ticket what triggered
    python -m extensions.scripts.run_tradebot --stage eod --ticket --dry-run --json   # preview; writes nothing
    python -m extensions.scripts.run_tradebot --ticket-only --dry-run       # preview tickets for triggered items
    python -m extensions.scripts.run_tradebot --stage eod --item 0123456789ab

The eod stage checks waiting and armed items on the completed session (--as-of, default: today after 16:15 New
York, else the last session); the intraday stage checks armed items on today's complete 4-hour and 1-hour bars
and does not run on a weekend, holiday or early-close day. See extensions.tradebot.triggers for the gates and
triggers and extensions.tradebot.ticketing for levels and size. Tickets are sized from the last completed
session: as_of after the close, the session before during the day.

Reads: daily (SIP) and 30-minute (IEX) bars from data.alpaca.markets, and the paper account, positions, open
orders and assets from paper-api.alpaca.markets, all by HTTPS GET with paper keys only (paper_orders.session_bars
and paper_orders.snapshot). Alpaca's trading client is never loaded. Sending is paper_orders.sender --tradebot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from extensions.status import squeeze_gate
from extensions.status.confirmation import DAILY_LOOKBACK_DAYS, INDEXES, MarketData
from extensions.status.paper_tickets import (
    TicketInputError,
    TradingClientLoaded,
    account_from_alpaca,
    refuse_trading_client,
)
from extensions.tradebot import config, sources, store, ticketing, triggers
from extensions.tradebot.triggers import EOD, INTRADAY, STAGES
from paper_orders import market_calendar
from paper_orders.session_bars import BarsReadFailed, data_reader
from paper_orders.snapshot import ACCOUNT_PATH, PAPER_URL, NotPaperKey, ReadFailed, paper_reader
from paper_orders.trade_window import MARKET_TZ

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT = 0, 1, 2
REPO_ROOT = Path(__file__).resolve().parents[2]
CLOSE_CUTOFF = time(16, 15)  # as the scheduler's scan dates: bars are complete after this


def eod_as_of(now_local: datetime) -> date:
    if market_calendar.is_trading_day(now_local.date()) and now_local.time() >= CLOSE_CUTOFF:
        return now_local.date()
    return market_calendar.previous_trading_day(now_local.date())


def read_market(symbols: list[str], as_of: date, levels_as_of: date) -> tuple[MarketData, pd.DataFrame]:
    """Daily bars for the names and SPY/QQQ through as_of, and 30-minute bars for the names. Read-only."""
    load_dotenv(REPO_ROOT / ".env")
    reader = data_reader(os.environ)
    daily = reader.bars_daily(list(dict.fromkeys([*symbols, *INDEXES])),
                              levels_as_of - timedelta(days=DAILY_LOOKBACK_DAYS), as_of)
    thirty = reader.bars_30min(symbols, as_of - timedelta(days=squeeze_gate.BARS_LOOKBACK_DAYS), as_of) \
        if symbols else pd.DataFrame()
    return MarketData(daily, squeeze_gate.roll_session_bars(thirty, "4h") if not thirty.empty else pd.DataFrame(),
                      "GET data.alpaca.markets/v2/stocks/bars (1Day sip, 30Min iex)"), thirty


def paper_readers():
    load_dotenv(REPO_ROOT / ".env")
    reader = paper_reader(os.environ)

    def account():
        try:
            return account_from_alpaca(reader.account(), datetime.now(UTC), f"GET {PAPER_URL}{ACCOUNT_PATH}")
        except TicketInputError as exc:
            raise ticketing.TicketingError(str(exc)) from exc

    return account, reader.positions, reader.open_orders, reader.asset


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check the Tradebot queue's entry triggers (never sends an order).")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--stage", choices=STAGES, help="eod: after the close; intraday: during the session")
    which.add_argument("--ticket-only", action="store_true", help="ticket the triggered items without a check")
    parser.add_argument("--as-of", help="the session to check, YYYY-MM-DD (default: see above)")
    parser.add_argument("--item", action="append", help="check (and ticket) only this queue item id; repeatable")
    parser.add_argument("--ticket", action="store_true", help="write tickets for the triggered items")
    parser.add_argument("--dry-run", action="store_true", help="check and size, but write nothing")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def _apply(checks: list[triggers.Check], seen: dict[str, str]) -> list[str]:
    """Save the checks under the lock. An item whose status changed since it was read is left alone."""
    notes = []
    with store.transaction() as queues:
        for c in checks:
            try:
                item = store.find(queues, c.item_id)
            except store.StoreError:
                notes.append(f"{c.ticker}: removed from the queue during the check")
                continue
            if item.get("status") != seen[c.item_id]:
                notes.append(f"{c.ticker}: status changed to {item.get('status')} during the check; left as is")
                continue
            store.add_check(item, c.record())
            if c.status:
                store.set_status(item, c.status, c.reason)
    return notes


def main(argv: list[str] | None = None, now: datetime | None = None, market_reader=read_market,
         readers=paper_readers) -> int:
    args = _parser().parse_args(argv)
    now_local = (now or datetime.now(UTC)).astimezone(MARKET_TZ)
    try:
        refuse_trading_client()
        store.assert_not_synced()
        settings = config.load()
        stage = args.stage or EOD
        if args.as_of:
            as_of = date.fromisoformat(args.as_of)
        else:
            as_of = now_local.date() if stage == INTRADAY else eod_as_of(now_local)
    except (TradingClientLoaded, store.StoreError, ValueError) as exc:  # SettingsError is a ValueError
        print(f"error: {exc}; nothing checked", file=sys.stderr)
        return EXIT_BAD_INPUT

    out: dict = {"stage": None if args.ticket_only else stage, "as_of": as_of.isoformat(), "dry_run": args.dry_run,
                 "checks": [], "notes": [], "tickets": None}
    if stage == INTRADAY and not args.ticket_only:
        if not market_calendar.is_trading_day(as_of):
            out["notes"].append(f"{as_of} is not a trading day; the intraday stage does not run")
            return _finish(args, out, EXIT_OK)
        if market_calendar.early_close(as_of):
            out["notes"].append(f"{as_of} closes early; the intraday stage does not run (the session roll does not "
                                "know early closes)")
            return _finish(args, out, EXIT_OK)
    levels_as_of = market_calendar.previous_trading_day(as_of) if stage == INTRADAY else as_of
    out["levels_as_of"] = levels_as_of.isoformat()

    try:
        queues = store.read()
    except store.StoreError as exc:
        print(f"error: {exc}; nothing checked", file=sys.stderr)
        return EXIT_FAILED
    wanted = set(args.item or [])
    statuses = {"triggered"} if args.ticket_only else ({"armed"} if stage == INTRADAY else store.CHECKABLE)
    if args.ticket:
        statuses = statuses | {"triggered"}
    items = [i for i in queues["trade"] if i.get("status") in statuses and (not wanted or i["id"] in wanted)]
    if not items:
        out["notes"].append("no queue items to check")
        return _finish(args, out, EXIT_OK)

    symbols = sorted({i["ticker"] for i in items})
    try:
        market, thirty = market_reader(symbols, as_of, levels_as_of)
    except (NotPaperKey, BarsReadFailed) as exc:
        print(f"error: cannot read bars: {exc}; nothing checked", file=sys.stderr)
        return EXIT_FAILED

    if not args.ticket_only:
        try:
            label, route_file = triggers.route_label(as_of, stage)
        except sources.SourceError as exc:
            print(f"error: {exc}; nothing checked", file=sys.stderr)
            return EXIT_FAILED
        out["route"] = {"label": label, "file": route_file}
        fundamentals = store.data_home() / "fundamentals"
        clock = now_local.replace(tzinfo=None) if stage == INTRADAY else None
        to_check = [i for i in items if i.get("status") in (store.CHECKABLE if stage == EOD else {"armed"})]
        checks = [triggers.check_item(i, stage, as_of, label, settings, market, thirty, fundamentals, clock)
                  for i in to_check]
        out["checks"] = [{"item_id": c.item_id, "ticker": c.ticker, "from": next(i["status"] for i in to_check
                                                                                  if i["id"] == c.item_id),
                          **c.record()} for c in checks]
        if not args.dry_run:
            try:
                out["notes"] += _apply(checks, {i["id"]: i["status"] for i in to_check})
            except store.StoreError as exc:
                print(f"error: {exc}; the checks were not saved", file=sys.stderr)
                return EXIT_FAILED

    if args.ticket or args.ticket_only:
        try:
            read_account, read_positions, read_orders, read_asset = readers()
            if args.dry_run and not args.ticket_only:
                # the check was not saved, so preview what it would trigger plus what is triggered already
                preview = ({c["item_id"] for c in out["checks"] if c["status"] == "triggered"}
                           | {i["id"] for i in items if i.get("status") == "triggered"})
                run = _preview(as_of, levels_as_of, market, read_account, read_positions, read_orders, read_asset,
                               preview)
            else:
                run = ticketing.ticket_triggered(as_of, levels_as_of, market, read_account, read_positions,
                                                 read_orders, read_asset, list(wanted) or None, args.dry_run)
        except (ticketing.TicketingError, NotPaperKey, ReadFailed, store.StoreError, TicketInputError) as exc:
            out["tickets"] = {"error": str(exc)}
            print(f"error: cannot ticket: {exc}", file=sys.stderr)
            return _finish(args, out, EXIT_FAILED)
        out["tickets"] = run.to_dict()
    return _finish(args, out, EXIT_OK)


def _preview(as_of, levels_as_of, market, read_account, read_positions, read_orders, read_asset, ids):
    """A dry-run ticket preview for items the unsaved check just triggered: the queue copy is marked triggered in
    memory only."""
    queues = store.read()
    for item in queues["trade"]:
        if item["id"] in ids:
            item["status"] = "triggered"
    items = [i for i in queues["trade"] if i["id"] in ids]
    account = read_account()
    original = ticketing._series_original(ticketing.series_path(), account)
    run = ticketing.build_run(items, as_of, levels_as_of, account, read_positions(), read_orders(), queues["trade"],
                              market, read_asset, original)
    run.dry_run = True
    return run


def _finish(args, out: dict, code: int) -> int:
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return code
    stage = out["stage"] or "ticket-only"
    print(f"tradebot {stage} {out['as_of']}" + (f": market label {out['route']['label']} ({out['route']['file']})"
                                                 if out.get("route") else ""))
    for c in out["checks"]:
        moved = f"{c['from']} -> {c['status']}" if c["status"] else f"{c['from']} (unchanged)"
        print(f"  {c['ticker']:<6}{moved:<24}{c['reason']}")
    for note in out["notes"]:
        print(f"  {note}")
    t = out.get("tickets")
    if t and "error" not in t:
        word = "would ticket" if t.get("dry_run") else "ticketed"
        print(f"  {word}: {len(t['tickets'])} (levels from {t['levels_as_of']}); book stop-risk "
              f"${t['risk'].get('open_risk', 0):,.2f} open, ${t['risk'].get('room', 0):,.2f} room"
              + (f" -> {t['file']}" if t.get("file") else ""))
        for tk in t["tickets"]:
            print(f"    {tk['symbol']:<6}{tk['side']:<6}qty {tk['quantity']:>6}  limit {tk['entry']:>9.2f}  "
                  f"stop {tk['stop']:>9.2f}  target {tk['target']:>9.2f}  risk ${tk['risk_dollars']:,.2f}  "
                  f"id {tk['client_order_id']}")
        for sym, why in t["skipped"].items():
            print(f"    {sym:<6}no ticket: {why}")
        if t.get("no_ticket_reason"):
            print(f"    {t['no_ticket_reason']}")
    if args.dry_run:
        print("  dry run: nothing written")
    print("  no order sent")
    return code


if __name__ == "__main__":
    sys.exit(main())
