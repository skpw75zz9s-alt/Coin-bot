"""Analyst: looks for volume-backed breakouts in an uptrend."""

from __future__ import annotations

from dataclasses import dataclass

from ..indicators import ema, rsi
from ..market import Candle, Ticker


@dataclass(frozen=True)
class Signal:
    instrument: str
    score: float
    reason: str


def analyse(ticker: Ticker, candles: list[Candle], cfg) -> Signal | None:
    need = max(cfg.ema_slow, cfg.breakout_lookback, cfg.rsi_period) + 2
    if len(candles) < need:
        return None
    if ticker.change_24h > cfg.max_change_24h:
        return None  # already went vertical; chasing these is how you buy the top

    closes = [c.close for c in candles]
    last = candles[-1]
    window = candles[-1 - cfg.breakout_lookback : -1]
    prior_high = max(c.high for c in window)
    avg_volume = sum(c.volume for c in window) / len(window)
    if avg_volume <= 0 or last.close <= prior_high:
        return None

    surge = last.volume / avg_volume
    if surge < cfg.volume_surge:
        return None
    fast, slow = ema(closes, cfg.ema_fast)[-1], ema(closes, cfg.ema_slow)[-1]
    if fast <= slow:
        return None
    if last.open > 0 and last.close / last.open - 1 > cfg.max_candle_gain:
        return None
    # A breakout candle always pushes RSI up; what matters is that the coin
    # wasn't already stretched going into it.
    strength = rsi(closes[:-1], cfg.rsi_period)
    if strength > cfg.rsi_max:
        return None

    breakout = last.close / prior_high - 1
    return Signal(
        instrument=ticker.instrument,
        score=surge * (1 + breakout * 10),
        reason=f"breakout +{breakout:.1%} over {cfg.breakout_lookback}-bar high, "
               f"volume x{surge:.1f}, prior RSI {strength:.0f}",
    )
