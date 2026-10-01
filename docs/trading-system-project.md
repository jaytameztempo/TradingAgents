# Swing Trading System — Project Record

**Owner:** Jay Tamez  
**Last updated:** 2026-09-30  
**Status:** Design only. No code has been written yet. No live trading.  
**This file is not financial, investment, or trading advice.**

This document records everything decided in this project so far, plus the professional market-regime practices RegimeBot will follow.

---

## 1. What this project is

An automated **swing-trading** system for US stocks. It scans names, decides whether the market (and each basket) is trending up, trending down, or moving sideways, then opens and closes trades with rules that match that regime.

Holding period target: days to weeks. Not day trading. Not high-frequency trading.

The system is **not** a finished product. It is a design that will be built later, in small steps, on a fork of an existing open-source framework.

---

## 2. Decisions already made

### 2.1 Core framework

Use **TradingAgents** by Tauric Research as the decision engine.

- Repository: https://github.com/TauricResearch/TradingAgents
- Paper: *TradingAgents: Multi-Agents LLM Financial Trading Framework* (arXiv 2412.20138)
- License: Apache-2.0
- Language: Python 3.11+ (their README uses 3.13)
- Orchestration: LangGraph
- Target version to start from: **v0.5.2 or later** (parallel analysts, unattended CLI, run settings recorded in every report, backtests that only see data published by the analysis date)

TradingAgents models a trading firm. It is a **research framework**, not a broker terminal and not a finished swing-trading product. The official disclaimer applies: results vary with model, temperature, period, and data quality. It is not investment advice.



### 2.2 Broker, language, models

- **Broker API:** Alpaca Markets — https://alpaca.markets/
- **Language:** Python
- **LLMs (pick one to start):** Grok, ChatGPT (OpenAI), Claude, or Gemini. TradingAgents already supports multiple providers (OpenAI, Anthropic, Google, xAI, OpenRouter, Ollama, and others). Mixing four providers on day one only adds cost and confusion.

## 3. Product shape

### 3.1 Roles in plain language

**Manager** is the supervisor. It is the only process allowed to launch the firm graph, change baskets, or send broker orders. It does not replace TradingAgents. It sits above it.

**SCAN-*** algorithms scan the market and return lists of tradeable stocks. Those lists are called **TickerBaskets**. A SCAN does not place orders.

**RegimeBot** labels direction for the market, a sector, an industry, or a basket: **UP**, **DOWN**, or **SIDE**.

**Strategy series** are playbooks. Manager only pairs a playbook with a basket whose regime matches.

| Prefix | When it may run | Example |
|---|---|---|
| `SCAN-` | Always, to build baskets | `SCAN-Upward Trend Momentum` |
| `UP-` | Regime is UP | `UP-HeikenAshi Trend Pullback` |
| `DOWN-` | Regime is DOWN | `DOWN-Short Momentum Fade` |
| `SIDE-` | Regime is SIDE | `SIDE-Mean Reversion Oscillator` |

A mismatch is a hard reject. An UP playbook never runs on a DOWN basket.

### 3.2 Layers

```
Manager (supervisor)
  ├── SCAN-* workers          → TickerBaskets
  ├── RegimeBot               → UP | DOWN | SIDE
  ├── Strategy router         → UP-* | DOWN-* | SIDE-* pack
  └── TradingAgentsGraph (per selected ticker)
        ├── Analyst Team (parallel)
        ├── Researcher Team: Bull vs Bear + Research Manager
        ├── Trader (regime-conditioned playbook)
        ├── Risk Team: Aggressive / Neutral / Conservative
        └── Portfolio Manager → approve or reject
              └── Execution adapter → Alpaca paper, later live
                 (or the built-in simulated exchange for research)
```

### 3.3 Native TradingAgents roles, and how this project uses them

Do not collapse the bull/bear debate. That adversarial step is the point of the framework.

| TradingAgents role | Use in this project |
|---|---|
| Fundamentals Analyst | Quality, leverage, dilution, earnings quality for a multi-day hold |
| Sentiment Analyst | News and social mood for the short horizon |
| News Analyst | Macro and event risk that can invalidate a swing |
| Technical Analyst | Indicators plus playbook features (Heikin Ashi, ADX, bands) |
| Bull researcher | Builds the upside case from the analyst reports |
| Bear researcher | Builds the downside case. Must be allowed to win |
| Research Manager | One synthesized view before a trade is proposed |
| Trader | Emits a structured trade intent under the active UP / DOWN / SIDE pack |
| Risk team | Volatility, liquidity, correlation, concentration |
| Portfolio Manager | Final yes or no. Only then may an order leave the system |

### 3.4 Execution

TradingAgents, by default, sends an approved order to a **simulated** exchange. This project adds an execution adapter:

- Alpaca **paper** key first. Paper key and live key are different keys.
- Live mode requires an explicit config flag and a completed paper track record.
- Idempotent client order IDs.
- Bracket orders (entry, stop, target) where the playbook requires them.
- Reconcile fills against the intended trade.
- If Alpaca is down, do not assume the order filled.

### 3.5 Machine learning

Models are **rankers and tools**, not a second ungoverned trader.

- Rank SCAN hits so only the best names pay for a full LLM graph run.
- Help RegimeBot when rule-based confidence is in the middle.
- Feed realized profit and loss back into TradingAgents memory so later debates see outcomes.
- Optional later: a cheaper policy net for names that never clear the LLM cost bar.

Hard risk rules override any model score.

---

## 4. Professional market-regime practices used by this project

A market regime is a **persistent statistical state** of prices, not a feeling and not a single indicator print. Professional trend-followers, CTAs, and multi-strategy desks separate three questions that beginners often mash together:

1. **Direction** — up or down?
2. **Character** — trending or ranging?
3. **Volatility** — calm or stressed?

This project’s labels collapse direction and character into three trading states, and keep volatility as a **sizing and veto** layer:

| Project label | Professional meaning | Allowed playbook |
|---|---|---|
| UP | Confirmed uptrend | `UP-*` only |
| DOWN | Confirmed downtrend | `DOWN-*` only |
| SIDE | Range / no confirmed trend, or signals disagree | `SIDE-*` only, or cash |

If the detectors disagree, the default is **SIDE or cash**, not a forced trend trade. That is the professional default: do not pay for a trend playbook when the trend is not there.

### 4.1 Why regimes exist in professional books

Trend-following (time-series momentum) and mean reversion do not work in the same tape.

- In a **trend**, pullbacks resume and breakouts follow through. Fading the move loses.
- In a **range**, breakouts fail and extremes revert. Chasing the move loses.
- In **high volatility**, both can be right on direction and still lose money because size is too large.

Managed-futures and CTA practice (the Moskowitz, Ooi, and Pedersen time-series momentum line of work, and later regime-dependent allocation research) treats the regime as an input to **exposure**, not as a story. Position size should come from expected risk in that state, not from a fixed “always 100 shares” rule.

Man Group’s regime work makes the same practical point from another angle: define the state more than one way, then see which strategies have actually worked in that state. Trend has historically been the strategy that keeps working across more definitions. Mean reversion has not.

### 4.2 The detectors RegimeBot will use

No single detector is trusted. RegimeBot requires **agreement across layers**. A label is UP or DOWN only when the trend layer and the direction layer agree, and the veto layer is not in stress.

**Layer A — Trend vs range (character)**

- **ADX (Average Directional Index).** ADX measures strength, not direction.
  - ADX below 20: treat as range. Mean reversion is eligible. Trend playbooks are not.
  - ADX 20 to 25: transition. Reduce size or stand aside.
  - ADX above 25: trending. Trend playbooks are eligible.
  - ADX above 40: strong trend. Do not fade it. Watch for exhaustion, do not auto-reverse.
- **Moving-average structure.** 50-day above 100-day above 200-day, and price above the 200-day, is the simple institutional uptrend filter. Inverted stack is the downtrend filter. Interwoven averages are a transition, not a trend.
- **Price structure.** Higher highs and higher lows vs lower highs and lower lows vs a box.
- **Optional later: rolling Hurst exponent.** Above about 0.55 supports persistence (trend). Below about 0.45 supports mean reversion. Between them, treat as noise and stand aside. Not required for v1.
- **Optional later: Hidden Markov Model** on index returns and volatility, with states mapped to bull / sideways / bear. Used as a second opinion, not as the only switch. Academic and CTA research uses HMMs this way because the state is hidden and only inferred from returns.

**Layer B — Direction**

- Sign of the trend: price vs 200-day, slope of the 50-day, and whether the MA stack is aligned or inverted.
- For a basket: the same test on the basket’s median name, plus the matching sector ETF.
- UP requires aligned up structure. DOWN requires aligned down structure. Anything else is SIDE.

**Layer C — Participation (breadth)**

A rising index with few stocks participating is not a healthy UP regime.

- Percent of index members above their 200-day moving average.
- Advance/decline behavior.
- New highs vs new lows.
- Narrowing breadth while the index rises is a late-cycle warning, not a reason to add trend risk.

Breadth is cross-sectional. Price trend is time-series. Professionals treat them as complements. Both must agree at major switches.

**Layer D — Volatility and stress (veto and sizing, not a fourth playbook)**

- Realized volatility and ATR versus their own recent history.
- VIX level and **VIX term structure**. Short-term implied vol above long-term (backwardation) is a stress state. Contango is the calmer state. Stress does not by itself flip UP to DOWN, but it **cuts size** and can force flat.
- Optional macro confirmation, not required for v1: credit spreads, yield-curve stress, high-yield vs Treasuries. These are risk-on / risk-off context for the News Analyst and for the veto layer.

**Layer E — Horizon**

- Weekly and daily bars set the strategic label (what playbook is allowed).
- Intraday noise does not flip the label.
- A regime must **persist** before it counts. Use hysteresis: require the new label for several closes, or a minimum hold of the old label, so one noisy day does not thrash the book. Professional definitions often refuse to call a regime that has not lasted on the order of weeks to months. This project can use a shorter swing horizon, but it still must not flip every day.

### 4.3 How a label is assigned

```
If stress veto is on (vol spike or VIX backwardation beyond limit):
    size = reduced or flat
If ADX < 20 or MA stack is tangled or breadth disagrees with price:
    label = SIDE
Else if up structure and ADX confirms trend and breadth is not breaking:
    label = UP
Else if down structure and ADX confirms trend:
    label = DOWN
Else:
    label = SIDE
```

Outputs are structured: label, confidence, horizon, which layers agreed, which veto fired. Free-text opinion is not the label.

RegimeBot runs on:

- the broad market (SPY, QQQ, IWM)
- sector ETFs
- each TickerBasket

A name can be dropped if its sector regime conflicts with the basket regime.

### 4.4 What each label is allowed to do

**UP — trend-following, long-biased**

- Enter on pullbacks in an established uptrend, not on the first green bar of a new story.
- Example playbook: `UP-HeikenAshi Trend Pullback`.
- Do not fade strength. Do not run mean-reversion shorts.

**DOWN — trend-following, short, defensive or cash**

- Short failed rallies, or reduce and hedge. Same trend logic, opposite direction.
- Example playbook: `DOWN-Short Momentum Fade`.
- Do not buy dips just because price looks “cheap” while the down structure is intact.

**SIDE — mean reversion**

- Fade extremes only inside a defined range (RSI or stochastic at the band, ATR band intact).
- Example playbook: `SIDE-Mean Reversion Oscillator`.
- If the range is not clean, prefer cash over a trade. SIDE is permission to fade, not an obligation to trade.

### 4.5 Position sizing (professional practice this project will copy)

- Risk a small fixed fraction per name (design default: about 1% of equity at the stop). Configurable.
- **Volatility targeting:** size inversely to recent ATR or realized volatility so a wild name does not dominate the book. This is standard in trend-following and risk-parity style books.
- Cap names per basket and correlation to the same sector.
- Cut size in the ADX 20–25 transition band and in the stress veto.
- Earnings blackout unless a playbook explicitly allows the event.
- Regime flip exits or hands the name to the other series. It does not leave an UP position open under a DOWN label.

### 4.6 Research rules that protect the regime logic

- **No lookahead.** Backtests may use only data that was published by that analysis date. TradingAgents v0.5.2 states this contract. This project keeps it.
- **Point-in-time universes.** A SCAN must not use today’s index membership to pick yesterday’s trades.
- **Costs.** Model spread, commissions, and borrow for shorts. A regime edge that dies after costs is not an edge.
- **Separate discovery from confirmation.** Fit detectors on one period. Judge them on a later period.
- **Record the run.** Every report stores date, version, provider, models, analysts, debate rounds, and data vendors.

---

## 5. Functional workflow

1. Manager loads config and mode (`paper` or `live`). Live is impossible unless the flag is set.
2. SCAN jobs write TickerBaskets (ticker, scan name, scores, timestamp).
3. RegimeBot tags market, sector, and basket using the layers in section 4.
4. Router attaches the matching playbook. Mismatch stops the path.
5. For each selected ticker, run `TradingAgentsGraph` with the analysis date and selected analysts.
6. Parallel analysts → bull/bear debate → trader proposal → risk debate → portfolio manager.
7. If approved and limits pass, the adapter sends an Alpaca **paper** order.
8. Monitor until the playbook exit or a regime flip.
9. Store the report with run settings. Feed realized P&L back into memory.

### 5.1 SCAN examples (filters, not orders)

- `SCAN-Upward Trend Momentum` — price above the 50-day, 50-day above the 200-day, RSI above 50, enough dollar volume, then optional ML rank.
- `SCAN-Breakdown Short Candidates` — inverted MA stack, weakness vs sector.
- `SCAN-Mean Reversion Range` — ADX low, price oscillating inside a band.
- `SCAN-High Relative Strength`
- `SCAN-Volatility Squeeze`

Deterministic filters first. Model rank second. Orders never.

### 5.2 Interfaces

- Unattended CLI style already in TradingAgents, for example a ticker, a date, selected analysts, and `--save`.
- Python call into `TradingAgentsGraph()` for a basket batch.
- Structured outputs for trade intent, regime, and basket records.
- Config in YAML or TOML: universe, risk, LLM, paper vs live.

---

## 6. Efficiency and security requirements

### 6.1 Efficiency

- Parallel SCANs. Parallel analysts (v0.5.2 behavior).
- Do not run a full LLM graph on the whole universe. Only ranked basket members.
- Cache bars by date.
- Bound debate rounds. On timeout, fail closed (no order).
- SCAN of a few thousand names should finish in minutes. Per-ticker time will be dominated by the LLM, not by Python.

### 6.2 Security

- No API keys in git. Environment variables or a vault only (`ALPACA_*`, LLM keys).
- TLS for every API.
- Least privilege: a SCAN cannot place orders. Only the execution adapter can, and only after the portfolio manager and the Manager both pass.
- Immutable audit log: settings hashes, decisions, orders, fills.
- Validate tickers and dates.
- Rate-limit and back off on Alpaca and the LLM.
- Paper mode must make a live order impossible.

### 6.3 Reliability

- Checkpoint graph state so a run can resume.
- Kill switch cancels working orders and can flatten via Alpaca.
- Markets closed means idle. That is acceptable.

---

## 7. Build order for a first-time builder

Do these in order. Do not skip ahead.

1. Close the old VS Code folder. Open a new folder.
2. Fork or clone TradingAgents. Install. Run the official CLI on **one** ticker in simulation.
3. Create an Alpaca **paper** account. Write a tiny script that pulls bars and does not order.
4. Add one SCAN that writes a JSON basket.
5. Add RegimeBot that labels SPY as UP, DOWN, or SIDE using the rules in section 4.
6. Run TradingAgents on the first few names in a basket.
7. Send paper orders only after portfolio-manager approval.
8. Add ML ranking only after that paper path works.
9. Live trading is out of scope until paper logs are understood.

Prompt rule for the in-editor AI: one task at a time, and ask it to list files before it writes them. Example:

> This repo is a fork of TauricResearch/TradingAgents. Do not replace LangGraph or the official agents. Add a module under extensions/ for SCAN-Upward Trend Momentum. It should read Alpaca paper bars, apply these filters, and write a TickerBasket JSON. No live orders. Show the files you will create first.

---

## 8. Milestones

1. Foundation — TradingAgents v0.5.2+ installed, paper Alpaca read-only.
2. SCAN and baskets — deterministic filters and storage.
3. RegimeBot — UP / DOWN / SIDE with confidence and layer agreement.
4. One playbook of each series wired into the Trader tools.
5. Execution adapter — paper orders from portfolio-manager approval.
6. ML ranker and reflection — cut graph spend, feed P&L into memory.
7. Security review and paper soak. Then live at tiny size.

## 9. Success criteria

- Paper path works end to end: SCAN → RegimeBot → graph → portfolio manager → Alpaca paper fill → exit rule.
- Zero live orders when mode is paper.
- Every report records run settings.
- Backtests refuse future bars.
- Strategy prefix always matches basket regime.
- The audit log can reconstruct why a ticker was bought or skipped.

## 10. Risks

| Risk | Mitigation |
|---|---|
| LLM non-determinism and cost | Structured schemas, debate caps, cheaper models for analysts |
| Lookahead in research | Date-aware data only |
| Alpaca rate limits | Cache, batch, backoff |
| Regime misclassification | SIDE or cash when layers disagree |
| Upstream API changes | Pin a TradingAgents version |
| Treating sim results as a live edge | Paper gate with written promotion rules |

## 11. Out of scope for v1

- Intraday or high-frequency trading.
- Crypto as the primary book.
- Replacing LangGraph.
- A mobile app.
- OpenClaw.ai and Quantide.io as required platforms.
- Four LLM providers at once.
- Live trading before a paper track record.

## 12. Stack

Python, LangGraph via TradingAgents, Pandas / NumPy, a technical-analysis library, Alpaca SDK, one LLM provider, optional PyTorch later for rankers.

## 13. Sources for the regime section

Practices above are the project’s operating rules, drawn from common institutional methods rather than from a single vendor:

- Trend vs range split, and ADX / ATR as the practical filters used on trading desks.
- Time-series momentum as the academic base for CTA-style trend exposure (Moskowitz, Ooi, Pedersen).
- Regime-dependent position sizing rather than a fixed +1 / −1 switch (later allocation research in that line).
- Multi-definition regimes and the historical tendency of trend to work across more states than mean reversion (Man Group regime work; AQR commodity and trend research is in the same family).
- Breadth plus a hidden-state model as complementary, not redundant, regime gates.
- VIX term structure (contango vs backwardation) as a stress veto.
- Volatility targeting as the default size rule.
- TradingAgents’ own research disclaimer and date-aware backtest contract.

---

*End of project record. Next concrete step, when building starts: close the old VS Code folder, open a new folder, clone TradingAgents, run one simulated ticker. No orders.*
