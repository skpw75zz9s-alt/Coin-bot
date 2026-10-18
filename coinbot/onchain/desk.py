"""On-chain desk: scans Solana DEX memecoins, applies the analyst, paper-trades the FOLLOWs."""

from __future__ import annotations

from types import SimpleNamespace

from ..desk import iso
from ..team.reporter import Notifier
from ..team.risk import check_drawdown
from ..team.treasurer import coverage, record_realized
from .analyst import Report, assess, flow_flags, fmt_usd, render
from .data import OnchainData, Pair

MISSES_BEFORE_WRITE_OFF = 3  # pool vanished from DexScreener this many runs in a row = rugged


class PaperDex:
    """Simulates a constant-product pool: bigger orders vs. liquidity get worse prices."""

    def __init__(self, fee_pct: float):
        self.fee_pct = fee_pct

    @staticmethod
    def _impact(usd: float, pair: Pair) -> float:
        quote_side = pair.liquidity_usd / 2
        return min(0.99, usd / quote_side) if quote_side > 0 else 0.99

    def buy(self, pair: Pair, usd: float) -> tuple[float, float, float]:
        """Returns (tokens received, effective price, fee)."""
        fee = usd * self.fee_pct
        price = pair.price_usd * (1 + self._impact(usd, pair))
        return (usd - fee) / price, price, fee

    def sell(self, pair: Pair, qty: float) -> tuple[float, float, float]:
        """Returns (usd received, effective price, fee)."""
        gross = qty * pair.price_usd
        gross *= 1 - self._impact(gross, pair)
        fee = gross * self.fee_pct
        return gross - fee, gross / qty if qty else 0.0, fee


class OnchainDesk:
    def __init__(self, cfg, state: dict, data: OnchainData, notifier: Notifier | None = None):
        self.cfg = cfg
        self.oc = cfg.onchain
        self.state = state
        self.data = data
        self.dex = PaperDex(self.oc.fee_pct)
        self.notify = notifier or Notifier(None)
        self.pairs: dict[str, Pair] = {}
        self.reports: list[Report] = []
        self.now_ms = 0

    # -- accounting ------------------------------------------------------------------------
    def _mark(self, token: str, pos: dict) -> float:
        pair = self.pairs.get(token)
        return pair.price_usd if pair else pos["last_price"]

    def equity(self) -> float:
        held = sum(p["qty"] * self._mark(t, p) for t, p in self.state["positions"].items())
        return self.state["cash"] - self.state["reserve_usd"] + held

    def _book_equity(self) -> float:
        return (self.state["cash"] - self.state["reserve_usd"]
                + sum(p["cost_usd"] for p in self.state["positions"].values()))

    # -- actions ---------------------------------------------------------------------------
    def _sell(self, token: str, fraction: float, reason: str) -> None:
        pos = self.state["positions"][token]
        pair = self.pairs.get(token)
        qty = pos["qty"] * fraction
        if pair is None:  # pool is gone: the position is worth nothing
            usd, price, fee = 0.0, 0.0, 0.0
        else:
            usd, price, fee = self.dex.sell(pair, qty)
        cost = pos["cost_usd"] * fraction
        pnl = usd - cost
        st = self.state
        st["cash"] += usd
        st["fees_total"] += fee
        pos["qty"] -= qty
        pos["cost_usd"] -= cost
        closed = fraction >= 0.999 or pos["qty"] * (pair.price_usd if pair else 0) < 0.5
        if closed:
            del st["positions"][token]
            st["cooldowns"][token] = self.now_ms + self.oc.cooldown_hours * 3_600_000
        st["trades"].append({
            "instrument": pos["symbol"], "token": token, "reason": reason,
            "entry_ts": pos["entry_ts"], "exit_ts": self.now_ms,
            "entry_price": pos["entry_price"], "exit_price": price,
            "cost_usd": cost, "proceeds_usd": usd, "pnl": pnl,
            "pnl_pct": pnl / cost if cost else 0.0, "fraction": fraction,
        })
        now_iso = iso(self.now_ms)
        before = coverage(st, now_iso, self.cfg.treasury)["covered_this_month"]
        swept = record_realized(st, pnl, now_iso, self.cfg.treasury, self._book_equity())
        msg = (f"⛓️ {'🟢' if pnl > 0 else '🔴'} Sold {fraction:.0%} of {pos['symbol']} ({reason}) "
               f"P&L ${pnl:+.2f}")
        if swept:
            msg += f" · ${swept:.2f} to subscription reserve"
        self.notify.send(msg)
        if coverage(st, now_iso, self.cfg.treasury)["covered_this_month"] and not before:
            self.notify.send("🎉 This month's subscription is covered by on-chain profits.")

    def _buy(self, report: Report, usd: float) -> None:
        p = report.pair
        qty, price, fee = self.dex.buy(p, usd)
        self.state["cash"] -= usd
        self.state["fees_total"] += fee
        self.state["positions"][p.token] = {
            "symbol": p.symbol, "qty": qty, "entry_price": price, "entry_mcap": p.mcap_usd,
            "entry_liquidity": p.liquidity_usd, "peak_price": p.price_usd, "last_price": p.price_usd,
            "cost_usd": usd, "entry_ts": self.now_ms, "tp_done": 0, "misses": 0, "url": p.url,
        }
        self.notify.send(f"⛓️ 🛒 Bought ${usd:.2f} of {p.symbol} at {fmt_usd(p.mcap_usd)} mcap — "
                         f"{'; '.join(report.signals)}")

    # -- exits -----------------------------------------------------------------------------
    def _manage(self, token: str, pos: dict) -> None:
        oc, pair = self.oc, self.pairs.get(token)
        if pair is None or pair.price_usd <= 0:
            pos["misses"] = pos.get("misses", 0) + 1
            if pos["misses"] >= MISSES_BEFORE_WRITE_OFF:
                self._sell(token, 1.0, "pool gone (written off)")
            return
        pos["misses"] = 0
        pos["last_price"] = pair.price_usd
        pos["peak_price"] = max(pos["peak_price"], pair.price_usd)
        entry, price = pos["entry_price"], pair.price_usd
        if price <= entry * (1 - oc.stop_loss_pct):
            return self._sell(token, 1.0, "invalidation: stop-loss")
        if pair.liquidity_usd < pos["entry_liquidity"] * (1 - oc.liquidity_pull_pct):
            return self._sell(token, 1.0, "leave now: liquidity pulled")
        if pair.sells_h1 >= 20 and pair.sells_h1 > oc.dump_sell_buy_ratio * max(1, pair.buys_h1):
            return self._sell(token, 1.0, "leave now: holders dumping")
        if pos["tp_done"] and price <= pos["peak_price"] * (1 - oc.trail_pct):
            return self._sell(token, 1.0, "trailing stop")
        if self.now_ms - pos["entry_ts"] >= oc.max_hold_hours * 3_600_000:
            return self._sell(token, 1.0, "time stop")
        # Take profits: sell the configured share of the ORIGINAL position at each level.
        while pos["tp_done"] < len(oc.take_profit):
            multiple, share = oc.take_profit[pos["tp_done"]]
            if price < entry * multiple:
                break
            remaining = 1 - sum(s for _, s in oc.take_profit[: pos["tp_done"]])
            pos["tp_done"] += 1
            self._sell(token, min(1.0, share / remaining), f"take-profit {multiple}x")
            if token not in self.state["positions"]:
                break

    # -- the cycle -------------------------------------------------------------------------
    def run(self, now_ms: int) -> None:
        oc, st = self.oc, self.state
        self.now_ms = now_ms
        held = list(st["positions"])
        candidates = [t for t in self.data.candidates(oc.chain)
                      if t not in held and st["cooldowns"].get(t, 0) <= now_ms]
        self.pairs = self.data.pairs(oc.chain, held + candidates, oc.dex_allowlist)

        if st["starting_equity"] is None:
            st["starting_equity"] = self.equity()
        if st["peak_equity"] is None:
            st["peak_equity"] = self.equity()

        for token in held:
            if token in st["positions"]:
                self._manage(token, st["positions"][token])

        equity = self.equity()
        st["peak_equity"] = max(st["peak_equity"], equity)
        if not st["halted"]:
            breach = check_drawdown(equity, st["peak_equity"],
                                    SimpleNamespace(max_drawdown_pct=oc.max_drawdown_pct))
            if breach:
                st["halted"] = {"reason": breach, "at": iso(now_ms)}
                self.notify.send(f"⛓️ 🛑 On-chain desk HALTED: {breach}. Selling everything.")
        if st["halted"]:
            for token in list(st["positions"]):
                self._sell(token, 1.0, "halt")

        # Analyst: cheap DEX checks for everyone, holder RPC checks for the best few.
        shortlist = [p for t, p in self.pairs.items() if t in candidates]
        reports = [assess(p, None, now_ms, oc) for p in shortlist if not flow_flags(p, now_ms, oc)]
        reports.sort(key=lambda r: r.score, reverse=True)
        checked, rpc_failures = [], 0
        for r in reports[: oc.max_holder_checks]:
            holders = None
            if rpc_failures < 3:  # every RPC refusing: don't burn the run retrying
                try:
                    holders = self.data.holders(r.pair.token)
                    rpc_failures = 0
                except Exception as err:
                    rpc_failures += 1
                    print(f"holders {r.pair.symbol}: {err}")
            checked.append(assess(r.pair, holders, now_ms, oc))
        self.reports = sorted(checked, key=lambda r: ({"FOLLOW": 0, "WATCH": 1}.get(r.verdict, 2), -r.score))

        if not st["halted"]:
            for r in self.reports:
                if r.verdict != "FOLLOW" or len(st["positions"]) >= oc.max_open_positions:
                    continue
                size = min(self.equity() * oc.position_pct,
                           r.pair.liquidity_usd * oc.max_liquidity_share,
                           st["cash"] - st["reserve_usd"])
                if size >= oc.min_order_usd:
                    self._buy(r, round(size, 2))

        st["cooldowns"] = {k: v for k, v in st["cooldowns"].items() if v > now_ms}
        st["last_run"] = iso(now_ms)
        st["last_scan"] = {"candidates": len(candidates), "priced": len(shortlist),
                           "passed_flow": len(reports), "holder_checked": len(checked)}

    # -- reporting -------------------------------------------------------------------------
    def report_md(self) -> str:
        st, now_iso = self.state, iso(self.now_ms)
        cov = coverage(st, now_iso, self.cfg.treasury)
        start = st.get("starting_equity") or 0.0
        eq = self.equity()
        out = [
            "# On-chain desk (Solana, paper)", "",
            f"_Updated {now_iso} UTC_", "",
        ]
        if st.get("halted"):
            out += [f"> **HALTED** since {st['halted']['at']}: {st['halted']['reason']}", ""]
        scan = st.get("last_scan", {})
        out += [
            f"- Equity ${eq:,.2f} (started ${start:,.2f}, "
            f"{(eq + st['reserve_usd']) / start - 1 if start else 0:+.1%} incl. reserve)",
            f"- Subscription this month ${cov['swept_this_month']:.2f} / ${cov['goal']:.2f} · "
            f"reserve ${cov['reserve']:.2f}",
            f"- Realized P&L ${st['realized_pnl_total']:+,.2f} · fees ${st['fees_total']:,.2f}",
            f"- Last scan: {scan.get('candidates', 0)} trending tokens, {scan.get('priced', 0)} priced, "
            f"{scan.get('passed_flow', 0)} passed flow checks, {scan.get('holder_checked', 0)} holder-checked",
            "", "## Open positions", "",
        ]
        if st["positions"]:
            out += ["| Token | Entry mcap | Now | P&L | TPs hit |", "|---|---|---|---|---|"]
            for token, pos in st["positions"].items():
                price = self._mark(token, pos)
                out.append(f"| [{pos['symbol']}]({pos.get('url', '')}) | {fmt_usd(pos['entry_mcap'])} | "
                           f"{price / pos['entry_price'] - 1:+.0%} | ${pos['qty'] * price - pos['cost_usd']:+.2f} | "
                           f"{pos['tp_done']} |")
        else:
            out.append("None.")
        out += ["", "## Recent exits", ""]
        if st["trades"]:
            out += ["| Token | Reason | P&L |", "|---|---|---|"]
            for t in reversed(st["trades"][-15:]):
                out.append(f"| {t['instrument']} | {t['reason']} | ${t['pnl']:+.2f} ({t['pnl_pct']:+.0%}) |")
        else:
            out.append("None yet.")
        out += ["", "## Latest analyst calls", ""]
        out += [render(r, self.now_ms, self.oc) + "\n" for r in self.reports[:8]] or ["Nothing passed the flow checks."]
        return "\n".join(out) + "\n"
