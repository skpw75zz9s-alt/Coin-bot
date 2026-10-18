"""The desk: runs one cycle of the whole team. Called once an hour by the scheduler."""

from __future__ import annotations

from datetime import datetime, timezone

from .team.analyst import analyse
from .team.reporter import Notifier
from .team.risk import check_drawdown, daily_limit_hit, position_size
from .team.scout import scout
from .team.trader import exit_reason
from .team.treasurer import coverage, record_realized


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class Desk:
    def __init__(self, cfg, state: dict, market, broker, notifier: Notifier | None = None):
        self.cfg = cfg
        self.state = state
        self.market = market
        self.broker = broker
        self.notify = notifier or Notifier(None)
        self.tickers: dict = {}
        self.now_ms = 0

    # -- accounting ------------------------------------------------------------------------
    def marks(self) -> dict[str, float]:
        return {n: self.tickers[n].bid for n in self.state["positions"] if n in self.tickers}

    def positions_value(self) -> float:
        marks = self.marks()
        return sum(p["qty"] * marks.get(n, p["entry_price"]) for n, p in self.state["positions"].items())

    def equity(self) -> float:
        """Trading equity: everything except the ring-fenced reserve."""
        return self.state["cash"] - self.state["reserve_usd"] + self.positions_value()

    def spendable(self) -> float:
        ledger = self.state["cash"] - self.state["reserve_usd"]
        exchange = self.broker.cash()
        return max(0.0, ledger if exchange is None else min(ledger, exchange))

    # -- actions ---------------------------------------------------------------------------
    def _close(self, name: str, reason: str) -> bool:
        pos = self.state["positions"][name]
        ticker = self.tickers.get(name)
        if ticker is None:
            return False
        try:
            fill = self.broker.sell(ticker, pos["qty"])
        except Exception as err:
            self.notify.send(f"⚠️ Couldn't sell {name} ({reason}): {err}")
            return False
        now_iso = iso(self.now_ms)
        pnl = fill.usd - pos["cost_usd"]
        self.state["cash"] += fill.usd
        self.state["fees_total"] += fill.fee_usd
        del self.state["positions"][name]
        self.state["trades"].append({
            "instrument": name, "reason": reason,
            "entry_ts": pos["entry_ts"], "exit_ts": self.now_ms,
            "entry_price": pos["entry_price"], "exit_price": fill.price,
            "cost_usd": pos["cost_usd"], "proceeds_usd": fill.usd,
            "pnl": pnl, "pnl_pct": pnl / pos["cost_usd"] if pos["cost_usd"] else 0.0,
        })
        self.state["cooldowns"][name] = self.now_ms + self.cfg.risk.cooldown_hours * 3_600_000
        before = coverage(self.state, now_iso, self.cfg.treasury)["covered_this_month"]
        swept = record_realized(self.state, pnl, now_iso, self.cfg.treasury)
        cov = coverage(self.state, now_iso, self.cfg.treasury)
        msg = f"{'🟢' if pnl > 0 else '🔴'} Sold {name} ({reason}) P&L ${pnl:+.2f}"
        if swept:
            msg += f" · ${swept:.2f} to subscription reserve"
        self.notify.send(msg)
        if cov["covered_this_month"] and not before:
            self.notify.send(f"🎉 This month's ${cov['goal']:.2f} subscription is covered. "
                             f"Reserve: ${cov['reserve']:.2f}")
        return True

    def _open(self, name: str, notional: float, reason: str) -> None:
        ticker = self.tickers[name]
        try:
            fill = self.broker.buy(ticker, notional)
        except Exception as err:
            self.notify.send(f"⚠️ Couldn't buy {name}: {err}")
            return
        if fill.qty <= 0:
            return
        self.state["cash"] -= fill.usd
        self.state["fees_total"] += fill.fee_usd
        self.state["positions"][name] = {
            "qty": fill.qty, "entry_price": fill.price, "peak_price": fill.price,
            "cost_usd": fill.usd, "entry_ts": self.now_ms, "reason": reason,
        }
        self.notify.send(f"🛒 Bought ${fill.usd:.2f} of {name} @ {fill.price:.8g} — {reason}")

    def _halt(self, reason: str) -> None:
        self.state["halted"] = {"reason": reason, "at": iso(self.now_ms)}
        self.notify.send(f"🛑 Desk HALTED: {reason}. Selling everything. "
                         f"It will stay flat until you run `resume`.")

    # -- the cycle -------------------------------------------------------------------------
    def run(self, now_ms: int) -> dict:
        cfg, st = self.cfg, self.state
        self.now_ms = now_ms
        now_iso = iso(now_ms)
        self.tickers = self.market.tickers()

        if st["starting_equity"] is None:
            st["starting_equity"] = self.equity()
        if st["peak_equity"] is None:
            st["peak_equity"] = self.equity()
        today = now_iso[:10]
        if not st["day"] or st["day"]["date"] != today:
            if st["day"] and datetime.fromisoformat(today).weekday() == 0:
                self.notify.send(self.weekly_digest(now_iso))
            st["day"] = {"date": today, "start_equity": self.equity()}

        # 1. Manage what we already hold.
        for name, pos in list(st["positions"].items()):
            ticker = self.tickers.get(name)
            if ticker is None:
                continue
            pos["peak_price"] = max(pos["peak_price"], ticker.bid)
            reason = exit_reason(pos, ticker.bid, now_ms, cfg.exits)
            if reason:
                self._close(name, reason)

        # 2. Kill switch.
        equity = self.equity()
        st["peak_equity"] = max(st["peak_equity"], equity)
        if not st["halted"]:
            breach = check_drawdown(equity, st["peak_equity"], cfg.risk)
            if breach:
                self._halt(breach)
        if st["halted"]:
            for name in list(st["positions"]):
                self._close(name, "halt")

        # 3. Look for new trades.
        can_enter = (not st["halted"]
                     and not daily_limit_hit(self.equity(), st["day"]["start_equity"], cfg.risk)
                     and len(st["positions"]) < cfg.risk.max_open_positions)
        if can_enter:
            signals = []
            for ticker in scout(self.tickers, cfg.universe, cfg.scout):
                name = ticker.instrument
                if name in st["positions"] or st["cooldowns"].get(name, 0) > now_ms:
                    continue
                try:
                    candles = self.market.candles(name, cfg.analyst.timeframe, cfg.analyst.candles)
                except Exception as err:
                    print(f"candles {name}: {err}")
                    continue
                sig = analyse(ticker, candles, cfg.analyst)
                if sig:
                    signals.append(sig)
            for sig in sorted(signals, key=lambda s: s.score, reverse=True):
                if len(st["positions"]) >= cfg.risk.max_open_positions:
                    break
                size = position_size(self.tickers[sig.instrument], self.equity(), self.spendable(),
                                     cfg.exits.stop_loss_pct, cfg.risk)
                if size > 0:
                    self._open(sig.instrument, size, sig.reason)

        st["cooldowns"] = {k: v for k, v in st["cooldowns"].items() if v > now_ms}
        st["last_run"] = now_iso
        return self.snapshot()

    def snapshot(self) -> dict:
        return {
            "equity": self.equity(),
            "spendable": self.spendable(),
            "positions_value": self.positions_value(),
            "marks": self.marks(),
        }

    def weekly_digest(self, now_iso: str) -> str:
        cov = coverage(self.state, now_iso, self.cfg.treasury)
        week_ago = self.now_ms - 7 * 86_400_000
        week = [t for t in self.state["trades"] if t["exit_ts"] >= week_ago]
        pnl = sum(t["pnl"] for t in week)
        return (f"📊 Weekly: {len(week)} trades, P&L ${pnl:+.2f}. Equity ${self.equity():,.2f}. "
                f"Subscription ${cov['swept_this_month']:.2f}/${cov['goal']:.2f} this month, "
                f"reserve ${cov['reserve']:.2f}.")
