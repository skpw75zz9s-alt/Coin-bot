"""Reporter: writes the status page and only pings you when something matters."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

from .treasurer import coverage


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def render_report(state: dict, snapshot: dict, now_iso: str, cfg) -> str:
    cov = coverage(state, now_iso, cfg.treasury)
    start = state.get("starting_equity") or 0.0
    equity = snapshot["equity"]
    lines = [
        "# Memecoin desk status",
        "",
        f"_Updated {now_iso} UTC · mode **{state['mode']}**_",
        "",
    ]
    if state.get("halted"):
        lines += [f"> **HALTED** since {state['halted']['at']}: {state['halted']['reason']}.",
                  "> Run the workflow with command `resume` once you've reviewed it.", ""]
    lines += [
        "## Subscription",
        "",
        f"- This month: **${cov['swept_this_month']:.2f} / ${cov['goal']:.2f}**"
        + (" ✅ covered" if cov["covered_this_month"] else ""),
        f"- Reserve (ring-fenced, never traded): **${cov['reserve']:.2f}**"
        f" ≈ {cov['months_prepaid']} month(s)",
        "",
        "## Trading pot",
        "",
        f"- Equity: ${equity:,.2f} (started ${start:,.2f}, "
        f"{(equity + state['reserve_usd']) / start - 1 if start else 0:+.1%} incl. reserve)",
        f"- Cash: ${snapshot['spendable']:,.2f} · in positions: ${snapshot['positions_value']:,.2f}",
        f"- Realized P&L: ${state['realized_pnl_total']:+,.2f} · fees paid: ${state['fees_total']:,.2f}",
        "",
        "## Open positions",
        "",
    ]
    if state["positions"]:
        lines += ["| Coin | Entry | Now | P&L | Since |", "|---|---|---|---|---|"]
        for name, pos in sorted(state["positions"].items()):
            bid = snapshot["marks"].get(name, pos["entry_price"])
            lines.append(f"| {name} | {pos['entry_price']:.8g} | {bid:.8g} | "
                         f"{bid / pos['entry_price'] - 1:+.1%} | {_fmt_ts(pos['entry_ts'])} |")
    else:
        lines.append("None.")
    lines += ["", "## Recent trades", ""]
    recent = state["trades"][-15:]
    if recent:
        lines += ["| Coin | Exit | Reason | P&L |", "|---|---|---|---|"]
        for t in reversed(recent):
            lines.append(f"| {t['instrument']} | {_fmt_ts(t['exit_ts'])} | {t['reason']} | "
                         f"${t['pnl']:+.2f} ({t['pnl_pct']:+.1%}) |")
    else:
        lines.append("None yet.")
    closed = state["trades"]
    if closed:
        wins = sum(1 for t in closed if t["pnl"] > 0)
        lines += ["", f"Win rate {wins}/{len(closed)} ({wins / len(closed):.0%})."]
    return "\n".join(lines) + "\n"


class Notifier:
    """Posts to a Discord or Slack-compatible webhook. Silent if no URL is configured."""

    def __init__(self, url: str | None, post=None):
        self.url = url
        self._post = post or self._http_post
        self.sent: list[str] = []

    def _http_post(self, url: str, text: str) -> None:
        body = json.dumps({"content": text[:1900], "text": text}).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "coinbot/1.0"})
        urllib.request.urlopen(req, timeout=10).close()

    def send(self, text: str) -> None:
        self.sent.append(text)
        if not self.url:
            return
        try:
            self._post(self.url, text)
        except Exception as err:  # a broken webhook must never break trading
            print(f"notify failed: {err}")
