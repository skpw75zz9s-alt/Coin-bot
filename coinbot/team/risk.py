"""Risk manager: decides whether and how much the desk may buy, and pulls the kill switch."""

from __future__ import annotations

from ..market import Ticker


def check_drawdown(equity: float, peak: float, cfg) -> str | None:
    if peak > 0 and equity < peak * (1 - cfg.max_drawdown_pct):
        return (f"drawdown {1 - equity / peak:.1%} from peak ${peak:,.2f} "
                f"exceeded {cfg.max_drawdown_pct:.0%} limit")
    return None


def daily_limit_hit(equity: float, day_start: float, cfg) -> bool:
    return day_start > 0 and equity < day_start * (1 - cfg.daily_loss_limit_pct)


def position_size(ticker: Ticker, equity: float, spendable: float, stop_loss_pct: float, cfg) -> float:
    """Dollar amount to buy, or 0 if the trade is too small to be worth it."""
    size = min(
        equity * cfg.risk_per_trade_pct / stop_loss_pct,  # loss at the stop ~= risk_per_trade
        equity * cfg.max_position_pct,
        ticker.volume_usd_24h * cfg.max_volume_share,
        spendable,
    )
    return round(size, 2) if size >= cfg.min_order_usd else 0.0
