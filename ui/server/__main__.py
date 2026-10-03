"""Start the UI: python -m ui.server, then open http://127.0.0.1:8000."""

from __future__ import annotations

import argparse

import uvicorn

# Loopback only. The broker keys live in .env on this machine; nothing else
# on the network should be able to reach an endpoint that reads the account.
HOST = "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m ui.server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    args = parser.parse_args()
    uvicorn.run("ui.server.app:app", host=HOST, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
