# Local UI

A local web screen for this fork: Dashboard, Scans, Research, Strategy and Trade.
It runs the scripts in `extensions/scripts`, reads the JSON and Markdown they
write under `~/.tradingagents`, and reads the Alpaca **paper** account.
It does not change upstream `cli/` or `tradingagents/`, and the server **never talks to
the broker to place orders itself**: Liquidate and Close all Positions are shown but
disabled, and no endpoint calls an Alpaca order method (see `tests/ui/test_no_orders.py`).
Paper orders go out only through `paper_orders.sender`, started as a job by the
scheduler (Tradebot auto mode with paper auto-send on) or by a confirmed Send on the Trade
page (the Tradebot queue or a manual paper ticket). Every send goes through the risk guard
(`paper_orders/guard.py`) and is written to the order ledger.

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

## Running unattended (Windows)

`scripts/start_bats.ps1` starts the server and its scheduler in the foreground and writes its
output to `~/.tradingagents/logs/server_<yyyyMMdd_HHmmss>.log` (plus `.err.log`, and
`.check.log` for the read-only setup check it runs first). Logs older than 30 days are deleted.
If the port is already in use, it exits at once, so a second start does nothing.

```powershell
# once, from the repo root, after the .venv is set up
powershell -ExecutionPolicy Bypass -File scripts\start_bats.ps1 -Install    # register the task
powershell -ExecutionPolicy Bypass -File scripts\start_bats.ps1             # or start it by hand now
powershell -ExecutionPolicy Bypass -File scripts\start_bats.ps1 -Uninstall  # remove the task
```

`-Install` registers the **"BATS server"** task. It runs as you, and only while you are logged on,
so no password is stored. It starts at logon and on weekdays at 08:30, and wakes the PC for the
08:30 start if wake timers are allowed (Power Options → Sleep → Allow wake timers). After a
failure it restarts every 5 minutes, up to 5 times. It has no run-time limit, and a missed start
runs as soon as possible.

The task points at this repo's path: after moving the repo, recreate the `.venv` in its new place and run
`-Install` again.

Check it:

- `Get-ScheduledTask -TaskName "BATS server" | Get-ScheduledTaskInfo` shows the last and next run.
- The Dashboard's **Tradebot and scheduler** panel shows "ticking" with the last tick time. It turns
  red after 2 minutes without a tick.
- `.venv\Scripts\python -m extensions.scripts.check_setup` checks the setup and prints PASS, WARN
  or FAIL for each item. It is read-only.

The data folder (`~/.tradingagents`, or `TRADINGAGENTS_UI_HOME`) must not be inside OneDrive. If it
is, the server keeps its scheduler off and says why on the Dashboard. Keep the repo out of OneDrive
too, so sync never locks a file in `.venv`. Day-to-day operation is in
[`docs/go-live-runbook.md`](../docs/go-live-runbook.md).

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
