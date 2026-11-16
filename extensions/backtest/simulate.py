"""Bar-by-bar simulation of one plan, and the summary statistics.

No look-ahead: a plan is made from files and bars dated on or before the run date, and the simulation starts on the
first bar of the first session after it. Fill at that bar's open. Then, bar by bar (the fill bar included, after its
open): a gap past the stop or target exits at the open; a bar reaching the stop exits at the stop; a bar reaching the
target exits at the target; a bar reaching both counts as the stop. With neither, the trade exits at the close of the
last allowed session. When the cached bars end first, the trade is ``open_at_end``, marked at the last close and left
out of the statistics.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from extensions.backtest.levels import LONG, Plan

STOP, TARGET, TIME, OPEN_AT_END = "stop", "target", "time", "open_at_end"
BOTS = ("up", "down", "side")
BOT_LABEL = {"up": "Long / UPBot", "down": "Short / DOWNBot", "side": "Sideways / SIDEBot", "all": "All"}


class Skip(Exception):
    """The plan was not traded; the message says why."""


def simulate(plan: Plan, bars: pd.DataFrame, after: date, max_sessions: int | None) -> dict:
    """Simulate ``plan`` on ``bars`` from the first session after ``after``. Raises Skip when no trade is taken.
    ``max_sessions`` None holds the trade until its stop or target (no time exit): one that reaches neither before
    the bars end is ``open_at_end``."""
    future = bars[bars["session"] > after]
    if future.empty:
        raise Skip(f"no cached bars after {after}")
    sessions = list(dict.fromkeys(future["session"]))
    if max_sessions is not None:
        sessions = sessions[:max_sessions]
    window = future[future["session"].isin(sessions)]
    sign = 1 if plan.side == LONG else -1
    fill = round(float(window["open"].iloc[0]), 4)
    risk = sign * (fill - plan.stop)
    if not risk > 0:
        raise Skip(f"opened {fill:g}, already past the stop {plan.stop:g}")
    if plan.max_risk is not None and risk > plan.max_risk:
        raise Skip(f"risk {risk:.2f} at the fill {fill:g} is over 3 x ATR ({plan.max_risk:.2f})")
    target = plan.target if plan.target is not None else round(fill + sign * plan.target_r * risk, 2)
    if not sign * (target - fill) > 0:
        raise Skip(f"opened {fill:g}, already past the target {target:g}")

    exit_price = exit_reason = exit_session = None
    for i, bar in enumerate(window.itertuples(index=False)):
        if i > 0:  # a gap past a level fills at the open
            if sign * (bar.open - plan.stop) <= 0:
                exit_price, exit_reason = bar.open, STOP
            elif sign * (bar.open - target) >= 0:
                exit_price, exit_reason = bar.open, TARGET
        if exit_reason is None:
            hit_stop = (bar.low if sign > 0 else bar.high) * sign <= plan.stop * sign
            hit_target = (bar.high if sign > 0 else bar.low) * sign >= target * sign
            if hit_stop:
                exit_price, exit_reason = plan.stop, STOP
            elif hit_target:
                exit_price, exit_reason = target, TARGET
        if exit_reason is not None:
            exit_session = bar.session
            break
    if exit_reason is None:
        last = window.iloc[-1]
        exit_price, exit_session = float(last["close"]), last["session"]
        exit_reason = TIME if max_sessions is not None and len(sessions) >= max_sessions else OPEN_AT_END

    held = sessions.index(exit_session) + 1
    return {
        "bot": plan.bot, "symbol": plan.symbol, "side": plan.side, "levels_source": plan.levels_source,
        "reference_entry": plan.reference_entry, "entry_date": sessions[0].isoformat(), "entry": fill,
        "stop": plan.stop, "target": target, "exit_date": exit_session.isoformat(),
        "exit": round(float(exit_price), 4), "exit_reason": exit_reason,
        "r": round(sign * (float(exit_price) - fill) / risk, 3), "risk_per_share": round(risk, 4),
        "sessions_held": held, "detail": plan.detail,
    }


def _drawdown(values: list[float]) -> float:
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = max(worst, (peak - v) / peak * 100 if peak > 0 else 0.0)
    return round(worst, 3)


def summarize(trades: list[dict], risk_pct: float, start: date, bots: tuple[str, ...] = BOTS,
              origins: tuple[str, ...] = ("replay",)) -> tuple[list, list]:
    """(summary rows, equity curve). Each closed trade risks ``risk_pct`` of the starting equity (100), no
    compounding: its return is R x risk_pct. One row per origin and strategy (plus an "all" row per origin); the two
    origins are never added together. The curve has one value per origin ("replay", "reconstructed": its combined
    equity), at exit dates; open_at_end trades are left out."""
    closed = sorted((t for t in trades if t["exit_reason"] != OPEN_AT_END),
                    key=lambda t: (t["exit_date"], t["entry_date"], t["symbol"]))
    rows = []
    for origin in origins:
        for g in [*bots, "all"]:
            def mine(t, g=g, origin=origin):
                return t.get("origin", "replay") == origin and (g == "all" or t["bot"] == g)
            rs = [t["r"] for t in closed if mine(t)]
            equity = [100.0]
            for r in rs:
                equity.append(equity[-1] + r * risk_pct)
            wins = sum(1 for r in rs if r > 0)
            rows.append({
                "origin": origin, "strategy": g, "label": BOT_LABEL[g], "trades": len(rs),
                "open": sum(1 for t in trades if t["exit_reason"] == OPEN_AT_END and mine(t)),
                "wins": wins, "win_rate": round(wins / len(rs) * 100, 2) if rs else None,
                "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 3),
                "total_return_pct": round(sum(rs) * risk_pct, 3), "max_drawdown_pct": _drawdown(equity),
            })

    curve = [{"date": start.isoformat(), **{o: 100.0 for o in origins}}]
    level = {o: 100.0 for o in origins}
    for t in closed:
        origin = t.get("origin", "replay")
        if origin not in level:
            continue
        level[origin] += t["r"] * risk_pct
        point = {"date": t["exit_date"], **{o: round(level[o], 4) for o in origins}}
        if curve[-1]["date"] == point["date"]:
            curve[-1] = point
        else:
            curve.append(point)
    return rows, curve
