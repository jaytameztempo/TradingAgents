"""UPBot: write a trend-following research plan for one routed symbol when the regime is UP.

It reads saved route files only. It imports no Alpaca module, fetches no bars, and places no orders.

Run from the TradingAgents folder:

    python -m extensions.scripts.run_up_bot --as-of 2026-09-30 --symbol NVDA

Reads the latest route from ~/.tradingagents/routes/ (or --route-dir). If its regime is
UP and the symbol is in the route, writes the plan to ~/.tradingagents/plans/ unless
--out-dir is set. Any other regime stops it and nothing is written.
"""

from __future__ import annotations

import sys

from extensions.playbooks.trend_bots import UPBOT
from extensions.scripts import trend_bot_cli


def main(argv: list[str] | None = None) -> int:
    return trend_bot_cli.main(UPBOT, argv)


if __name__ == "__main__":
    sys.exit(main())
