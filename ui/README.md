# Local UI

A local web screen for this fork: Dashboard, Scans, Research, Strategy and Trade.
It runs the scripts in `extensions/scripts`, reads the JSON and Markdown they
write under `~/.tradingagents`, and reads the Alpaca **paper** account.
It does not change upstream `cli/` or `tradingagents/`, and the server **never talks to
the broker to place orders itself**: Liquidate and Close all Positions are shown but
disabled, and no endpoint calls an Alpaca order method (see `tests/ui/test_no_orders.py`).
Paper orders go out only through `paper_orders.sender`, started as a job by the
scheduler's auto-send or by a confirmed Send on a Trade page paper ticket.

## Run

```sh
uv pip install -e ".[ui]"            # FastAPI + uvicorn into .venv
cd ui/web && npm install && npm run build && cd ../..
.venv/Scripts/python -m ui.server    # then open http://127.0.0.1:8000
```

The server listens on 127.0.0.1 only. Keys come from `.env` in the repo root
(`ALPACA_API_KEY` must be a paper key, starting with `PK`).

Development with hot reload: run `python -m ui.server --reload` and, in
`ui/web`, `npm run dev` (http://127.0.0.1:5173, proxies `/api` to :8000).

## How it fits together

- `ui/server/jobs.py` runs whitelisted scripts as `python -m extensions.scripts.<name>`
  and streams their output to the page over Server-Sent Events.
- `ui/server/files.py` reads baskets, regime labels, routes, plans and
  TradingAgents reports with plain `json` (newest `created_at` wins, as in the router).
- `ui/server/broker.py` is the only module that imports `alpaca.trading`; its
  `ReadOnlyBroker` exposes account, portfolio history, positions and orders, nothing else.
- Trade page order entry: Order… on a "To trade" row writes a manual paper ticket
  (`ui/server/manual_tickets.py`, in `~/.tradingagents/tickets/manual/`), using symbol, side and
  date from the queued row. Preview runs `paper_orders.sender --manual <name>` (a dry run);
  a confirmed Send adds `--send`. The entry is a limit DAY order. Running Send again after it
  fills places the OCO exit: target, and a stop the same distance from the actual fill.

## HTTPS behind antivirus

If Python requests fail with `CERTIFICATE_VERIFY_FAILED` (an antivirus scanning
HTTPS, such as Avast, re-signs traffic), point Python at a bundle that includes
that root certificate, e.g. `REQUESTS_CA_BUNDLE=path/to/bundle.pem`. The scripts'
bar fetches need the same.
