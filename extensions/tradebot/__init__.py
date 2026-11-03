"""The Tradebot layer: names the router allowed wait in the trade queue until their entry triggers fire.

``store`` owns ~/.tradingagents/queues.json behind a cross-process lock. Nothing in this package loads Alpaca's
trading client; orders are sent only by ``paper_orders.sender``.
"""
