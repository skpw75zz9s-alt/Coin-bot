"""Order execution: a paper broker for simulation and a Crypto.com Exchange live broker."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.request
import uuid
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .market import API_BASE, Ticker


@dataclass(frozen=True)
class Fill:
    qty: float        # base units now held (buy) or sold (sell)
    price: float      # average fill price
    fee_usd: float
    usd: float        # cash out for a buy (incl. fees), cash in for a sell (net of fees)


class PaperBroker:
    """Fills at the far side of the book plus slippage and fees."""

    live = False

    def __init__(self, fee_pct: float, slippage_pct: float):
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct

    def cash(self) -> float | None:
        return None  # no real account; the desk's own ledger is the only limit

    def buy(self, ticker: Ticker, notional: float) -> Fill:
        price = ticker.ask * (1 + self.slippage_pct)
        fee = notional * self.fee_pct
        return Fill(qty=(notional - fee) / price, price=price, fee_usd=fee, usd=notional)

    def sell(self, ticker: Ticker, qty: float) -> Fill:
        price = ticker.bid * (1 - self.slippage_pct)
        gross = qty * price
        fee = gross * self.fee_pct
        return Fill(qty=qty, price=price, fee_usd=fee, usd=gross - fee)


def _params_to_str(obj, level: int = 0) -> str:
    """Crypto.com's signing serialisation: sorted keys, values concatenated, max depth 3."""
    if level >= 3:
        return str(obj)
    if not isinstance(obj, dict):
        return str(obj)
    out = ""
    for key in sorted(obj):
        out += key
        value = obj[key]
        if value is None:
            out += "null"
        elif isinstance(value, list):
            for item in value:
                out += _params_to_str(item, level + 1)
        else:
            out += str(value)
    return out


def sign_request(method: str, req_id: int, api_key: str, params: dict, nonce: int, secret: str) -> str:
    payload = method + str(req_id) + api_key + _params_to_str(params) + str(nonce)
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _floor_to_tick(qty: float, tick: str | float) -> str:
    tick_d = Decimal(str(tick))
    return format((Decimal(str(qty)) / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d, "f")


class CryptoComBroker:
    """Spot market orders on Crypto.com Exchange.

    Use an API key with trading permission only. Never enable withdrawals for this key.
    """

    live = True

    def __init__(self, api_key: str, api_secret: str, quote: str = "USD", base: str = API_BASE,
                 transport=None, instruments: dict | None = None, sleep=time.sleep):
        self.api_key = api_key
        self.api_secret = api_secret
        self.quote = quote
        self.base = base
        self._transport = transport or self._http_post
        self._instruments = instruments
        self._sleep = sleep

    # -- transport -------------------------------------------------------------------------
    def _http_post(self, url: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "coinbot/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())

    def _private(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        req_id = int(time.time() * 1000)
        nonce = req_id
        body = {
            "id": req_id,
            "method": method,
            "api_key": self.api_key,
            "params": params,
            "nonce": nonce,
            "sig": sign_request(method, req_id, self.api_key, params, nonce, self.api_secret),
        }
        resp = self._transport(f"{self.base}{method}", body)
        if resp.get("code", 0) != 0:
            raise RuntimeError(f"{method} error {resp.get('code')}: {resp.get('message')}")
        return resp.get("result", {})

    # -- account ---------------------------------------------------------------------------
    def balance(self, currency: str) -> float:
        result = self._private("private/user-balance")
        for account in result.get("data", []):
            for pos in account.get("position_balances", []):
                if pos.get("instrument_name") == currency:
                    return max(0.0, _f(pos.get("quantity")) - _f(pos.get("reserved_qty")))
        return 0.0

    def cash(self) -> float:
        return self.balance(self.quote)

    def _qty_tick(self, instrument: str) -> str:
        if self._instruments is None:
            from .market import CryptoComMarket
            self._instruments = CryptoComMarket(self.base).instruments()
        info = self._instruments.get(instrument, {})
        return str(info.get("qty_tick_size") or "1")

    # -- orders ----------------------------------------------------------------------------
    def _wait_for_fill(self, order_id: str) -> dict:
        detail: dict = {}
        for _ in range(10):
            detail = self._private("private/get-order-detail", {"order_id": order_id})
            if detail.get("status") in ("FILLED", "CANCELED", "REJECTED", "EXPIRED"):
                break
            self._sleep(1)
        if _f(detail.get("cumulative_quantity")) <= 0:
            raise RuntimeError(f"order {order_id} not filled: {detail.get('status')} {detail.get('reason', '')}")
        return detail

    def _place(self, params: dict) -> dict:
        params = {**params, "type": "MARKET", "client_oid": uuid.uuid4().hex[:32]}
        result = self._private("private/create-order", params)
        return self._wait_for_fill(str(result["order_id"]))

    def buy(self, ticker: Ticker, notional: float) -> Fill:
        detail = self._place({"instrument_name": ticker.instrument, "side": "BUY",
                              "notional": f"{notional:.2f}"})
        qty = _f(detail.get("cumulative_quantity"))
        value = _f(detail.get("cumulative_value"))
        fee = _f(detail.get("cumulative_fee"))
        fee_ccy = detail.get("fee_instrument_name", self.quote)
        avg = _f(detail.get("avg_price")) or (value / qty if qty else ticker.ask)
        if fee_ccy == self.quote:
            return Fill(qty=qty, price=avg, fee_usd=fee, usd=value + fee)
        # Fee taken in the coin itself: we hold less of it.
        return Fill(qty=qty - fee, price=avg, fee_usd=fee * avg, usd=value)

    def sell(self, ticker: Ticker, qty: float) -> Fill:
        base_ccy = ticker.instrument.removesuffix(self.quote).rstrip("_")
        qty = min(qty, self.balance(base_ccy))
        qty_str = _floor_to_tick(qty, self._qty_tick(ticker.instrument))
        if Decimal(qty_str) <= 0:
            raise RuntimeError(f"nothing to sell for {ticker.instrument} (qty {qty})")
        detail = self._place({"instrument_name": ticker.instrument, "side": "SELL",
                              "quantity": qty_str})
        sold = _f(detail.get("cumulative_quantity"))
        value = _f(detail.get("cumulative_value"))
        fee = _f(detail.get("cumulative_fee"))
        avg = _f(detail.get("avg_price")) or (value / sold if sold else ticker.bid)
        fee_usd = fee if detail.get("fee_instrument_name", self.quote) == self.quote else fee * avg
        return Fill(qty=sold, price=avg, fee_usd=fee_usd, usd=value - fee_usd)
