# BATS Brightrise Algorithmic Trading Systemt — Project Record

**Owner:** Jay Tamez  
**Last updated:** 2026-10-02  
**Status:** Research path works. No live trading. No Alpaca orders.  
**Fork:** https://github.com/jaytameztempo/TradingAgents  
**This file is not financial, investment, or trading advice.**

This document records the project decisions, the bot names, and the market-regime rules RegimeBot follows.

---

## 1. What this project is

An automated **swing-trading** system for US stocks. It scans names, decides whether the market is trending up, trending down, or moving sideways, then writes a plan with the bot that matches that regime.

Holding period target: days to weeks. Not day trading. Not high-frequency trading.

The system is built in small steps on a fork of TradingAgents. Orders are not enabled.

---

## 2. Decisions already made

### 2.1 Core framework

Use **TradingAgents** by Tauric Research as the decision engine.

- Upstream: https://github.com/TauricResearch/TradingAgents
- This project's fork: https://github.com/jaytameztempo/TradingAgents
- Paper: *TradingAgents: Multi-Agents LLM Financial Trading Framework* (arXiv 2412.20138)
- License: Apache-2.0
- Language: Python 3.11+ (this machine uses 3.13)
- Orchestration: LangGraph
- Pinned line: **v0.5.2**

TradingAgents is a research framework, not a broker terminal. Results vary with model, temperature, period, and data quality. It is not investment advice.

### 2.2 Broker, language, models

- **Broker API:** Alpaca Markets — https://alpaca.markets/
- **Language:** Python
- **LLM in use:** Google Gemini, provider name `google`, key `GOOGLE_API_KEY`
- Other providers (Grok, ChatGPT, Claude) stay unused until one path is reliable

### 2.3 Bot names

RegimeBot only assigns a label. The playbooks are separate bots.

| Name | Role | When it may run |
|---|---|---|
| `SCAN-` | Builds a TickerBasket. Does not trade. | Always, for its scan |
| RegimeBot | Labels the market UP, DOWN, or SIDE. Does not trade. | Always, for its symbol and date |
| UPBot | Uptrend playbook | Only when the regime label is UP |
| DOWNBot | Downtrend playbook | Only when the regime label is DOWN |
| SIDEBot | Sideways / mean-reversion playbook | Only when the regime label is SIDE |

A mismatch is a hard reject. UPBot never runs on a DOWN or SIDE label. DOWNBot never runs on an UP or SIDE label. SIDEBot never runs on an UP or DOWN label.

Older names `UP-`, `DOWN-`, and `SIDE-` are retired. Use UPBot, DOWNBot, and SIDEBot.

---

## 3. Product shape

### 3.1 Roles

**Manager** is the supervisor. It is the only process allowed to launch the firm graph, change baskets, or send broker orders. It is not built yet. It does not replace TradingAgents.

**SCAN-*** algorithms return lists of names called **TickerBaskets**. A SCAN does not place orders.

**RegimeBot** labels the market, a sector, an industry, or a basket: **UP**, **DOWN**, or **SIDE**.

**UPBot, DOWNBot, and SIDEBot** are the playbooks. The router pairs a bot with a basket only when the label matches.

### 3.2 Layers

```
Manager (not built yet)
  ├── SCAN-* workers          → TickerBaskets
  ├── RegimeBot               → UP | DOWN | SIDE
  ├── Strategy router         → UPBot | DOWNBot | SIDEBot
  └── TradingAgentsGraph (research only until an order step is approved)
        ├── Analyst Team
        ├── Researcher Team: Bull vs Bear + Research Manager
        ├── Trader
        ├── Risk Team
        └── Portfolio Manager → approve or reject
              └── Execution adapter → not built; paper only, later
```

### 3.3 What each bot is allowed to do

**UPBot — trend-following, long-biased**

- Enter on pullbacks in an established uptrend, not on the first green bar.
- Example plan: Heikin Ashi trend pullback.
- Do not fade strength. Do not run mean-reversion shorts.
- Not built. The 2026-09-30 router blocked it.

**DOWNBot — trend-following, short, defensive, or cash**

- Short failed rallies, or reduce and hedge.
- Example plan: short momentum fade.
- Do not buy dips just because price looks cheap while the down structure is intact.
- Not built.

**SIDEBot — mean reversion, or cash**

- Fade extremes only inside a defined range.
- Example plan: mean-reversion oscillator.
- If the range is not clean, prefer cash. SIDE is permission to plan a fade, not an obligation to trade.
- Not built. The next build step is a SIDEBot that only writes a plan. It must not call Alpaca's trading client.

### 3.4 Execution

No order path exists. Alpaca is used only to read daily bars.

- Paper key only. A live key must not be pasted into `.env`.
- Live mode is out of scope until a paper track record exists.
- A future adapter may send a paper order only after the router allows the bot and the portfolio manager approves.
- If Alpaca is down, do not assume an order filled.

### 3.5 Machine learning

Not in this version. Later models may rank SCAN hits or help RegimeBot. Hard risk rules override any model score.

---

## 4. Professional market-regime practices

A market regime is a persistent statistical state of prices, not a feeling and not a single indicator print. Three questions stay separate:

1. **Direction** — up or down?
2. **Character** — trending or ranging?
3. **Volatility** — calm or stressed?

This project uses three labels. Volatility is a sizing and veto layer, not a fourth bot.

| Label | Meaning | Bot allowed |
|---|---|---|
| UP | Confirmed uptrend | UPBot only |
| DOWN | Confirmed downtrend | DOWNBot only |
| SIDE | Range, weak trend, tangled averages, or signals disagree | SIDEBot only, or cash |

If the detectors disagree, the default is SIDE or cash, not a forced trend trade.

### 4.1 Rules in the current RegimeBot

Built for one symbol, SPY by default, on a given date. Read-only bars. No orders.

- SIDE if there are fewer than 200 daily bars.
- SIDE if the 50-day and 200-day averages are tangled: the 50-day is within 1% of the 200-day, or the two crossed in the last 20 trading days.
- SIDE if 14-day ADX is under 20.
- UP only if the close is above the 50-day, the 50-day is above the 200-day, and ADX is at least 25.
- DOWN only if the close is below the 50-day, the 50-day is below the 200-day, and ADX is at least 25.
- Anything else, including ADX from 20 to 25, is SIDE.

Not built yet: breadth, VIX term-structure veto, confidence, and hysteresis. Those stay in the spec for a later pass.

### 4.2 Detectors still to add

- Breadth: percent of members above the 200-day, advance/decline, new highs versus new lows.
- Volatility veto: ATR or realized vol versus its own history; VIX backwardation cuts size or forces flat. It does not by itself flip UP to DOWN.
- Hysteresis: a new label must persist for several closes before the bot changes.
- Optional later: Hurst exponent, hidden Markov model as a second opinion.

### 4.3 Position sizing, when orders exist

- About 1% of equity at the stop, configurable.
- Size inversely to recent ATR.
- Cap names per basket and sector correlation.
- Cut size when ADX is 20 to 25 or a stress veto is on.
- A regime flip exits or hands the name to the other bot. An UPBot position must not stay open under a DOWN label.

### 4.4 Research rules

- No lookahead. A scan or label may use only bars dated on or before the as-of date.
- Record the run: date, label, inputs, and the files read.
- Costs matter later. An edge that dies after spread and commission is not an edge.

---

## 5. What is already built

All of this is read-only. None of it places an order.

| Step | Result |
|---|---|
| TradingAgents v0.5.2 | Installed. One NVDA research report ran. |
| Gemini | Provider `google`. Deep and quick models set in `.env`. |
| Alpaca paper bars | SPY, 85 daily bars, 2026-06-01 to 2026-09-30, last close 762.34. |
| SCAN-Upward Trend Momentum | 2026-09-30: MSFT, NVDA, AAPL, QQQ passed. SPY failed. |
| RegimeBot | SPY 2026-09-30: SIDE, because ADX 10.7 is under 20. |
| Router | UPBot blocked. Passed names are not tradeable under UP. |
| Research runner | NVDA research ran while blocked. Rating Hold. No order. |
| GitHub | Fork `jaytameztempo/TradingAgents`, 2 commits ahead of upstream. |

Code lives under `extensions/`. Keys live in `.env` only. `.env` is not committed.

---

## 6. Workflow

1. SCAN writes a TickerBasket.
2. RegimeBot labels SPY for that date.
3. Router allows only the matching bot: UPBot, DOWNBot, or SIDEBot.
4. A blocked bot does not trade. Research may still run and must say no order was placed.
5. A future paper order can leave only after the router allows the bot and a later approval step says yes.
6. Store the report and the decision JSON.

### 6.1 SCAN examples

- `SCAN-Upward Trend Momentum` — built: at least 200 bars, close above the 50-day, 50-day above the 200-day. RSI and dollar volume are not in this version.
- `SCAN-Breakdown Short Candidates` — not built.
- `SCAN-Mean Reversion Range` — not built.

---

## 7. Efficiency and security

- Do not run a full LLM graph on the whole universe.
- No API keys in git.
- Paper key and live key are different. Paper mode must make a live order impossible.
- A SCAN, RegimeBot, router, and research runner cannot place orders.
- Only a future execution adapter may place an order, and only after approval.
- On timeout, fail closed. No order.

---

## 8. Build order from here

1. SIDEBot writes a plan for a name when the label is SIDE. No Alpaca trading client.
2. UPBot and DOWNBot write plans only when their label matches. No orders.
3. Paper orders only after a later, explicit approval step.
4. ML ranking only after a paper path exists.
5. Live trading is out of scope until paper logs are understood.

Prompt rule: one task at a time. Ask for the file list before any files are written.

---

## 9. Success criteria

- SCAN, RegimeBot, and the router agree on the bot name for a date.
- UPBot is blocked when the label is SIDE, as it was on 2026-09-30.
- Research can run while blocked and still places no order.
- Zero live orders.
- The JSON files can reconstruct why a name was planned or skipped.

## 10. Out of scope for this version

- Intraday or high-frequency trading.
- Crypto as the primary book.
- Replacing LangGraph.
- A mobile app.
- Four LLM providers at once.
- Live trading.
- Alpaca order placement.

## 11. Stack

Python, LangGraph via TradingAgents, Pandas, Alpaca's market-data client, Gemini. PyTorch is optional later.

---

*End of project record. Next build step: SIDEBot writes a plan only. It must not call Alpaca's trading client.*
