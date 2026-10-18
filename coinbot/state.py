"""The desk's memory between runs: a single JSON file, written atomically."""

from __future__ import annotations

import json
import os
from pathlib import Path

MAX_TRADES_KEPT = 1000


def new_state(mode: str, starting_cash: float, now_iso: str) -> dict:
    return {
        "version": 1,
        "mode": mode,
        "created_at": now_iso,
        "cash": starting_cash,          # paper mode only; live mode reads the exchange
        "starting_equity": None,        # set on the first run
        "peak_equity": None,
        "reserve_usd": 0.0,             # ring-fenced subscription money, never traded
        "unswept_profit": 0.0,          # net realized P&L not yet split with the reserve
        "realized_pnl_total": 0.0,
        "fees_total": 0.0,
        "positions": {},
        "trades": [],
        "cooldowns": {},
        "day": None,                    # {"date", "start_equity"}
        "months": {},                   # "YYYY-MM" -> {"swept", "realized"}
        "halted": None,                 # {"reason", "at"} once the kill switch fires
        "last_run": None,
        "consecutive_errors": 0,
    }


def load(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path) as fh:
        return json.load(fh)


def save(path: Path, state: dict) -> None:
    state["trades"] = state["trades"][-MAX_TRADES_KEPT:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
