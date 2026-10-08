"""Operator-supplied GICS sector map for the channel rank step.

No saved SCANBot report carries a sector field, so the 30% sector cap reads
its labels from this hand-entered table. It covers the 24 names in the
2026-09-30 SCAN-LongSidewaysChannel and SCAN-ShortSidewaysChannel reports.
It is not point in time and not fetched from any vendor. Edit it by hand.
A symbol missing from it ranks under UNKNOWN_SECTOR and the rank report warns.
"""

from __future__ import annotations

SECTOR_SOURCE = "operator-supplied"
SECTOR_MAP_NOTE = "hand-entered GICS sectors in extensions/scanbot/sector_map.py; not point in time"
UNKNOWN_SECTOR = "Unknown"

GICS_SECTOR: dict[str, str] = {
    # SCAN-LongSidewaysChannel 2026-09-30
    "AFRM": "Financials",
    "ALGM": "Information Technology",
    "AMT": "Real Estate",
    "CI": "Health Care",
    "DG": "Consumer Staples",
    "EQPT": "Industrials",
    "HAS": "Consumer Discretionary",
    "INCY": "Health Care",
    "LIVN": "Health Care",
    "LTH": "Consumer Discretionary",
    "MO": "Consumer Staples",
    "OPCH": "Health Care",
    "ORCL": "Information Technology",
    "ORLY": "Consumer Discretionary",
    "OTEX": "Information Technology",
    "PPC": "Consumer Staples",
    "TKO": "Communication Services",
    "UAL": "Industrials",
    "VG": "Energy",
    "VST": "Utilities",
    "WMT": "Consumer Staples",
    "ZM": "Information Technology",
    # SCAN-ShortSidewaysChannel 2026-09-30
    "IONQ": "Information Technology",
    "VIR": "Health Care",
}


def sector_of(symbol: str) -> str:
    return GICS_SECTOR.get(symbol, UNKNOWN_SECTOR)
