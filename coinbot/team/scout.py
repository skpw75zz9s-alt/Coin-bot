"""Scout: finds which memecoins are liquid enough to trade right now."""

from __future__ import annotations

from ..market import Ticker


def _norm(name: str) -> str:
    return name.replace("_", "").replace("-", "").upper()


def scout(tickers: dict[str, Ticker], universe: list[str], cfg) -> list[Ticker]:
    # Accept "DOGE_USD" and "DOGEUSD" alike; the ticker keeps the exchange's own spelling.
    by_norm = {_norm(k): t for k, t in tickers.items()}
    picks = []
    for name in universe:
        t = by_norm.get(_norm(name))
        if t is None or t.bid <= 0 or t.ask <= 0 or t.ask < t.bid:
            continue
        if t.volume_usd_24h < cfg.min_volume_usd_24h or t.spread_pct > cfg.max_spread_pct:
            continue
        picks.append(t)
    return sorted(picks, key=lambda t: t.volume_usd_24h, reverse=True)
