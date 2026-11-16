"""Backtest: replay past dates from the files the pipeline already saved, under the live regime rules.

Read-only. It reads saved routes, handoffs, scan reports, baskets and cached bars under the data folder with plain
``json`` and pandas. It never fetches data, never writes a file, never imports Alpaca (no market-data client, no
trading client), and never places an order. ``tests/extensions/test_backtest_engine.py`` checks the imports.
"""
