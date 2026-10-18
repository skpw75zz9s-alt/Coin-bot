"""Market data: Crypto.com Exchange public API, plus a historical replay for backtests."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_BASE = "https://api.crypto.com/exchange/v1/"

TIMEFRAME_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "12h": 43_200_000,
    "1D": 86_400_000,
}


@dataclass(frozen=True)
class Ticker:
    instrument: str
    bid: float
    ask: float
    last: float
    volume_usd_24h: float
    change_24h: float  # fraction, e.g. 0.12 for +12%

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.mid > 0 else 1.0


@dataclass(frozen=True)
class Candle:
    ts: int  # candle open time, epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float  # base units

    @property
    def volume_usd(self) -> float:
        return self.volume * self.close


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def http_get_json(url: str, timeout: float = 15.0, retries: int = 3) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "coinbot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as err:  # network blips are expected; retry with backoff
            last_err = err
            time.sleep(2**attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_err}")


class CryptoComMarket:
    """Read-only market data. No API key needed."""

    def __init__(self, base: str = API_BASE, get_json=http_get_json):
        self.base = base
        self._get_json = get_json

    def _public(self, method: str, **params) -> list:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        payload = self._get_json(f"{self.base}public/{method}" + (f"?{query}" if query else ""))
        if payload.get("code", 0) != 0:
            raise RuntimeError(f"{method} error {payload.get('code')}: {payload.get('message')}")
        return payload.get("result", {}).get("data", []) or []

    def tickers(self) -> dict[str, Ticker]:
        out = {}
        for row in self._public("get-tickers"):
            name = row.get("i")
            if not name:
                continue
            out[name] = Ticker(
                instrument=name,
                bid=_f(row.get("b")),
                ask=_f(row.get("k")),
                last=_f(row.get("a")),
                volume_usd_24h=_f(row.get("vv")),
                change_24h=_f(row.get("c")),
            )
        return out

    def candles(self, instrument: str, timeframe: str = "1h", count: int = 120,
                now_ms: int | None = None, end_ts: int | None = None) -> list[Candle]:
        """Closed candles only, oldest first. The still-forming candle is dropped."""
        rows = self._public("get-candlestick", instrument_name=instrument,
                            timeframe=timeframe, count=min(count, 300), end_ts=end_ts)
        candles = sorted(
            (Candle(int(r["t"]), _f(r["o"]), _f(r["h"]), _f(r["l"]), _f(r["c"]), _f(r["v"]))
             for r in rows),
            key=lambda c: c.ts,
        )
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        step = TIMEFRAME_MS.get(timeframe, 3_600_000)
        return [c for c in candles if c.ts + step <= now_ms]

    def instruments(self) -> dict[str, dict]:
        return {r["symbol"]: r for r in self._public("get-instruments") if r.get("symbol")}

    def history(self, instrument: str, timeframe: str, hours: int) -> list[Candle]:
        """Page backwards through the candle endpoint to collect a longer history."""
        step = TIMEFRAME_MS[timeframe]
        wanted = hours * 3_600_000 // step
        end = int(time.time() * 1000)
        seen: dict[int, Candle] = {}
        while len(seen) < wanted:
            batch = self.candles(instrument, timeframe, 300, end_ts=end)
            new = [c for c in batch if c.ts not in seen]
            if not new:
                break
            for c in new:
                seen[c.ts] = c
            end = min(c.ts for c in new) - 1
        return sorted(seen.values(), key=lambda c: c.ts)[-wanted:]


class ReplayMarket:
    """Serves historical candles as if time were `now_ms`. Used for backtests and tests."""

    def __init__(self, series: dict[str, list[Candle]], timeframe: str = "1h",
                 spread_pct: float = 0.003):
        self.series = {k: sorted(v, key=lambda c: c.ts) for k, v in series.items()}
        self.timeframe = timeframe
        self.step = TIMEFRAME_MS[timeframe]
        self.spread_pct = spread_pct
        self.now_ms = 0

    def timeline(self) -> list[int]:
        return sorted({c.ts + self.step for s in self.series.values() for c in s})

    def _closed(self, instrument: str) -> list[Candle]:
        return [c for c in self.series.get(instrument, []) if c.ts + self.step <= self.now_ms]

    def tickers(self) -> dict[str, Ticker]:
        out = {}
        per_day = max(1, 86_400_000 // self.step)
        for name in self.series:
            closed = self._closed(name)
            if not closed:
                continue
            last = closed[-1].close
            day = closed[-per_day:]
            half = self.spread_pct / 2
            out[name] = Ticker(
                instrument=name,
                bid=last * (1 - half),
                ask=last * (1 + half),
                last=last,
                volume_usd_24h=sum(c.volume_usd for c in day),
                change_24h=(last / day[0].open - 1) if day[0].open else 0.0,
            )
        return out

    def candles(self, instrument: str, timeframe: str = "1h", count: int = 120, **_) -> list[Candle]:
        return self._closed(instrument)[-count:]
