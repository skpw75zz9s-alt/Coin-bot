"""Treasurer: ring-fences part of every profit so the subscription gets paid first."""

from __future__ import annotations


def month_key(now_iso: str) -> str:
    return now_iso[:7]


def record_realized(state: dict, pnl: float, now_iso: str, cfg) -> float:
    """Book a closed trade's P&L and sweep profit into the reserve. Returns the amount swept.

    Losses carry forward: the desk has to win back what it lost before it can sweep again,
    so the reserve only ever holds money that was actually earned.
    """
    month = state["months"].setdefault(month_key(now_iso), {"swept": 0.0, "realized": 0.0})
    month["realized"] += pnl
    state["realized_pnl_total"] += pnl
    state["unswept_profit"] += pnl
    if state["unswept_profit"] <= 0:
        return 0.0

    goal = cfg.monthly_subscription_usd
    profit = state["unswept_profit"]
    # Sweep at the high rate until this month's goal is met, then at the lower rate.
    to_goal = max(0.0, goal - month["swept"])
    high_rate_profit = min(profit, to_goal / cfg.sweep_pct) if cfg.sweep_pct > 0 else 0.0
    swept = high_rate_profit * cfg.sweep_pct + (profit - high_rate_profit) * cfg.sweep_pct_after_goal
    swept = round(swept, 6)
    state["unswept_profit"] = 0.0
    state["reserve_usd"] += swept
    month["swept"] += swept
    # Reserve money leaves the trading pot, so it shouldn't look like a drawdown.
    if state.get("peak_equity"):
        state["peak_equity"] -= swept
    if state.get("day"):
        state["day"]["start_equity"] -= swept
    return swept


def coverage(state: dict, now_iso: str, cfg) -> dict:
    goal = cfg.monthly_subscription_usd
    swept = state["months"].get(month_key(now_iso), {}).get("swept", 0.0)
    return {
        "goal": goal,
        "swept_this_month": swept,
        "covered_this_month": swept >= goal,
        "reserve": state["reserve_usd"],
        "months_prepaid": int(state["reserve_usd"] // goal) if goal > 0 else 0,
    }
