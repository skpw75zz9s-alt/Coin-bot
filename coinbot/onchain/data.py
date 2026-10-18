"""Free on-chain data: DexScreener (pairs, flow), GeckoTerminal (trending), Solana RPC (holders)."""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass

from ..market import http_get_json

DEXSCREENER = "https://api.dexscreener.com/"
GECKOTERMINAL = "https://api.geckoterminal.com/api/v2/"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
# Owners of pool vaults that are not the pool address itself.
AMM_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM
}


def http_post_json(url: str, body: dict, timeout: float = 20.0, retries: int = 3) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "User-Agent": "coinbot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as err:
            last_err = err
            time.sleep(2**attempt)
    raise RuntimeError(f"POST {url} failed after {retries} attempts: {last_err}")


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Pair:
    token: str
    symbol: str
    pair_address: str
    dex: str
    price_usd: float
    liquidity_usd: float
    mcap_usd: float
    created_ms: int
    buys_h1: int
    sells_h1: int
    buys_h6: int
    sells_h6: int
    vol_h1: float
    vol_h6: float
    vol_h24: float
    chg_h1: float   # fractions, e.g. 0.12 = +12%
    chg_h6: float
    chg_h24: float
    url: str = ""

    def age_hours(self, now_ms: int) -> float:
        return (now_ms - self.created_ms) / 3_600_000 if self.created_ms else 0.0

    @property
    def liq_to_mcap(self) -> float:
        return self.liquidity_usd / self.mcap_usd if self.mcap_usd > 0 else 0.0


def parse_pair(row: dict) -> Pair | None:
    try:
        txns, vol, chg = row.get("txns") or {}, row.get("volume") or {}, row.get("priceChange") or {}
        mcap = _f(row.get("marketCap")) or _f(row.get("fdv"))
        return Pair(
            token=row["baseToken"]["address"],
            symbol=row["baseToken"].get("symbol", "?"),
            pair_address=row.get("pairAddress", ""),
            dex=row.get("dexId", ""),
            price_usd=_f(row.get("priceUsd")),
            liquidity_usd=_f((row.get("liquidity") or {}).get("usd")),
            mcap_usd=mcap,
            created_ms=int(row.get("pairCreatedAt") or 0),
            buys_h1=int((txns.get("h1") or {}).get("buys", 0)),
            sells_h1=int((txns.get("h1") or {}).get("sells", 0)),
            buys_h6=int((txns.get("h6") or {}).get("buys", 0)),
            sells_h6=int((txns.get("h6") or {}).get("sells", 0)),
            vol_h1=_f(vol.get("h1")),
            vol_h6=_f(vol.get("h6")),
            vol_h24=_f(vol.get("h24")),
            chg_h1=_f(chg.get("h1")) / 100,
            chg_h6=_f(chg.get("h6")) / 100,
            chg_h24=_f(chg.get("h24")) / 100,
            url=row.get("url", ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Holders:
    supply: float
    top: list            # [(owner, amount)] largest first, pool vaults excluded
    mint_authority: str | None
    freeze_authority: str | None
    token_program: str

    def share(self, n: int) -> float:
        return sum(a for _, a in self.top[:n]) / self.supply if self.supply > 0 else 1.0

    @property
    def top10_pct(self) -> float:
        return self.share(10)

    @property
    def max_single_pct(self) -> float:
        return self.share(1)


class OnchainData:
    def __init__(self, get_json=http_get_json, post_json=http_post_json, rpc_url: str = SOLANA_RPC):
        self._get = get_json
        self._post = post_json
        self.rpc_url = rpc_url
        self.all_pairs: dict[str, list[Pair]] = {}

    # -- discovery -------------------------------------------------------------------------
    def candidates(self, chain: str) -> list[str]:
        """Tokens people are paying attention to right now, most recent first, deduplicated."""
        seen: dict[str, None] = {}
        for path in ("token-boosts/latest/v1", "token-boosts/top/v1", "token-profiles/latest/v1"):
            try:
                rows = self._get(DEXSCREENER + path)
            except Exception as err:
                print(f"dexscreener {path}: {err}")
                continue
            for row in rows if isinstance(rows, list) else []:
                if row.get("chainId") == chain and row.get("tokenAddress"):
                    seen.setdefault(row["tokenAddress"], None)
        try:
            trending = self._get(f"{GECKOTERMINAL}networks/{chain}/trending_pools")
            for pool in trending.get("data", []):
                ref = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {})
                prefix = f"{chain}_"
                if str(ref.get("id", "")).startswith(prefix):
                    seen.setdefault(ref["id"][len(prefix):], None)
        except Exception as err:
            print(f"geckoterminal trending: {err}")
        return list(seen)

    # -- market ----------------------------------------------------------------------------
    def pairs(self, chain: str, tokens: list[str], dex_allowlist: list[str]) -> dict[str, Pair]:
        """Deepest allowed pool for each token."""
        best: dict[str, Pair] = {}
        for i in range(0, len(tokens), 30):
            chunk = tokens[i : i + 30]
            try:
                rows = self._get(f"{DEXSCREENER}tokens/v1/{chain}/{','.join(chunk)}")
            except Exception as err:
                print(f"dexscreener pairs: {err}")
                continue
            for row in rows if isinstance(rows, list) else []:
                pair = parse_pair(row)
                if pair is None or pair.token not in chunk:
                    continue
                self.all_pairs.setdefault(pair.token, []).append(pair)
                if pair.dex not in dex_allowlist:
                    continue
                if pair.token not in best or pair.liquidity_usd > best[pair.token].liquidity_usd:
                    best[pair.token] = pair
        return best

    # -- holders ---------------------------------------------------------------------------
    def _rpc(self, method: str, params: list):
        resp = self._post(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error']}")
        return resp["result"]

    def holders(self, mint: str) -> Holders:
        info = self._rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])["value"]
        parsed = info["data"]["parsed"]["info"]
        decimals = int(parsed.get("decimals", 0))
        supply = int(parsed.get("supply", 0)) / 10**decimals
        largest = self._rpc("getTokenLargestAccounts", [mint])["value"]
        accounts = [a["address"] for a in largest]
        owners_raw = self._rpc("getMultipleAccounts", [accounts, {"encoding": "jsonParsed"}])["value"]
        pools = {p.pair_address for p in self.all_pairs.get(mint, [])} | AMM_AUTHORITIES
        by_owner: dict[str, float] = {}
        for acct, owner_info in zip(largest, owners_raw):
            owner = (((owner_info or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
            owner = owner or acct["address"]
            if owner in pools:
                continue
            by_owner[owner] = by_owner.get(owner, 0.0) + _f(acct.get("uiAmount"))
        top = sorted(by_owner.items(), key=lambda kv: kv[1], reverse=True)
        return Holders(supply=supply, top=top,
                       mint_authority=parsed.get("mintAuthority"),
                       freeze_authority=parsed.get("freezeAuthority"),
                       token_program=info.get("owner", ""))
