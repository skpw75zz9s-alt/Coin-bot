import unittest

from coinbot import config as config_mod
from coinbot import state as state_mod
from coinbot.onchain.analyst import assess, render
from coinbot.onchain.data import AMM_AUTHORITIES, TOKEN_2022, OnchainData, parse_pair
from coinbot.onchain.desk import OnchainDesk, PaperDex

HOUR = 3_600_000
NOW = 1_790_000_000_000
POOL = "Pool1111111111111111111111111111111111111111"


def pair_row(token="Mint1", price=0.001, liq=200_000, mcap=2_000_000, age_h=30, buys_h1=400,
             sells_h1=200, buys_h6=1200, sells_h6=900, vol_h1=60_000, vol_h6=240_000,
             chg_h1=12, chg_h6=20, chg_h24=40, dex="raydium", pool=POOL):
    return {
        "chainId": "solana", "dexId": dex, "url": f"https://dexscreener.com/solana/{pool}",
        "pairAddress": pool,
        "baseToken": {"address": token, "name": "Test", "symbol": "TEST"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
        "priceUsd": str(price),
        "txns": {"h1": {"buys": buys_h1, "sells": sells_h1}, "h6": {"buys": buys_h6, "sells": sells_h6}},
        "volume": {"h1": vol_h1, "h6": vol_h6, "h24": vol_h6 * 3},
        "priceChange": {"h1": chg_h1, "h6": chg_h6, "h24": chg_h24},
        "liquidity": {"usd": liq}, "fdv": mcap, "marketCap": mcap,
        "pairCreatedAt": NOW - age_h * HOUR,
    }


class FakeChain:
    """Routes DexScreener/GeckoTerminal GETs and Solana RPC POSTs to canned data."""

    def __init__(self, pairs, holders=None, mint_authority=None, program="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"):
        self.pairs = pairs  # token -> pair row (mutable between runs)
        self.supply = 1_000_000_000
        self.holders = holders or [(POOL, 300_000_000)] + [(f"W{i}", 10_000_000) for i in range(19)]
        self.mint_authority = mint_authority
        self.program = program

    def get(self, url):
        if "token-boosts" in url or "token-profiles" in url:
            return [{"chainId": "solana", "tokenAddress": t} for t in self.pairs] + [
                {"chainId": "ethereum", "tokenAddress": "0xnope"}]
        if "trending_pools" in url:
            return {"data": [{"relationships": {"base_token": {"data": {"id": "solana_Extra"}}}}]}
        if "/tokens/v1/solana/" in url:
            asked = url.rsplit("/", 1)[1].split(",")
            return [self.pairs[t] for t in asked if t in self.pairs and self.pairs[t]]
        raise AssertionError(url)

    def post(self, url, body):
        m, params = body["method"], body["params"]
        if m == "getAccountInfo":
            return {"result": {"value": {"owner": self.program, "data": {"parsed": {"info": {
                "decimals": 6, "supply": str(self.supply * 10**6),
                "mintAuthority": self.mint_authority, "freezeAuthority": None}}}}}}
        if m == "getTokenLargestAccounts":
            return {"result": {"value": [{"address": f"acct-{o}", "uiAmount": a} for o, a in self.holders]}}
        if m == "getMultipleAccounts":
            return {"result": {"value": [{"data": {"parsed": {"info": {"owner": a.removeprefix("acct-")}}}}
                                         for a in params[0]]}}
        raise AssertionError(m)

    def data(self):
        return OnchainData(get_json=self.get, post_json=self.post)


def cfg():
    return config_mod.load(env={})


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self.oc = cfg().onchain

    def holders(self, chain, token="Mint1"):
        data = chain.data()
        data.pairs("solana", [token], self.oc.dex_allowlist)
        return data.holders(token)

    def test_healthy_momentum_token_is_follow_and_pool_excluded(self):
        chain = FakeChain({"Mint1": pair_row()})
        h = self.holders(chain)
        self.assertNotIn(POOL, [o for o, _ in h.top])  # the pool's tokens are not a "holder"
        self.assertAlmostEqual(h.top10_pct, 0.10)
        r = assess(parse_pair(pair_row()), h, NOW, self.oc)
        self.assertEqual(r.verdict, "FOLLOW", r.flags)
        block = render(r, NOW, self.oc)
        for line in ("VERDICT: FOLLOW", "SMART MONEY: not visible", "TAKE PROFIT:", "INVALIDATION:",
                     "Who is my exit liquidity?"):
            self.assertIn(line, block)

    def test_red_flags_kill_the_trade(self):
        cases = {
            "thin liquidity": dict(liq=30_000, mcap=2_000_000),
            "too new": dict(age_h=1),
            "already pumped": dict(chg_h24=450),
            "distribution": dict(buys_h1=200, sells_h1=190, chg_h1=1),
        }
        for name, kw in cases.items():
            r = assess(parse_pair(pair_row(**kw)), None, NOW, self.oc)
            self.assertEqual(r.verdict, "SKIP", name)

    def test_holder_red_flags(self):
        p = parse_pair(pair_row())
        whale = FakeChain({"Mint1": pair_row()}, holders=[("Whale", 200_000_000)])
        r = assess(p, self.holders(whale), NOW, self.oc)
        self.assertEqual(r.verdict, "SKIP")
        self.assertTrue(any("one wallet holds 20%" in f for f in r.flags))
        self.assertTrue(any("top 3 sell half" in f for f in r.flags))
        mintable = FakeChain({"Mint1": pair_row()}, mint_authority="Dev")
        self.assertIn("mint authority still enabled: dev can print supply",
                      assess(p, self.holders(mintable), NOW, self.oc).flags)
        t22 = FakeChain({"Mint1": pair_row()}, program=TOKEN_2022)
        self.assertEqual(assess(p, self.holders(t22), NOW, self.oc).verdict, "SKIP")

    def test_missing_holder_data_never_follows(self):
        self.assertEqual(assess(parse_pair(pair_row()), None, NOW, self.oc).verdict, "SKIP")

    def test_amm_authority_excluded(self):
        auth = next(iter(AMM_AUTHORITIES))
        chain = FakeChain({"Mint1": pair_row()}, holders=[(auth, 500_000_000), ("W", 1_000_000)])
        self.assertEqual([o for o, _ in self.holders(chain).top], ["W"])


class PaperDexTests(unittest.TestCase):
    def test_bigger_orders_get_worse_prices(self):
        dex = PaperDex(0.01)
        p = parse_pair(pair_row(price=1.0, liq=100_000))
        _, small_price, _ = dex.buy(p, 100)
        _, big_price, _ = dex.buy(p, 5_000)
        self.assertLess(small_price, big_price)
        usd, _, _ = dex.sell(p, 100)
        self.assertLess(usd, 100)


class DeskTests(unittest.TestCase):
    def make(self, pairs):
        c = cfg()
        chain = FakeChain(pairs)
        state = state_mod.new_state("paper", 500, "x")
        return c, chain, state, OnchainDesk(c, state, chain.data())

    def run_at(self, desk, chain, t):
        desk.data = chain.data()
        desk.run(t)

    def test_buys_follow_then_takes_profit_in_steps_and_trails(self):
        c, chain, state, desk = self.make({"Mint1": pair_row(price=0.001)})
        self.run_at(desk, chain, NOW)
        pos = state["positions"]["Mint1"]
        self.assertAlmostEqual(pos["cost_usd"], 20.0)  # 4% of $500
        entry = pos["entry_price"]
        # Up 1.6x: first take-profit sells 40% of the position.
        chain.pairs["Mint1"] = pair_row(price=entry * 1.6, mcap=3_200_000, chg_h1=5)
        self.run_at(desk, chain, NOW + HOUR)
        self.assertEqual(state["positions"]["Mint1"]["tp_done"], 1)
        self.assertAlmostEqual(state["trades"][-1]["fraction"], 0.4)
        # Up 2.6x: second take-profit sells another 40% of the original (2/3 of what's left).
        chain.pairs["Mint1"] = pair_row(price=entry * 2.6, mcap=5_200_000, chg_h1=5)
        self.run_at(desk, chain, NOW + 2 * HOUR)
        self.assertAlmostEqual(state["trades"][-1]["fraction"], 0.4 / 0.6)
        # Falls 35% from the peak: the runner trails out.
        chain.pairs["Mint1"] = pair_row(price=entry * 2.6 * 0.65, mcap=3_400_000, chg_h1=-5)
        self.run_at(desk, chain, NOW + 3 * HOUR)
        self.assertNotIn("Mint1", state["positions"])
        self.assertEqual(state["trades"][-1]["reason"], "trailing stop")
        self.assertGreater(state["realized_pnl_total"], 0)
        self.assertGreater(state["reserve_usd"], 0)

    def test_liquidity_pull_exits_immediately(self):
        c, chain, state, desk = self.make({"Mint1": pair_row()})
        self.run_at(desk, chain, NOW)
        chain.pairs["Mint1"] = pair_row(liq=100_000)
        self.run_at(desk, chain, NOW + HOUR)
        self.assertEqual(state["trades"][-1]["reason"], "leave now: liquidity pulled")

    def test_vanished_pool_is_written_off(self):
        c, chain, state, desk = self.make({"Mint1": pair_row()})
        self.run_at(desk, chain, NOW)
        chain.pairs["Mint1"] = None
        for i in range(1, 4):
            self.run_at(desk, chain, NOW + i * HOUR)
        self.assertNotIn("Mint1", state["positions"])
        self.assertAlmostEqual(state["trades"][-1]["pnl"], -20.0)

    def test_report_renders(self):
        c, chain, state, desk = self.make({"Mint1": pair_row()})
        self.run_at(desk, chain, NOW)
        md = desk.report_md()
        self.assertIn("On-chain desk", md)
        self.assertIn("VERDICT: FOLLOW", md)


if __name__ == "__main__":
    unittest.main()
