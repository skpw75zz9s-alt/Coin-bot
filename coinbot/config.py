"""Configuration loading: config.toml plus a few environment overrides."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.toml"

LIVE_CONFIRM_PHRASE = "I understand this can lose real money"


def _ns(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in value.items()})
    return value


def load(path: str | os.PathLike | None = None, env: dict | None = None) -> SimpleNamespace:
    env = os.environ if env is None else env
    with open(path or DEFAULT_PATH, "rb") as fh:
        raw = tomllib.load(fh)
    if env.get("COINBOT_MODE"):
        raw["mode"] = env["COINBOT_MODE"].strip().lower()
    if env.get("COINBOT_MONTHLY_SUBSCRIPTION_USD"):
        raw["treasury"]["monthly_subscription_usd"] = float(env["COINBOT_MONTHLY_SUBSCRIPTION_USD"])
    if env.get("COINBOT_LIVE_MAX_CAPITAL_USD"):
        raw["live"]["max_capital_usd"] = float(env["COINBOT_LIVE_MAX_CAPITAL_USD"])
    cfg = _ns(raw)
    if cfg.mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {cfg.mode!r}")
    return cfg


def live_trading_allowed(cfg, env: dict | None = None) -> tuple[bool, str]:
    """Live trading needs three separate things, so it can't be switched on by accident."""
    env = os.environ if env is None else env
    if cfg.mode != "live":
        return False, "mode is paper"
    if not (env.get("CRYPTOCOM_API_KEY") and env.get("CRYPTOCOM_API_SECRET")):
        return False, "CRYPTOCOM_API_KEY / CRYPTOCOM_API_SECRET not set"
    if env.get("COINBOT_LIVE_CONFIRM", "").strip() != LIVE_CONFIRM_PHRASE:
        return False, f"COINBOT_LIVE_CONFIRM must be exactly: {LIVE_CONFIRM_PHRASE}"
    return True, "ok"
