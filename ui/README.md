# Local UI

A local web screen for this fork: Dashboard, Scans, Research, Strategy and Trade.
It runs the scripts in `extensions/scripts`, reads the JSON and Markdown they
write under `~/.tradingagents`, and reads the Alpaca **paper** account.
It does not change upstream `cli/` or `tradingagents/`, and it **places no orders**:
Liquidate and Close all Positions are shown but disabled, and the server has no
endpoint that sends anything to the broker (see `tests/ui/test_no_orders.py`).

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

## HTTPS behind antivirus

If Python requests fail with `CERTIFICATE_VERIFY_FAILED` (an antivirus scanning
HTTPS, such as Avast, re-signs traffic), point Python at a bundle that includes
that root certificate, e.g. `REQUESTS_CA_BUNDLE=path/to/bundle.pem`. The scripts'
bar fetches need the same.
