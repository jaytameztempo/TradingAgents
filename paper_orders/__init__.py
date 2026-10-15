"""Sends saved paper tickets to the Alpaca paper account (``sender``) and prints a read-only snapshot of its
open positions and orders (``snapshot``); ``session_bars`` reads 30-minute bars for the TTM Squeeze gate,
which the ticket builder no longer calls. Kept outside ``extensions`` so the scans, router, bots and ticket
builder never load Alpaca's trading client. Nothing is imported here, so loading ``snapshot`` never loads
``sender``."""
