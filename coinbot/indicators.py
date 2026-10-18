"""Plain-Python technical indicators."""

from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values: list[float], period: int = 14) -> float:
    """Wilder's RSI of the last value. Returns 50 when there isn't enough data."""
    if len(values) <= period:
        return 50.0
    gains = losses = 0.0
    for prev, cur in zip(values[:period], values[1 : period + 1]):
        change = cur - prev
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for prev, cur in zip(values[period:], values[period + 1 :]):
        change = cur - prev
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
