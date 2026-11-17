"""Bar-by-bar simulation of one plan, and the summary statistics.

No look-ahead: a plan is made from files and bars dated on or before the run date, and the simulation starts on the
first bar of the first session after it.

Entry:
- with ``entry_limit`` (SIDE, as live: a DAY limit at the channel line), the first ``entry_sessions`` sessions are
  searched bar by bar: an open at or through the limit fills at the open, else a bar reaching the limit fills at the
  limit; not reached, no trade. The stop is then ``stop_offset`` from the fill (``paper_tickets.stop_for_fill``);
- without it (UP / DOWN), the fill is the first bar's open.

Exit, bar by bar from the fill bar: a gap past the stop or target exits at the open; a bar reaching the stop exits
at the stop; a bar reaching the target exits at the target; both in one bar counts as the stop. On a bar filled at
the limit inside the bar, only the stop counts (which came first is unknown). With neither, the trade exits at the
close of the last allowed session (``max_sessions``), or with ``max_sessions`` None it is held: when the bars run
out it is ``open_at_end``, marked at the last close and left out of the statistics (the engine relabels it
``bars_end`` when its bars stopped well before the run's data end).

Slippage (``slippage_bps``) moves market-like prices against the trade: an entry at the open, and exits at the stop,
at a gap, at the time limit or at the last mark. Limit fills and target fills get none.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from extensions.backtest.levels import LONG, Plan
from extensions.status.paper_tickets import stop_for_fill

STOP, TARGET, TIME, OPEN_AT_END, BARS_END = "stop", "target", "time", "open_at_end", "bars_end"
NOT_CLOSED = (OPEN_AT_END, BARS_END)
BOTS = ("up", "down", "side")
BOT_LABEL = {"up": "Long / UPBot", "down": "Short / DOWNBot", "side": "Sideways / SIDEBot", "all": "All"}
OUTLIER_R = 5.0


class Skip(Exception):
    """The plan was not traded; the message says why."""


def simulate(plan: Plan, bars: pd.DataFrame, after: date, max_sessions: int | None, *,
             entry_limit: float | None = None, entry_sessions: int = 1, slippage_bps: float = 0.0) -> dict:
    """Simulate ``plan`` on ``bars`` from the first session after ``after``. Raises Skip when no trade is taken.
    ``max_sessions`` None holds the trade until its stop or target (no time exit)."""
    future = bars[bars["session"] > after].reset_index(drop=True)
    if future.empty:
        raise Skip(f"no cached bars after {after}")
    sign = 1 if plan.side == LONG else -1
    slip = max(slippage_bps, 0.0) / 10_000

    def against_entry(price: float) -> float:
        return price * (1 + sign * slip)

    def against_exit(price: float) -> float:
        return price * (1 - sign * slip)

    if entry_limit is None:
        start, at_open = 0, True
        fill = round(against_entry(float(future["open"].iloc[0])), 4)
        stop = plan.stop
    else:
        window = set(list(dict.fromkeys(future["session"]))[:max(entry_sessions, 1)])
        start = None
        for i, bar in enumerate(future.itertuples(index=False)):
            if bar.session not in window:
                break
            if sign * (bar.open - entry_limit) <= 0:
                start, at_open, fill = i, True, float(bar.open)
                break
            if sign * (bar.low if sign > 0 else bar.high) <= sign * entry_limit:
                start, at_open, fill = i, False, float(entry_limit)
                break
        if start is None:
            raise Skip(f"limit {entry_limit:g} not reached in {entry_sessions} session(s)")
        fill = round(fill, 4)
        if plan.stop_offset:
            try:
                stop = stop_for_fill(plan.side, plan.stop_offset, fill)
            except ValueError as exc:
                raise Skip(str(exc)) from None
        else:
            stop = plan.stop

    risk = sign * (fill - stop)
    if not risk > 0:
        raise Skip(f"opened {fill:g}, already past the stop {stop:g}")
    if plan.max_risk is not None and risk > plan.max_risk:
        raise Skip(f"risk {risk:.2f} at the fill {fill:g} is over 3 x ATR ({plan.max_risk:.2f})")
    target = plan.target if plan.target is not None else round(fill + sign * plan.target_r * risk, 2)
    if not sign * (target - fill) > 0:
        raise Skip(f"opened {fill:g}, already past the target {target:g}")

    rest = future.iloc[start:]
    sessions = list(dict.fromkeys(rest["session"]))
    if max_sessions is not None:
        sessions = sessions[:max_sessions]
    window_bars = rest[rest["session"].isin(sessions)]

    exit_price = exit_reason = exit_session = None
    for i, bar in enumerate(window_bars.itertuples(index=False)):
        if i > 0:  # a gap past a level fills at the open
            if sign * (bar.open - stop) <= 0:
                exit_price, exit_reason = against_exit(bar.open), STOP
            elif sign * (bar.open - target) >= 0:
                exit_price, exit_reason = bar.open, TARGET
        if exit_reason is None:
            hit_stop = (bar.low if sign > 0 else bar.high) * sign <= stop * sign
            hit_target = (bar.high if sign > 0 else bar.low) * sign >= target * sign and (i > 0 or at_open)
            if hit_stop:
                exit_price, exit_reason = against_exit(stop), STOP
            elif hit_target:
                exit_price, exit_reason = target, TARGET
        if exit_reason is not None:
            exit_session = bar.session
            break
    if exit_reason is None:
        last = window_bars.iloc[-1]
        exit_price, exit_session = against_exit(float(last["close"])), last["session"]
        exit_reason = TIME if max_sessions is not None and len(sessions) >= max_sessions else OPEN_AT_END

    r = round(sign * (float(exit_price) - fill) / risk, 3)
    return {
        "bot": plan.bot, "symbol": plan.symbol, "side": plan.side, "levels_source": plan.levels_source,
        "reference_entry": plan.reference_entry, "entry_type": "open" if entry_limit is None else "limit",
        "entry_date": sessions[0].isoformat(), "entry": fill, "stop": stop, "target": target,
        "exit_date": exit_session.isoformat(), "exit": round(float(exit_price), 4), "exit_reason": exit_reason,
        "r": r, "risk_per_share": round(risk, 4), "sessions_held": sessions.index(exit_session) + 1,
        "outlier": r > OUTLIER_R, "detail": plan.detail,
    }


def _drawdown(values: list[float]) -> float:
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak * 100 if peak > 0 else 0.0)
    return round(worst, 3)


def _peaks(trades: list[dict], capital: float) -> tuple[int, float]:
    """(most positions open on one session, most open stop-risk as % of capital), over every trade taken."""
    best_n, best_risk = 0, 0.0
    for day in sorted({t["entry_date"] for t in trades}):
        open_now = [t for t in trades if t["entry_date"] <= day <= t["exit_date"]]
        best_n = max(best_n, len(open_now))
        best_risk = max(best_risk, sum(t.get("risk_dollars") or 0.0 for t in open_now))
    return best_n, round(best_risk / capital * 100, 3) if capital else 0.0


def summarize(trades: list[dict], capital: float, start: date, bots: tuple[str, ...] = BOTS,
              origins: tuple[str, ...] = ("replay",)) -> tuple[list, list]:
    """(summary rows, equity curve). Dollars come from each trade's whole-share ``pnl_usd`` on the starting
    ``capital`` (no compounding); returns are % of that capital. One row per origin and strategy (plus an "all" row
    per origin); the two origins are never added together. The curve has one value per origin, 100 x (capital +
    closed P/L) / capital at exit dates; trades not closed (open at the end, or bars ended) are left out of the
    figures and counted on their own."""
    closed = sorted((t for t in trades if t["exit_reason"] not in NOT_CLOSED),
                    key=lambda t: (t["exit_date"], t["entry_date"], t["symbol"]))
    rows = []
    for origin in origins:
        for g in [*bots, "all"]:
            def mine(t, g=g, origin=origin):
                return t.get("origin", "replay") == origin and (g == "all" or t["bot"] == g)
            done = [t for t in closed if mine(t)]
            rs = [t["r"] for t in done]
            usd = [t.get("pnl_usd") or 0.0 for t in done]
            equity = [100.0]
            for v in usd:
                equity.append(equity[-1] + v / capital * 100)
            wins = sum(1 for r in rs if r > 0)
            taken = [t for t in trades if mine(t)]
            peak_n, peak_risk = _peaks(taken, capital)
            total_usd = round(sum(usd), 2)
            rows.append({
                "origin": origin, "strategy": g, "label": BOT_LABEL[g], "trades": len(rs),
                "open": sum(1 for t in taken if t["exit_reason"] == OPEN_AT_END),
                "bars_end": sum(1 for t in taken if t["exit_reason"] == BARS_END),
                "wins": wins, "win_rate": round(wins / len(rs) * 100, 2) if rs else None,
                "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 3),
                "total_r_ex_best": round(sum(rs) - max(rs), 3) if rs else 0.0,
                "total_return_usd": total_usd,
                "total_return_usd_ex_best": round(total_usd - max(usd), 2) if usd else 0.0,
                "total_return_pct": round(total_usd / capital * 100, 3),
                "max_drawdown_pct": _drawdown(equity),
                "peak_positions": peak_n, "peak_open_risk_pct": peak_risk,
                "outliers": sum(1 for t in done if t.get("outlier")),
            })

    curve = [{"date": start.isoformat(), **{o: 100.0 for o in origins}}]
    level = {o: 100.0 for o in origins}
    for t in closed:
        origin = t.get("origin", "replay")
        if origin not in level:
            continue
        level[origin] += (t.get("pnl_usd") or 0.0) / capital * 100
        point = {"date": t["exit_date"], **{o: round(level[o], 4) for o in origins}}
        if curve[-1]["date"] == point["date"]:
            curve[-1] = point
        else:
            curve.append(point)
    return rows, curve
