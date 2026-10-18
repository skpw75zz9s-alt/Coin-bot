"""Trader: decides when an open position should be closed."""

from __future__ import annotations


def exit_reason(pos: dict, bid: float, now_ms: int, cfg) -> str | None:
    entry = pos["entry_price"]
    peak = pos["peak_price"]
    if bid <= entry * (1 - cfg.stop_loss_pct):
        return "stop-loss"
    if bid >= entry * (1 + cfg.take_profit_pct):
        return "take-profit"
    if peak >= entry * (1 + cfg.trail_activate_pct) and bid <= peak * (1 - cfg.trail_pct):
        return "trailing-stop"
    if now_ms - pos["entry_ts"] >= cfg.max_hold_hours * 3_600_000:
        return "time-stop"
    return None
