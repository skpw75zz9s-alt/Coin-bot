"""The on-chain analyst: your checklist, made deterministic.

Same questions as the prompt: who holds it, how liquid is it, is the flow buying or
distributing, and who is left to buy after us. Every check either uses data we can
verify on-chain/DEX, or says "not visible" and makes the verdict more cautious.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .data import TOKEN_2022, Holders, Pair


@dataclass
class Report:
    pair: Pair
    holders: Holders | None = None
    verdict: str = "SKIP"          # FOLLOW / WATCH / SKIP
    flags: list = field(default_factory=list)    # reasons to kill the trade
    signals: list = field(default_factory=list)  # reasons it's working
    exit_impact: float | None = None             # price drop if top 3 sell half
    score: float = 0.0


def fmt_usd(x: float) -> str:
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x / div:.2f}{unit}"
    return f"${x:,.0f}"


def flow_flags(p: Pair, now_ms: int, cfg) -> list[str]:
    """Cheap red flags from DEX data alone; run on every candidate before any RPC calls."""
    flags = []
    if p.liquidity_usd < cfg.min_liquidity_usd:
        flags.append(f"liquidity {fmt_usd(p.liquidity_usd)} below {fmt_usd(cfg.min_liquidity_usd)}")
    if p.liq_to_mcap < cfg.min_liq_to_mcap:
        flags.append(f"liquidity only {p.liq_to_mcap:.1%} of mcap: any real exit moves price hard")
    if not cfg.min_mcap_usd <= p.mcap_usd <= cfg.max_mcap_usd:
        flags.append(f"mcap {fmt_usd(p.mcap_usd)} outside range")
    age = p.age_hours(now_ms)
    if age < cfg.min_age_hours:
        flags.append(f"pool only {age:.1f}h old: launch bundles still in play")
    if age > cfg.max_age_days * 24:
        flags.append(f"pool {age / 24:.0f} days old: not early")
    if p.chg_h24 > cfg.max_change_24h:
        flags.append(f"up {p.chg_h24:.0%} in 24h: late, we'd be the exit liquidity")
    if p.buys_h1 and p.sells_h1 > cfg.distribution_sell_ratio * p.buys_h1 and abs(p.chg_h1) < 0.03:
        flags.append(f"sellers {p.sells_h1} vs buyers {p.buys_h1} in 1h while price holds: distribution")
    if p.buys_h6 and p.buys_h1 and p.vol_h6:
        rate_up = p.buys_h1 > 1.5 * p.buys_h6 / 6
        size_h1 = p.vol_h1 / max(1, p.buys_h1 + p.sells_h1)
        size_h6 = p.vol_h6 / max(1, p.buys_h6 + p.sells_h6)
        if rate_up and size_h1 < 0.6 * size_h6:
            flags.append("more buyers but much smaller tickets: retail arriving late")
    return flags


def holder_flags(p: Pair, h: Holders, cfg) -> tuple[list[str], float]:
    flags = []
    if h.mint_authority:
        flags.append("mint authority still enabled: dev can print supply")
    if h.freeze_authority:
        flags.append("freeze authority enabled: your tokens can be frozen")
    if h.token_program == TOKEN_2022:
        flags.append("Token-2022 mint: possible transfer fees/hooks")
    if h.top10_pct > cfg.max_top10_pct:
        flags.append(f"top 10 wallets hold {h.top10_pct:.0%} of supply")
    if h.max_single_pct > cfg.max_single_holder_pct:
        flags.append(f"one wallet holds {h.max_single_pct:.0%} of supply")
    # Constant-product estimate: top 3 dump half their bags into the pool.
    impact = 1.0
    if p.price_usd > 0 and p.liquidity_usd > 0:
        reserve = (p.liquidity_usd / 2) / p.price_usd
        dump = 0.5 * sum(a for _, a in h.top[:3])
        impact = 1 - (reserve / (reserve + dump)) ** 2
    if impact > cfg.max_exit_impact:
        flags.append(f"if the top 3 sell half, price drops ~{impact:.0%}")
    return flags, impact


def entry_signals(p: Pair, cfg) -> tuple[list[str], float]:
    signals, ok = [], True
    ratio = p.buys_h1 / max(1, p.sells_h1)
    vol_liq = p.vol_h1 / p.liquidity_usd if p.liquidity_usd else 0.0
    checks = [
        (p.chg_h1 >= cfg.min_change_1h, f"+{p.chg_h1:.0%} in 1h"),
        (ratio >= cfg.min_buy_sell_ratio_1h, f"buyers {ratio:.1f}x sellers in 1h"),
        (vol_liq >= cfg.min_volume_1h_to_liq, f"1h volume {vol_liq:.0%} of liquidity"),
        (p.chg_h6 > 0, f"6h trend {p.chg_h6:+.0%}"),
    ]
    for passed, text in checks:
        if passed:
            signals.append(text)
        ok = ok and passed
    score = ratio * (1 + p.chg_h1) * min(vol_liq, 3.0) if ok else 0.0
    return signals, score


def assess(p: Pair, holders: Holders | None, now_ms: int, cfg) -> Report:
    r = Report(pair=p, holders=holders)
    r.flags = flow_flags(p, now_ms, cfg)
    if holders is None:
        r.flags.append("holder data not visible")
    else:
        hf, r.exit_impact = holder_flags(p, holders, cfg)
        r.flags += hf
    r.signals, r.score = entry_signals(p, cfg)
    if r.flags:
        r.verdict = "SKIP"
    elif r.score > 0:
        r.verdict = "FOLLOW"
    else:
        r.verdict = "WATCH"
    return r


def render(r: Report, now_ms: int, cfg) -> str:
    """The output block from the analyst prompt."""
    p, h = r.pair, r.holders
    first = ""
    if r.exit_impact is not None and r.exit_impact > 0.9:
        first = "If the top 3 holders sell half, it goes to zero.\n"
    if h is None:
        base = "not visible"
    else:
        strength = "fragile" if h.top10_pct > 0.3 or h.max_single_pct > 0.1 else (
            "mixed" if h.top10_pct > 0.15 else "strong")
        base = f"{strength}: top 10 hold {h.top10_pct:.0%}, largest wallet {h.max_single_pct:.0%}"
    tp = ", ".join(f"{fmt_usd(p.mcap_usd * m)} sell {s:.0%}" for m, s in cfg.take_profit)
    lines = [
        f"### {p.symbol} `{p.token}`",
        "```",
        first + f"VERDICT: {r.verdict}",
        "SMART MONEY: not visible (needs a paid wallet-tracking source); confidence lowered",
        f"HOLDER BASE: {base}",
        f"LIQUIDITY: {fmt_usd(p.liquidity_usd)} = {p.liq_to_mcap:.1%} of {fmt_usd(p.mcap_usd)} mcap",
        f"CATALYST: not visible; pool {p.age_hours(now_ms):.0f}h old on {p.dex}, "
        f"24h {p.chg_h24:+.0%}, 1h {p.chg_h1:+.0%}",
        f"BIGGEST RISK: {r.flags[0] if r.flags else 'thin data: no smart-money or catalyst view'}",
        f"ENTRY ZONE: {fmt_usd(p.mcap_usd)} - {fmt_usd(p.mcap_usd * 1.1)}",
        f"TAKE PROFIT: {tp}, rest trails {cfg.trail_pct:.0%}",
        f"INVALIDATION: {fmt_usd(p.mcap_usd * (1 - cfg.stop_loss_pct))} mcap, or liquidity "
        f"-{cfg.liquidity_pull_pct:.0%}, or 1h sells > {cfg.dump_sell_buy_ratio:.0f}x buys",
        "```",
        "**Who is my exit liquidity?** "
        + (f"If the top 3 wallets sell half, price falls ~{r.exit_impact:.0%}. " if r.exit_impact is not None else "")
        + (f"Working for it: {'; '.join(r.signals)}. " if r.signals else "")
        + (f"Against it: {'; '.join(r.flags)}." if r.flags else ""),
    ]
    return "\n".join(lines)
