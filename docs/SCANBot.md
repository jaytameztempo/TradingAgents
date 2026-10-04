# SCANBot

**Owner:** Jay Tamez  
**Last updated:** 2026-10-03  
**Status:** Spec only. Does not trade. Does not call Alpaca's trading client.  
**Index:** see `trading-system-project.md` for system decisions. This file is the SCANBot spec only.  
**This file is not financial, investment, or trading advice.**

SCANBot is a ManagerBot-owned scanner. It reduces the US equity universe to scored TickerBaskets. It does not open, close, or size trades. ManagerBot pairs a basket with RegimeBot and then with the matching playbook. A label mismatch is a hard reject.

Scan names keep the `SCAN-` prefix. Sideways is two scans, not one: `SCAN-LongSidewaysChannel` and `SCAN-ShortSidewaysChannel`. They do not share a fundamentals gate.

## 1. Where it sits

```
Alpaca assets + bars/quotes          Fundamentals provider
        \                                  /
         SCANBot funnel (hard gates, mode, pivot, rank)
                         |
              TickerBaskets
              UP | DOWN | LONG_SIDE | SHORT_SIDE
                         |
                    ManagerBot
                    /         \
             RegimeBot      matching playbook
```

RegimeBot labels the market, sector, or industry. SCANBot decides which names are eligible inside that label. UP, DOWN, and the long channel scan are mutually exclusive on fundamentals and structure. The short channel scan is the exception: it requires losing, weakly financed companies, so it never overlaps the long channel scan.

Do not run a full LLM graph on the universe. SCANBot is deterministic rules plus a score. A later model may rank the short list. Hard gates override any model score.

## 2. Funnel

About 11,000 Alpaca US listings become a handful per basket. Target output is 10–25 names. Hard cap is 40. Under 8 survivors, return the short list with `thin_basket = true`. Do not relax a gate silently.

| Stage | Rule | Always on? |
| --- | --- | --- |
| 0. Universe | Active, tradable, listed common stock | Yes |
| 1. Financials | Mode-specific. Strong and profitable, or losing and weak. | Yes |
| 2. Liquidity | Dollar volume, share volume, spread | Yes |
| 3. Hygiene | Price, market cap, ATR band, earnings blackout | Yes |
| 4. Trend mode | UP, DOWN, LONG_SIDE, or SHORT_SIDE | Operator picks the scans |
| 5. Pivot | Low pivot for longs. High pivot for shorts. | On for swing scans |
| 6. Optional | Analyst bucket, P/E band, sector | Off unless set |
| 7. Rank | Score, sector cap, correlation cap | Yes |

Alpaca supplies the asset master, daily bars, quotes, and borrow flags. It does not supply profitability, P/E, analyst ratings, or sector. Those come from a fundamentals provider. Alpaca's screener endpoint is most-actives and movers only. It is a cross-check, not the scan.

## 3. Stage rules

### 3.1 Universe

From `GET /v2/assets`, keep:

- `class = us_equity`
- `status = active`
- `tradable = true`
- `exchange` in NYSE, NASDAQ, AMEX. Drop OTC.
- Common stock only. Drop ETFs, ETNs, warrants, rights, preferreds, units, and leveraged or inverse products.

DOWN mode and `SCAN-ShortSidewaysChannel` also require `shortable = true` and `easy_to_borrow = true`.

### 3.2 Financials (hard gate, mode-specific)

Profitability is not universal. `SCAN-UpwardTrend`, `SCAN-DownwardTrend`, and `SCAN-LongSidewaysChannel` require strong, profitable companies. `SCAN-ShortSidewaysChannel` requires the opposite. A name cannot pass both.

Measured on a trailing-twelve-month basis. Refresh weekly, and again the morning after an earnings print.

**Strong financials** — required for UP, DOWN, and `SCAN-LongSidewaysChannel`. All of:

1. Diluted EPS > 0
2. Operating income > 0
3. Operating cash flow > 0

Plus at least two of:

- Free cash flow > 0
- Operating margin above the sector median
- Current ratio >= 1.2
- Interest coverage > 3
- No going-concern language in the last 10-K or 10-Q

Reject a one-quarter profit spike after four losing quarters unless `allow_turnaround` is on. That flag is ignored by `SCAN-LongSidewaysChannel`. The long channel scan does not buy turnarounds.

**Losing and weak** — required for `SCAN-ShortSidewaysChannel`. All of:

1. Diluted EPS < 0
2. Operating income < 0
3. Operating cash flow < 0

Plus at least two of:

- Free cash flow < 0
- Operating margin below the sector median
- Current ratio < 1.0
- Interest coverage < 1, or no coverage because operating income is negative
- Going-concern language, a falling gross margin for two straight quarters, or shares outstanding up more than 10% year over year

A company that is merely unprofitable, but still cash-flow positive with a solid balance sheet, fails this scan. Weak means the financing is strained, not just that the multiple looks high. P/E is undefined here and is not a filter on this scan.

### 3.3 Liquidity (hard gate)

`avg_dollar_volume_20 = SMA(close * volume, 20)`

- 20-day average dollar volume >= $20 million
- 20-day average share volume >= 500,000
- Median bid-ask spread <= 0.15% of mid over the last 5 sessions
- No zero-volume session in the last 20 sessions

Aggressive preset: $5 million dollar volume, 0.30% spread. Conservative preset: $50 million, 0.08% spread.

### 3.4 Hygiene

- Price >= $10. Aggressive preset may use $5.
- Market cap >= $300 million.
- ATR(14) / price between 2% and 8%.
- Next earnings date not inside 3 sessions, unless `allow_earnings_catalyst` is on. Flag names inside 7 sessions either way.
- No unresolved halt. No split or special dividend inside 2 sessions.

### 3.5 Trend mode

Daily bars, split-adjusted. A scan may use only bars dated on or before `asof`. Thresholds match the current RegimeBot so a basket is not full of names the router will reject.

RegimeBot today: SIDE if ADX < 20, or if the 50-day and 200-day are tangled. UP only if close > SMA50 > SMA200 and ADX >= 25. DOWN is the mirror. ADX from 20 to 25 is SIDE.

**SCAN-UpwardTrend** — all of:

- close > SMA50 > SMA200
- SMA50 slope over 20 bars > 0
- ADX(14) >= 25
- last confirmed swing high > prior swing high, and last confirmed swing low > prior swing low

**SCAN-DownwardTrend** — all of:

- close < SMA50 < SMA200
- SMA50 slope over 20 bars < 0
- ADX(14) >= 25
- last confirmed swing high < prior swing high, and last confirmed swing low < prior swing low

**SCAN-LongSidewaysChannel** and **SCAN-ShortSidewaysChannel** share the channel geometry. They do not share the fundamental gate or the pivot.

Shared channel rules, all required:

- ADX(14) <= 20
- absolute SMA50 slope over 20 bars <= 0.5%
- 50-day is within 1% of the 200-day, or the two crossed in the last 20 sessions, or price is between them
- at least two touches of support and two touches of resistance inside 60 sessions
- channel height between 8% and 22% of price

`SCAN-LongSidewaysChannel` then keeps only strong-financial names at the low pivot. `SCAN-ShortSidewaysChannel` then keeps only losing, weak-financial names at the high pivot, and only if they are easy to borrow.

Names that fail every requested scan are dropped. ADX from 20 to 25 does not enter an UP or DOWN basket. Those names can enter a channel scan only if the channel rules pass. If they do not, they are out.

Swing points use a 5-bar fractal or a ZigZag with a 5% reversal. A swing is confirmed only after the reversal bar has closed. No lookahead.

## 4. Pivot rules

Pivot means the reward-to-risk is defined. It is not an entry trigger. UPBot, DOWNBot, and SIDEBot still own the trigger.

A prior draft passed a name if any two quality checks were true. That is too loose. The tightened rule is a required structure plus two quality checks. Missing structure is a fail, not a partial score.

### 4.1 UP bottom pivot — `SCAN-UpwardTrend`

Structure, all required:

- The last impulse is measured from the last confirmed swing low to the last confirmed swing high. That impulse is at least 6% and no more than 35%.
- Close is in the lower 35% of that impulse, and has not retraced more than 50% of it. A retrace past 50% fails. The old 62% cutoff let broken trends through.
- Close is still above SMA50. A close through SMA50 fails the uptrend pivot.
- Pullback length is 3 to 15 sessions. Shorter is noise. Longer is a stall.

Quality, at least two:

- Distance to rising SMA20 is inside 0.75 ATR. The old 1.0 ATR band was wide enough to include mid-leg names.
- RSI(14) is between 40 and 52. Below 40 is a breakdown risk. Above 52 is not a pullback.
- Pullback volume SMA(5) is below 80% of the 20-day average.
- The last two pullback sessions did not close in their bottom quartile.

### 4.2 DOWN top pivot — `SCAN-DownwardTrend`

Structure, all required:

- Last impulse, from the last confirmed swing high to the last confirmed swing low, is 6% to 35%.
- Close is in the upper 35% of that impulse, and has not retraced more than 50% of the decline.
- Close is still below SMA50.
- Bounce length is 3 to 15 sessions.
- `easy_to_borrow` is still true on the as-of snapshot.

Quality, at least two:

- Distance to falling SMA20 is inside 0.75 ATR.
- RSI(14) is between 48 and 60.
- Bounce volume SMA(5) is below 80% of the 20-day average.
- The last two bounce sessions did not close in their top quartile.

### 4.3 Low pivot — `SCAN-LongSidewaysChannel`

Profitable, strong-financial companies only. The channel is the median of the touch highs and the median of the touch lows over 60 sessions. Width must be 8% to 22%.

Structure, all required:

- Close is inside 0.6 ATR of channel support.
- Close is not through support by more than 0.25 ATR.
- ADX is still <= 20 on the as-of bar.
- Financial gate in section 3.2, strong side, has already passed.

Quality, at least two: RSI(14) 35–48, volume SMA(5) below the 20-day average, lower wick at least 40% of that bar's range, no close below support in the last 3 sessions.

`pivot_side` is always `LONG`. A name in the middle of the channel, or at resistance, does not enter this basket.

### 4.4 High pivot — `SCAN-ShortSidewaysChannel`

Losing, weak-financial companies only. Same channel definition as the long scan.

Structure, all required:

- Close is inside 0.6 ATR of channel resistance.
- Close is not through resistance by more than 0.25 ATR.
- ADX is still <= 20.
- `easy_to_borrow` is true on the as-of snapshot.
- Financial gate in section 3.2, weak side, has already passed.

Quality, at least two: RSI(14) 52–65, volume SMA(5) below the 20-day average, upper wick at least 40% of that bar's range, no close above resistance in the last 3 sessions.

`pivot_side` is always `SHORT`. A name at support does not enter this basket. These two scans cannot share a symbol: one requires profits and a sound balance sheet, the other requires losses and strain.

## 5. Optional filters

Off unless the request sets them.

Analyst buckets, from a consensus with at least 3 analysts and a rating newer than 90 days:

- `HIGH`: buy or strong buy
- `MEDIUM`: hold
- `LOW`: sell or strong sell
- unrated fails a strict rating filter

P/E bands use trailing P/E. Forward P/E is display only.

- `LOW`: 0 < P/E <= 15
- `MEDIUM`: 15 < P/E <= 30
- `HIGH`: P/E > 30

`pe_mode = SECTOR_RELATIVE` compares the name with its sector median instead: cheap at or below the 35th percentile, in-line between 35 and 65, rich above 65.

Sector is multi-select GICS. Empty means all sectors. Industry is a second optional cut. Sector return columns are computed on every scan even when the filter is off, so the later heatmap and RegimeBot share one table.

## 6. Extra ranking features

Not hard gates, except the two caps.

- 63-day relative strength vs SPY. UP prefers the top third. DOWN and the short channel prefer the bottom third. The long channel ignores direction and prefers proximity to support.
- 20-day sector relative strength. A name fighting RegimeBot's sector label loses score. It is not deleted here. Manager still rejects a label mismatch.
- Distance to the 52-week high or low. UP pullbacks still within 15% of the high score higher.
- Any overnight gap over 4% in 20 sessions flags the name and cuts the score.
- Sector cap: no more than 30% of a basket from one sector.
- Correlation cap: no more than 3 names with 60-day correlation above 0.8.

Pattern names such as flag, cup, and double bottom stay out of SCANBot.

## 7. Score

Each survivor gets a 0–100 score. Then the caps run. Then the top `max_names` are emitted.

UP: pivot quality 30, trend quality 25, relative strength 20, liquidity headroom 15, fundamentals 10.  
DOWN: same weights, with relative weakness and borrow quality inside the liquidity term.  
LONG_SIDE: range cleanliness 35, low-pivot proximity 30, volatility fit 15, liquidity 10, strong-financials quality 10.  
SHORT_SIDE: range cleanliness 30, high-pivot proximity 30, weakness of financials 20, liquidity and borrow 15, relative weakness 5.

On the long channel scan, fundamentals points reward free cash flow, margin versus the sector, and balance-sheet slack. On the short channel scan, those same inputs are inverted: deeper losses, negative free cash flow, and a strained balance sheet raise the score. Missing fundamentals data scores 0. It does not invent a value. P/E is not scored on the short channel scan.

## 8. Parameter schema

One request object drives every scan. Defaults below are the ship values.

```python
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Mode = Literal["UP", "DOWN", "LONG_SIDE", "SHORT_SIDE"]
AnalystBucket = Literal["HIGH", "MEDIUM", "LOW"]
PeBand = Literal["LOW", "MEDIUM", "HIGH"]
PeMode = Literal["ABSOLUTE", "SECTOR_RELATIVE"]
Preset = Literal["DEFAULT", "AGGRESSIVE", "CONSERVATIVE"]


@dataclass(frozen=True)
class ScanParams:
    """SCANBot request. Gates are inclusive floors unless noted."""

    asof: date
    modes: tuple[Mode, ...] = ("UP", "DOWN", "LONG_SIDE", "SHORT_SIDE")
    preset: Preset = "DEFAULT"

    # Stage 1 — applied by scan, not as one global switch
    # LONG_SIDE, UP, DOWN use STRONG. SHORT_SIDE uses WEAK.
    strong_min_quality_checks: int = 2
    weak_min_quality_checks: int = 2
    allow_turnaround: bool = False

    # Stage 2
    min_dollar_volume_20: float = 20_000_000
    min_share_volume_20: int = 500_000
    max_spread_pct: float = 0.15

    # Stage 3
    min_price: float = 10.0
    min_market_cap: float = 300_000_000
    min_atr_pct: float = 2.0
    max_atr_pct: float = 8.0
    earnings_blackout_sessions: int = 3
    earnings_flag_sessions: int = 7
    allow_earnings_catalyst: bool = False

    # Stage 4 — kept equal to RegimeBot
    trend_adx_min: float = 25.0
    side_adx_max: float = 20.0
    sma_slope_lookback: int = 20
    side_slope_max_pct: float = 0.5
    channel_lookback: int = 60
    channel_min_height_pct: float = 8.0
    channel_max_height_pct: float = 22.0
    min_channel_touches: int = 2
    swing_reversal_pct: float = 5.0

    # Stage 5
    require_pivot: bool = True
    impulse_min_pct: float = 6.0
    impulse_max_pct: float = 35.0
    max_retrace_pct: float = 50.0
    pivot_zone_pct: float = 35.0
    pivot_atr_max: float = 0.75
    side_pivot_atr_max: float = 0.6
    side_break_atr_max: float = 0.25
    pullback_min_sessions: int = 3
    pullback_max_sessions: int = 15
    rsi_up_low: float = 40.0
    rsi_up_high: float = 52.0
    rsi_down_low: float = 48.0
    rsi_down_high: float = 60.0
    dryup_volume_ratio: float = 0.80
    min_quality_checks: int = 2

    # Stage 6 — empty means off
    analyst_buckets: tuple[AnalystBucket, ...] = ()
    min_analysts: int = 3
    analyst_max_age_days: int = 90
    pe_bands: tuple[PeBand, ...] = ()
    pe_mode: PeMode = "ABSOLUTE"
    sectors: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()

    # Stage 7
    max_names: int = 15
    hard_cap: int = 40
    thin_basket_below: int = 8
    sector_cap_pct: float = 30.0
    corr_lookback: int = 60
    corr_max: float = 0.8
    corr_name_cap: int = 3
    allow_overlap: bool = False

    def apply_preset(self) -> "ScanParams":
        if self.preset == "AGGRESSIVE":
            return ScanParams(
                asof=self.asof,
                modes=self.modes,
                preset=self.preset,
                min_dollar_volume_20=5_000_000,
                max_spread_pct=0.30,
                min_price=5.0,
            )
        if self.preset == "CONSERVATIVE":
            return ScanParams(
                asof=self.asof,
                modes=self.modes,
                preset=self.preset,
                min_dollar_volume_20=50_000_000,
                max_spread_pct=0.08,
                min_price=20.0,
                min_market_cap=2_000_000_000,
            )
        return self
```

`apply_preset` replaces only the liquidity and price floors. It does not loosen profitability, ADX, or pivot structure.

## 9. TickerBasket contract

```python
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Mode = Literal["UP", "DOWN", "LONG_SIDE", "SHORT_SIDE"]
FinancialGate = Literal["STRONG", "WEAK"]
PivotSide = Literal["LONG", "SHORT"]
PeBand = Literal["LOW", "MEDIUM", "HIGH"]
AnalystBucket = Literal["HIGH", "MEDIUM", "LOW"]


@dataclass(frozen=True)
class BasketMember:
    symbol: str
    mode: Mode
    financial_gate: FinancialGate
    score: float
    rank: int
    sector: str
    industry: str
    price: float
    dollar_volume_20: float
    spread_pct: float
    atr_pct: float
    pe_ttm: float | None
    pe_band: PeBand | None
    analyst_bucket: AnalystBucket | None
    earnings_date: date | None
    pivot_side: PivotSide
    distance_to_pivot_atr: float
    rs_63: float
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageCount:
    stage: str
    survivors: int


@dataclass(frozen=True)
class TickerBasket:
    """Output of one SCANBot run for one mode. Not an order list."""

    basket_id: str
    scan_name: str
    mode: Mode
    asof: date
    created_at: datetime
    params_hash: str
    members: tuple[BasketMember, ...]
    stage_counts: tuple[StageCount, ...]
    universe_count: int
    thin_basket: bool
    data_freshness: dict[str, str] = field(default_factory=dict)

    def symbols(self) -> tuple[str, ...]:
        return tuple(m.symbol for m in self.members)
```

`scan_name` is `SCAN-UpwardTrend`, `SCAN-DownwardTrend`, `SCAN-LongSidewaysChannel`, or `SCAN-ShortSidewaysChannel`.  
`basket_id` is `{scan_name}-{asof}`.  
`financial_gate` is `STRONG` on the first three and `WEAK` on the short channel scan.  
`pivot_side` is `LONG` for `SCAN-LongSidewaysChannel` and `SHORT` for `SCAN-ShortSidewaysChannel`.  
`params_hash` is a hash of the `ScanParams` snapshot so a later run can explain why a name appeared.  
`flags` may include `EARNINGS_7D`, `GAP_RISK`, `THIN_BORROW`, `LOW_ANALYST_COVERAGE`, `GOING_CONCERN`.  
`data_freshness` records the bar date and the fundamentals file date. A stale fundamentals file older than 10 days fails the run closed.

ManagerBot pairing:

- RegimeBot UP consumes only a mode UP basket, and only the UP playbook may plan it.
- RegimeBot DOWN consumes only a mode DOWN basket, and only the DOWN playbook may plan it.
- RegimeBot SIDE may consume `SCAN-LongSidewaysChannel` or `SCAN-ShortSidewaysChannel`. The long basket is planned only as a long fade off support. The short basket is planned only as a short fade off resistance.
- A SIDE label does not authorize an UP or DOWN basket. Disagreement is logged. It is not overridden.

## 10. Sector heatmap (later)

Same table as the scan. Do not build a second sector engine.

- Rows: 11 GICS sectors.
- Columns: 1-day, 5-day, 20-day, 60-day equal-weight return of the liquid profitable universe in that sector.
- Color: red to green, clipped at ±2 standard deviations.
- Overlay: RegimeBot label for that sector.
- Click-through: the basket already filtered to that sector.

Industry heatmap is a later pass.

## 11. Build order

1. Universe and liquidity on Alpaca only. Record how many names each gate removes.
2. Financial gates once the fundamentals file is cached. Strong and weak are separate passes.
3. Trend classifier using the RegimeBot ADX cutoffs. Weekly visual check of rejects.
4. Pivot structure, then quality checks. Review rejects by hand for two weeks.
5. Analyst, P/E, and sector parameters.
6. Score, caps, and the TickerBasket handoff. Paper the baskets for 20 sessions before any playbook consumes them.
7. Sector heatmap on the same table.

No step in this list places an order.

## 12. Defaults

Strong financials on UP, DOWN, and `SCAN-LongSidewaysChannel`. Losing and weak financials on `SCAN-ShortSidewaysChannel`. Dollar volume >= $20M. Price >= $10. ATR% 2–8. ADX >= 25 for UP and DOWN, ADX <= 20 for both channel scans. Pivot structure required, plus two quality checks. Analyst, P/E, and sector off. P/E is never applied to the short channel scan. Top 15 per basket. Sector cap 30%. Earnings blackout 3 sessions.
