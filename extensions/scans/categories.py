"""SCAN categories: three strictness levels for each regime scan, as data only.

Category 1 is the strictest (smallest baskets), 2 is balanced, 3 is the loosest
(largest baskets). The scans in ``scan_up_category``, ``scan_down_category`` and
``scan_side_category`` read only the Params object they are given, so a caller
(the command-line runner now, the UI later) picks a category by number and
nothing else in the scan changes.

Ranges are inclusive at both ends. ADX floors and ceilings are inclusive.
Moving-average orderings (close > SMA50 and so on) are strict, as are volume
ratios written as "below". Nothing here places an order.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from extensions.scanbot.financials import STRONG, WEAK

CATEGORIES = (1, 2, 3)
DEFAULT_CATEGORY = 1

NEUTRAL = "NEUTRAL"  # had fundamentals, failed the STRONG metrics, and is not a WEAK survivor

# Shared windows. RS is the 63-session return; "the 20-day average volume" is the
# 20 sessions before the as-of bar, so a spike on the as-of bar does not lift it.
RS_SESSIONS = 63
SMA50_SLOPE_SESSIONS = 20
VOLUME_AVERAGE_SESSIONS = 20
VOLUME_FAST_SESSIONS = 5
TREND_MIN_BARS = 200  # SMA200 must be real
LOOSE_MIN_BARS = RS_SESSIONS + 1  # SMA50 and a 63-session return

# Proximity levels.
SMA20, SMA50, SWING_LOW = "sma20", "sma50", "last_swing_low"

# SCANDOWN structures.
FAILED_RALLY = "failed_rally"
BELOW_SWING_HIGH_OR_SMA20 = "below_last_swing_high_or_sma20"
BELOW_SMA20_OR_SMA50 = "below_sma20_or_sma50"

# Borrow rules for short baskets, on the asset snapshot's flags.
SHORTABLE_AND_ETB = "shortable_and_easy_to_borrow"
EASY_TO_BORROW = "easy_to_borrow"
SHORTABLE_OR_ETB = "shortable_or_easy_to_borrow"


@dataclass(frozen=True)
class UpParams:
    category: int
    min_bars: int
    above_sma200: bool  # close > SMA50 > SMA200; otherwise close > SMA50 only
    sma50_rising: bool  # SMA50 above its value SMA50_SLOPE_SESSIONS bars earlier
    adx_min: float
    rs_rank_min: float | None  # None: no RS filter
    rs_or_beats_spy: bool  # the RS filter also passes on 63-session strength vs SPY > 0
    pullback_pct: tuple[float, float] | None  # below the last confirmed ZigZag high
    pullback_declining_volume: bool  # 5-session average volume below the 20-day average
    rsi: tuple[float, float]
    near_atr_max: float | None  # |close - level| / ATR at most this for any of near_levels
    near_levels: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DownParams:
    category: int
    min_bars: int
    below_sma200: bool  # close < SMA50 < SMA200; otherwise close < SMA50 only
    adx_min: float
    rs_rank_max: float | None  # None: no RS filter
    rs_or_lags_spy: bool  # the RS filter also passes on 63-session strength vs SPY < 0
    structure: str
    rsi: tuple[float, float]
    borrow: str
    # FAILED_RALLY only: a bar in the last rally_sessions before as-of reached SMA20 or broken
    # support on volume below rally_volume_below x the average; the as-of bar closed back below
    # that level on at least breakdown_volume_min x the average.
    rally_sessions: int = 5
    rally_volume_below: float = 1.0
    breakdown_volume_min: float = 1.5

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SideParams:
    category: int
    adx_max: float
    height_pct: tuple[float, float]  # (resistance - support) / close
    min_touches: int  # confirmed ZigZag swings of each kind in the channel window
    band_atr: tuple[float, float]  # (close - support) / ATR for longs, (resistance - close) / ATR for shorts
    rsi_long: tuple[float, float]
    rsi_short: tuple[float, float]
    volume_below: float | None  # as-of volume below this x the 20-day average; None: no filter
    long_financials: tuple[str, ...] | None  # None: no financial filter
    short_financials: tuple[str, ...] | None
    short_borrow: str

    def to_dict(self) -> dict:
        return asdict(self)


UP = {
    1: UpParams(1, TREND_MIN_BARS, True, True, 25.0, 80.0, True, (5.0, 15.0), True, (40.0, 55.0),
                1.0, (SMA20, SWING_LOW)),
    2: UpParams(2, TREND_MIN_BARS, True, False, 20.0, 60.0, False, (3.0, 20.0), False, (35.0, 60.0), None),
    3: UpParams(3, LOOSE_MIN_BARS, False, False, 15.0, None, False, None, False, (30.0, 70.0),
                2.0, (SMA20, SMA50)),
}

DOWN = {
    1: DownParams(1, TREND_MIN_BARS, True, 25.0, 20.0, True, FAILED_RALLY, (45.0, 60.0), SHORTABLE_AND_ETB),
    2: DownParams(2, TREND_MIN_BARS, True, 20.0, 40.0, False, BELOW_SWING_HIGH_OR_SMA20, (40.0, 70.0),
                  EASY_TO_BORROW),
    3: DownParams(3, LOOSE_MIN_BARS, False, 15.0, None, False, BELOW_SMA20_OR_SMA50, (30.0, 75.0),
                  SHORTABLE_OR_ETB),
}

SIDE = {
    1: SideParams(1, 20.0, (8.0, 22.0), 2, (0.0, 0.6), (35.0, 48.0), (52.0, 65.0), 1.0,
                  (STRONG,), (WEAK,), EASY_TO_BORROW),
    2: SideParams(2, 25.0, (6.0, 25.0), 2, (0.0, 1.0), (30.0, 50.0), (50.0, 70.0), 1.2,
                  (STRONG, NEUTRAL), None, EASY_TO_BORROW),
    3: SideParams(3, 30.0, (5.0, 30.0), 1, (-1.5, 1.5), (25.0, 55.0), (45.0, 75.0), None,
                  None, None, SHORTABLE_OR_ETB),
}

TABLES = {"up": UP, "down": DOWN, "side": SIDE}


def params_for(scan: str, category: int):
    """The Params for one scan (up, down or side) and category (1, 2 or 3)."""
    if scan not in TABLES:
        raise ValueError(f"scan must be one of {', '.join(TABLES)}, not {scan!r}")
    if category not in CATEGORIES:
        raise ValueError(f"category must be 1, 2 or 3, not {category!r}")
    return TABLES[scan][category]
