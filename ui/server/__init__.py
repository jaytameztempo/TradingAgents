"""FastAPI backend for the local UI.

It runs the fork's extension scripts as subprocesses, reads the JSON and
Markdown they write under ~/.tradingagents, and reads the Alpaca paper account.
It places no orders: there is no endpoint that changes anything at the broker.
"""
