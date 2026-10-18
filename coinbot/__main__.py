"""Command line: python -m coinbot {run,status,resume,backtest}."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import state as state_mod
from .broker import CryptoComBroker, PaperBroker
from .desk import Desk, iso
from .market import Candle, CryptoComMarket, ReplayMarket
from .team.reporter import Notifier, render_report
from .team.treasurer import coverage

ERRORS_BEFORE_ALERT = 3


def _state_path(args, cfg) -> Path:
    # One file per mode, so paper positions can never leak into a live account.
    return Path(args.state_dir) / f"state-{cfg.mode}.json"


def _build_broker(cfg):
    if cfg.mode == "paper":
        return PaperBroker(cfg.paper.fee_pct, cfg.paper.slippage_pct)
    return CryptoComBroker(os.environ["CRYPTOCOM_API_KEY"], os.environ["CRYPTOCOM_API_SECRET"])


def cmd_run(args) -> int:
    cfg = config_mod.load(args.config)
    if cfg.mode == "live":
        ok, why = config_mod.live_trading_allowed(cfg)
        if not ok:
            print(f"Refusing to trade live: {why}", file=sys.stderr)
            return 2
    path = _state_path(args, cfg)
    now_ms = int(time.time() * 1000)
    broker = _build_broker(cfg)
    notifier = Notifier(os.environ.get("COINBOT_WEBHOOK_URL"))

    state = state_mod.load(path)
    if state is None:
        if cfg.mode == "paper":
            cash = cfg.paper.starting_cash_usd
        else:
            cash = min(broker.cash(), cfg.live.max_capital_usd)
        state = state_mod.new_state(cfg.mode, cash, iso(now_ms))
        notifier.send(f"👋 Memecoin desk started in {cfg.mode} mode with ${cash:,.2f}.")

    desk = Desk(cfg, state, CryptoComMarket(), broker, notifier)
    exit_code = 0
    try:
        snapshot = desk.run(now_ms)
        state["consecutive_errors"] = 0
        (Path(args.state_dir) / "REPORT.md").write_text(
            render_report(state, snapshot, iso(now_ms), cfg))
        print(f"{iso(now_ms)} equity ${snapshot['equity']:,.2f} "
              f"positions {len(state['positions'])} reserve ${state['reserve_usd']:.2f}")
    except Exception as err:
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        print(f"run failed ({state['consecutive_errors']} in a row): {err!r}", file=sys.stderr)
        if state["consecutive_errors"] == ERRORS_BEFORE_ALERT:
            notifier.send(f"⚠️ Desk has failed {ERRORS_BEFORE_ALERT} runs in a row: {err}")
        # Fail the job (so GitHub emails you) only once it's clearly not a blip.
        exit_code = 1 if state["consecutive_errors"] >= ERRORS_BEFORE_ALERT else 0
    finally:
        state_mod.save(path, state)
    return exit_code


def cmd_status(args) -> int:
    report = Path(args.state_dir) / "REPORT.md"
    print(report.read_text() if report.exists() else "No report yet; the desk hasn't run.")
    return 0


def cmd_resume(args) -> int:
    cfg = config_mod.load(args.config)
    path = _state_path(args, cfg)
    state = state_mod.load(path)
    if not state or not state.get("halted"):
        print("Desk is not halted.")
        return 0
    print(f"Clearing halt: {state['halted']['reason']}")
    state["halted"] = None
    state["peak_equity"] = None  # measure the next drawdown from where we are now
    state_mod.save(path, state)
    return 0


def _load_series(directory: str) -> dict[str, list[Candle]]:
    series = {}
    for file in sorted(Path(directory).glob("*.json")):
        rows = json.loads(file.read_text())
        series[file.stem] = [Candle(int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]),
                                    float(r["c"]), float(r["v"])) for r in rows]
    return series


def _buy_and_hold_pct(series: dict[str, list[Candle]], start_ms: int, end_ms: int) -> float | None:
    """Average return of simply holding every coin through the window, for comparison."""
    moves = []
    for candles in series.values():
        inside = [c for c in candles if start_ms <= c.ts < end_ms]
        if len(inside) > 1 and inside[0].open > 0:
            moves.append(inside[-1].close / inside[0].open - 1)
    return round(100 * sum(moves) / len(moves), 2) if moves else None


def backtest(cfg, series: dict[str, list[Candle]], spread_pct: float = 0.003,
             start_ms: int | None = None, end_ms: int | None = None) -> dict:
    """Replay history through the team. Candles before start_ms only serve as warm-up."""
    market = ReplayMarket(series, cfg.analyst.timeframe, spread_pct)
    state = state_mod.new_state("paper", cfg.paper.starting_cash_usd, "backtest")
    desk = Desk(cfg, state, market, PaperBroker(cfg.paper.fee_pct, cfg.paper.slippage_pct))
    peak, max_dd, equity = 0.0, 0.0, cfg.paper.starting_cash_usd
    timeline = [t for t in market.timeline()
                if (start_ms is None or t >= start_ms) and (end_ms is None or t <= end_ms)]
    if not timeline:
        raise ValueError("no candles in the backtest window")
    for now_ms in timeline:
        market.now_ms = now_ms
        equity = desk.run(now_ms)["equity"] + state["reserve_usd"]
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak if peak else 0.0)
    trades = state["trades"]
    start = cfg.paper.starting_cash_usd
    days = (timeline[-1] - timeline[0]) / 86_400_000 if len(timeline) > 1 else 0
    return {
        "days": round(days, 1),
        "start": start,
        "end_equity_incl_reserve": round(equity, 2),
        "return_pct": round((equity / start - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(100 * sum(t["pnl"] > 0 for t in trades) / len(trades), 1) if trades else 0,
        "fees": round(state["fees_total"], 2),
        "reserve": round(state["reserve_usd"], 2),
        "subscription_months_covered": round(state["reserve_usd"] / cfg.treasury.monthly_subscription_usd, 2)
        if cfg.treasury.monthly_subscription_usd else None,
        "buy_and_hold_pct": _buy_and_hold_pct(series, timeline[0], timeline[-1]),
        "halted": state["halted"],
        "open_positions": list(state["positions"]),
    }


def walk_forward(cfg, series: dict[str, list[Candle]], window_days: int,
                 spread_pct: float = 0.003) -> dict:
    """Split the history into back-to-back windows and backtest each one from a fresh $start."""
    step = window_days * 86_400_000
    first = min(c.ts for s in series.values() for c in s if s)
    last = max(c.ts for s in series.values() for c in s if s)
    warmup = (cfg.analyst.candles + 2) * 3_600_000
    windows, start = [], first + warmup
    while start + step <= last + 3_600_000:
        r = backtest(cfg, series, spread_pct, start, start + step)
        r["from"] = iso(start)[:10]
        r["to"] = iso(start + step)[:10]
        windows.append(r)
        start += step
    rets = [w["return_pct"] for w in windows]
    return {
        "window_days": window_days,
        "windows": [{k: w[k] for k in ("from", "to", "return_pct", "buy_and_hold_pct",
                                        "max_drawdown_pct", "trades", "win_rate_pct",
                                        "reserve", "halted")} for w in windows],
        "profitable_windows": f"{sum(r > 0 for r in rets)}/{len(rets)}",
        "avg_return_pct": round(sum(rets) / len(rets), 2) if rets else None,
        "worst_return_pct": min(rets) if rets else None,
        "best_return_pct": max(rets) if rets else None,
        "total_reserve": round(sum(w["reserve"] for w in windows), 2),
    }


def cmd_backtest(args) -> int:
    cfg = config_mod.load(args.config)
    if args.data:
        series = _load_series(args.data)
    else:
        market = CryptoComMarket()
        series = {}
        for name in cfg.universe:
            try:
                series[name] = market.history(name, cfg.analyst.timeframe, args.days * 24)
                print(f"fetched {len(series[name])} candles for {name}", file=sys.stderr)
            except Exception as err:
                print(f"skip {name}: {err}", file=sys.stderr)
    if args.window:
        result = walk_forward(cfg, series, args.window, args.spread)
    else:
        result = backtest(cfg, series, args.spread)
    print(json.dumps(result, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="coinbot", description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--state-dir", default="state", help="where state and REPORT.md live")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run one cycle of the desk")
    sub.add_parser("status", help="print the latest report")
    sub.add_parser("resume", help="clear a kill-switch halt")
    bt = sub.add_parser("backtest", help="replay history through the same team")
    bt.add_argument("--days", type=int, default=30)
    bt.add_argument("--data", help="directory of <INSTRUMENT>.json candle files (t,o,h,l,c,v)")
    bt.add_argument("--spread", type=float, default=0.003, help="assumed bid/ask spread")
    bt.add_argument("--window", type=int, default=0,
                    help="split history into back-to-back windows of this many days")
    args = parser.parse_args(argv)
    return {"run": cmd_run, "status": cmd_status, "resume": cmd_resume,
            "backtest": cmd_backtest}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
