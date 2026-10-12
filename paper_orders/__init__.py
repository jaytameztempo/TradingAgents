"""Sends saved paper tickets to the Alpaca paper account (``sender``) and prints a read-only snapshot of its
open positions and orders (``snapshot``). Kept outside ``extensions`` so the scans, router, bots and ticket
builder never load Alpaca's trading client. Nothing is imported here, so loading ``snapshot`` never loads
``sender``."""
