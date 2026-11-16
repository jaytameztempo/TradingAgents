"""Cached bars, read from ``<home>/cache/bars``. Nothing is fetched.

The bar fetcher saves one CSV a symbol and date range, ``SYM_<start>_<end>_<feed>_<adjustment>.csv`` for daily bars and
the same name ending ``.4h_rth.csv`` for regular-session 4-hour bars. Ranges overlap, so every file for a symbol is
read and merged; on a duplicate bar the file with the later name (the later range) wins.

Each frame carries a ``session`` column (the trading date, a ``datetime.date``) and open/high/low/close, sorted in
time order. A daily frame has one row a session; a 4-hour frame has several.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

DAILY, FOUR_HOUR = "daily", "4h"
TIMEFRAMES = (DAILY, FOUR_HOUR)
_TICKER = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z]{1,2})?$")
_RANGE = r"_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}_[a-z]+_[a-z]+"
PRICE_COLUMNS = ["open", "high", "low", "close"]


class BarCache:
    def __init__(self, home: str | Path):
        self.folder = Path(home) / "cache" / "bars"
        self._frames: dict[tuple[str, str], pd.DataFrame] = {}

    def bars(self, symbol: str, timeframe: str = DAILY) -> pd.DataFrame:
        """All cached bars for ``symbol`` (empty when there are none or the symbol is not a plain ticker)."""
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
        key = (symbol, timeframe)
        if key not in self._frames:
            self._frames[key] = self._load(symbol, timeframe)
        return self._frames[key]

    def raw_daily(self, symbol: str) -> pd.DataFrame:
        """Every cached daily bar for ``symbol`` with all its saved columns, merged across files, ``date`` parsed to
        a timestamp (the shape ``fetch_daily_bars`` returns). Empty when there are none."""
        raw = self._read(symbol, DAILY)
        if raw.empty or "date" not in raw.columns:
            return pd.DataFrame()
        raw = raw.copy()
        raw["date"] = pd.to_datetime(raw["date"])
        raw = raw.drop_duplicates(subset="date", keep="last").sort_values("date", kind="stable")
        return raw.reset_index(drop=True)

    def _read(self, symbol: str, timeframe: str) -> pd.DataFrame:
        if not _TICKER.fullmatch(symbol or "") or not self.folder.is_dir():
            return pd.DataFrame()
        suffix = r"\.4h_rth\.csv" if timeframe == FOUR_HOUR else r"\.csv"
        name = re.compile(re.escape(symbol) + _RANGE + suffix + "$")
        paths = sorted(p for p in self.folder.glob(f"{symbol}_*.csv") if name.fullmatch(p.name))
        frames = []
        for path in paths:
            try:
                frames.append(pd.read_csv(path))
            except (OSError, ValueError, pd.errors.ParserError):
                continue
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _load(self, symbol: str, timeframe: str) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["session", "time", *PRICE_COLUMNS])
        raw = self._read(symbol, timeframe)
        if raw.empty or not set(PRICE_COLUMNS) <= set(raw.columns) or "date" not in raw.columns:
            return empty
        out = pd.DataFrame({
            "session": pd.to_datetime(raw["date"]).dt.date,
            "time": pd.to_datetime(raw["timestamp"]) if timeframe == FOUR_HOUR and "timestamp" in raw
            else pd.to_datetime(raw["date"]),
        })
        for col in PRICE_COLUMNS:
            out[col] = pd.to_numeric(raw[col], errors="coerce")
        out = out.dropna(subset=PRICE_COLUMNS)
        out = out.drop_duplicates(subset="time", keep="last").sort_values("time", kind="stable")
        return out.reset_index(drop=True)
