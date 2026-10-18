import random
import tempfile
import unittest
from pathlib import Path

from coinbot import config as config_mod
from coinbot import state as state_mod
from coinbot.__main__ import backtest, main, walk_forward
from coinbot.broker import CryptoComBroker, PaperBroker, _floor_to_tick, sign_request
from coinbot.desk import Desk
from coinbot.indicators import ema, rsi
from coinbot.market import Candle, CryptoComMarket, ReplayMarket, Ticker
from coinbot.team.analyst import analyse
from coinbot.team.scout import scout
from coinbot.team.risk import check_drawdown, position_size
from coinbot.team.trader import exit_reason
from coinbot.team.treasurer import coverage, record_realized

HOUR = 3_600_000
T0 = 1_760_000_000_000 // HOUR * HOUR


def cfg():
    return config_mod.load(env={})


def flat_then_breakout(n_flat=60, base=1.0, jump=1.06, vol=1_000_000.0, after=None):
    """Sideways candles, then one high-volume breakout candle, then optional path."""
    rng = random.Random(1)
    candles, price = [], base
    for i in range(n_flat):
        # Slight uptrend so fast EMA > slow EMA.
        price = base * (1 + 0.0004 * i) * (1 + rng.uniform(-0.002, 0.002))
        candles.append(Candle(T0 + i * HOUR, price, price * 1.003, price * 0.997, price, vol))
    b = price * jump
    candles.append(Candle(T0 + n_flat * HOUR, price, b, price, b, vol * 4))
    for j, p in enumerate(after or []):
        candles.append(Candle(T0 + (n_flat + 1 + j) * HOUR, p, p, p, p, vol))
    return candles


class IndicatorTests(unittest.TestCase):
    def test_ema_constant(self):
        self.assertAlmostEqual(ema([5.0] * 10, 3)[-1], 5.0)

    def test_rsi_bounds(self):
        self.assertEqual(rsi(list(range(1, 40)), 14), 100.0)
        self.assertLess(rsi(list(range(40, 1, -1)), 14), 1.0)


class TreasurerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg().treasury  # $20 goal, 50% then 20%
        self.state = state_mod.new_state("paper", 500, "x")
        self.state["starting_equity"] = 500.0

    def trade(self, pnl, day="2026-10-01"):
        """Close a trade the way the desk does: cash moves first, then the treasurer books it."""
        self.state["cash"] += pnl
        return record_realized(self.state, pnl, day, self.cfg,
                               self.state["cash"] - self.state["reserve_usd"])

    def test_sweeps_half_until_goal_then_less(self):
        self.assertAlmostEqual(self.trade(10), 5.0)
        # 30 more profit: 30 needed at 50% to finish the $15 left, then nothing extra.
        self.assertAlmostEqual(self.trade(30), 15.0)
        self.assertTrue(coverage(self.state, "2026-10-01", self.cfg)["covered_this_month"])
        self.assertAlmostEqual(self.trade(10), 2.0)

    def test_losses_must_be_recovered_before_sweeping(self):
        self.trade(-10)
        self.assertEqual(self.trade(6), 0.0)            # pot at 496, still below start
        self.assertAlmostEqual(self.trade(8), 2.0)      # pot at 504: only the $4 above start counts
        self.assertAlmostEqual(self.state["reserve_usd"], 2.0)

    def test_never_sweeps_starting_capital_after_a_drawdown(self):
        self.assertAlmostEqual(self.trade(20), 10.0)    # pot 510 after sweep
        self.trade(-40)                                 # pot 470: below start
        self.assertEqual(self.trade(25), 0.0)           # 495: a win, but not real profit yet
        self.assertAlmostEqual(self.trade(15), 5.0)     # 510: sweep half of the $10 above start
        self.assertLessEqual(self.state["reserve_usd"],
                             self.state["realized_pnl_total"] + 1e-9)

    def test_goal_resets_each_month(self):
        self.trade(100)
        self.assertFalse(coverage(self.state, "2026-11-01", self.cfg)["covered_this_month"])


class TraderRiskTests(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.pos = {"entry_price": 1.0, "peak_price": 1.0, "entry_ts": 0}

    def test_exits(self):
        x = self.c.exits
        self.assertEqual(exit_reason(self.pos, 0.91, 1, x), "stop-loss")
        self.assertEqual(exit_reason(self.pos, 1.26, 1, x), "take-profit")
        self.assertIsNone(exit_reason(self.pos, 1.05, 1, x))
        trailed = {**self.pos, "peak_price": 1.15}
        self.assertEqual(exit_reason(trailed, 1.07, 1, x), "trailing-stop")
        self.assertEqual(exit_reason(self.pos, 1.0, 72 * HOUR, x), "time-stop")

    def test_sizing_is_capped(self):
        t = Ticker("X_USD", 1, 1.001, 1, 1_000_000, 0)
        # 1% risk / 8% stop = 12.5% of 500 = 62.5
        self.assertEqual(position_size(t, 500, 500, 0.08, self.c.risk), 62.5)
        self.assertEqual(position_size(t, 500, 20, 0.08, self.c.risk), 20.0)  # cash-capped
        self.assertEqual(position_size(t, 500, 5, 0.08, self.c.risk), 0.0)    # too small
        thin = Ticker("X_USD", 1, 1.001, 1, 1_000, 0)
        self.assertEqual(position_size(thin, 500, 500, 0.08, self.c.risk), 0.0)  # illiquid

    def test_drawdown(self):
        self.assertIsNone(check_drawdown(80, 100, self.c.risk))
        self.assertIn("drawdown", check_drawdown(74, 100, self.c.risk))


class ScoutTests(unittest.TestCase):
    def test_filters_and_matches_either_spelling(self):
        c = cfg().scout
        tickers = {
            "DOGEUSD": Ticker("DOGEUSD", 0.0793, 0.0794, 0.0793, 850_000, 0),
            "PEPE_USD": Ticker("PEPE_USD", 1.0, 1.001, 1, 55_000, 0),
            "MOG_USD": Ticker("MOG_USD", 1.0, 1.001, 1, 1_000, 0),       # too thin
            "SPX_USD": Ticker("SPX_USD", 1.0, 1.02, 1, 90_000, 0),        # spread too wide
        }
        picks = scout(tickers, ["DOGE_USD", "PEPE_USD", "MOG_USD", "SPX_USD", "NOPE_USD"], c)
        self.assertEqual([t.instrument for t in picks], ["DOGEUSD", "PEPE_USD"])


class AnalystTests(unittest.TestCase):
    def test_detects_breakout(self):
        c = cfg()
        candles = flat_then_breakout()
        t = Ticker("X_USD", 1, 1, 1, 1e7, 0.05)
        self.assertIsNotNone(analyse(t, candles, c.analyst))

    def test_rejects_breakout_without_volume(self):
        c = cfg()
        candles = flat_then_breakout()
        last = candles[-1]
        candles[-1] = Candle(last.ts, last.open, last.high, last.low, last.close, 1_000_000.0)
        self.assertIsNone(analyse(Ticker("X_USD", 1, 1, 1, 1e7, 0.05), candles, c.analyst))

    def test_rejects_huge_single_candle(self):
        c = cfg()
        candles = flat_then_breakout(jump=1.3)
        self.assertIsNone(analyse(Ticker("X_USD", 1, 1, 1, 1e7, 0.05), candles, c.analyst))

    def test_rejects_vertical_pump(self):
        c = cfg()
        self.assertIsNone(analyse(Ticker("X_USD", 1, 1, 1, 1e7, 0.9), flat_then_breakout(), c.analyst))


class DeskTests(unittest.TestCase):
    def make(self, series):
        c = cfg()
        c.universe = list(series)
        market = ReplayMarket(series)
        state = state_mod.new_state("paper", 500, "x")
        desk = Desk(c, state, market, PaperBroker(c.paper.fee_pct, c.paper.slippage_pct))
        return c, market, state, desk

    def step(self, market, desk, ts):
        market.now_ms = ts
        return desk.run(ts)

    def test_buy_breakout_take_profit_and_sweep(self):
        candles = flat_then_breakout(after=[1.15, 1.25, 1.40])
        c, market, state, desk = self.make({"MEME_USD": candles})
        breakout_close = T0 + 61 * HOUR
        self.step(market, desk, breakout_close)
        self.assertIn("MEME_USD", state["positions"])
        for i in range(1, 4):
            self.step(market, desk, breakout_close + i * HOUR)
        self.assertNotIn("MEME_USD", state["positions"])
        self.assertEqual(state["trades"][-1]["reason"], "take-profit")
        self.assertGreater(state["trades"][-1]["pnl"], 0)
        self.assertGreater(state["reserve_usd"], 0)
        # Reserve is excluded from what the desk may spend.
        self.assertAlmostEqual(desk.spendable(), state["cash"] - state["reserve_usd"])

    def test_kill_switch_flattens_and_stays_halted(self):
        candles = flat_then_breakout(after=[0.97])
        c, market, state, desk = self.make({"MEME_USD": candles})
        c.risk.max_drawdown_pct = 0.01  # tiny limit so the next dip trips it
        c.exits.stop_loss_pct = 0.5
        c.risk.risk_per_trade_pct = 0.1  # keep the position big enough to matter
        self.step(market, desk, T0 + 61 * HOUR)
        self.assertIn("MEME_USD", state["positions"])
        self.step(market, desk, T0 + 62 * HOUR)
        self.assertIsNotNone(state["halted"])
        self.assertEqual(state["positions"], {})
        self.assertTrue(any("HALTED" in m for m in desk.notify.sent))

    def test_missing_coin_is_ignored(self):
        c, market, state, desk = self.make({"MEME_USD": flat_then_breakout()})
        c.universe = ["NOPE_USD"]
        self.step(market, desk, T0 + 61 * HOUR)
        self.assertEqual(state["positions"], {})


class ExchangeTests(unittest.TestCase):
    def test_parses_raw_tickers_and_drops_forming_candle(self):
        def fake(url):
            if "get-tickers" in url:
                return {"code": 0, "result": {"data": [
                    {"i": "DOGE_USD", "b": "0.0793", "k": "0.0794", "a": "0.0793",
                     "vv": "850773.9", "c": "-0.0065"}]}}
            return {"code": 0, "result": {"data": [
                {"t": T0 + HOUR, "o": "1", "h": "2", "l": "1", "c": "2", "v": "10"},
                {"t": T0, "o": "1", "h": "1", "l": "1", "c": "1", "v": "10"}]}}
        m = CryptoComMarket(get_json=fake)
        t = m.tickers()["DOGE_USD"]
        self.assertAlmostEqual(t.volume_usd_24h, 850773.9)
        candles = m.candles("DOGE_USD", "1h", 10, now_ms=T0 + HOUR + 10)
        self.assertEqual([c.ts for c in candles], [T0])

    def test_signature_is_order_independent_and_keyed(self):
        a = sign_request("private/create-order", 1, "k", {"side": "BUY", "quantity": "1"}, 2, "s")
        b = sign_request("private/create-order", 1, "k", {"quantity": "1", "side": "BUY"}, 2, "s")
        self.assertEqual(a, b)
        self.assertNotEqual(a, sign_request("private/create-order", 1, "k", {"side": "BUY"}, 2, "s"))

    def test_live_buy_with_fee_in_coin(self):
        calls = []

        def transport(url, body):
            calls.append(body)
            if body["method"] == "private/create-order":
                return {"code": 0, "result": {"order_id": "42"}}
            return {"code": 0, "result": {"status": "FILLED", "cumulative_quantity": "1000",
                                          "cumulative_value": "50", "avg_price": "0.05",
                                          "cumulative_fee": "2", "fee_instrument_name": "DOGE"}}
        broker = CryptoComBroker("k", "s", transport=transport, sleep=lambda _: None)
        fill = broker.buy(Ticker("DOGE_USD", 0.05, 0.05, 0.05, 1e6, 0), 50)
        self.assertEqual(fill.qty, 998)
        self.assertEqual(fill.usd, 50)
        order = calls[0]["params"]
        self.assertEqual((order["side"], order["type"], order["notional"]), ("BUY", "MARKET", "50.00"))
        self.assertIn("sig", calls[0])

    def test_live_sell_caps_to_balance_and_tick(self):
        calls = []

        def transport(url, body):
            calls.append(body)
            m = body["method"]
            if m == "private/user-balance":
                return {"code": 0, "result": {"data": [{"position_balances": [
                    {"instrument_name": "DOGE", "quantity": "997.6", "reserved_qty": "0"}]}]}}
            if m == "private/create-order":
                return {"code": 0, "result": {"order_id": "7"}}
            return {"code": 0, "result": {"status": "FILLED", "cumulative_quantity": "997",
                                          "cumulative_value": "59.82", "avg_price": "0.06",
                                          "cumulative_fee": "0.15", "fee_instrument_name": "USD"}}
        broker = CryptoComBroker("k", "s", transport=transport, sleep=lambda _: None,
                                 instruments={"DOGE_USD": {"qty_tick_size": "1"}})
        fill = broker.sell(Ticker("DOGE_USD", 0.06, 0.06, 0.06, 1e6, 0), 998)
        order = [c for c in calls if c["method"] == "private/create-order"][0]["params"]
        self.assertEqual(order["quantity"], "997")
        self.assertAlmostEqual(fill.usd, 59.67)

    def test_floor_to_tick(self):
        self.assertEqual(_floor_to_tick(123.456, "0.01"), "123.45")
        self.assertEqual(_floor_to_tick(999.9, "1"), "999")


class SafetyTests(unittest.TestCase):
    def test_live_requires_keys_and_confirmation(self):
        c = cfg()
        c.mode = "live"
        ok, _ = config_mod.live_trading_allowed(c, {"CRYPTOCOM_API_KEY": "a", "CRYPTOCOM_API_SECRET": "b"})
        self.assertFalse(ok)
        ok, _ = config_mod.live_trading_allowed(c, {
            "CRYPTOCOM_API_KEY": "a", "CRYPTOCOM_API_SECRET": "b",
            "COINBOT_LIVE_CONFIRM": config_mod.LIVE_CONFIRM_PHRASE})
        self.assertTrue(ok)

    def test_run_refuses_live_without_confirmation(self):
        import os
        old = dict(os.environ)
        try:
            os.environ["COINBOT_MODE"] = "live"
            os.environ.pop("COINBOT_LIVE_CONFIRM", None)
            with tempfile.TemporaryDirectory() as d:
                self.assertEqual(main(["--state-dir", d, "run"]), 2)
                self.assertFalse(any(Path(d).iterdir()))
        finally:
            os.environ.clear()
            os.environ.update(old)


class BacktestTests(unittest.TestCase):
    def test_random_walk_runs_and_respects_limits(self):
        rng = random.Random(7)
        series = {}
        for name in ("A_USD", "B_USD", "C_USD"):
            p, candles = 1.0, []
            for i in range(24 * 20):
                o = p
                p *= 1 + rng.gauss(0.0002, 0.02)
                candles.append(Candle(T0 + i * HOUR, o, max(o, p) * 1.005, min(o, p) * 0.995, p,
                                      rng.uniform(5e5, 3e6)))
            series[name] = candles
        c = cfg()
        c.universe = list(series)
        result = backtest(c, series)
        self.assertGreater(result["days"], 19)
        self.assertLessEqual(len(result["open_positions"]), c.risk.max_open_positions)
        self.assertGreaterEqual(result["reserve"], 0)
        self.assertIsNotNone(result["buy_and_hold_pct"])
        wf = walk_forward(c, series, 5)
        self.assertEqual(len(wf["windows"]), 2)  # 20 days minus ~5 days of warm-up = two full 5-day windows
        self.assertTrue(all(w["from"] < w["to"] for w in wf["windows"]))


if __name__ == "__main__":
    unittest.main()
