"""A small synthetic chain for the offline demo and the tests.

Every account, signature and amount here is made up. The story is the one the tracer is built for: money leaves an
exchange, passes two hub wallets into a fresh wallet (one transfer hidden behind a memo and a one-off co-signer), the
wallet trades a token, sends the proceeds on, and they come back to the exchange through a customer deposit address.
"""
import json

SYSTEM = "11111111111111111111111111111111"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEME = "MEME_TOKEN_MINT"
T0 = 1767225600                                        # 2026-01-01 00:00 UTC
FEE = 5000

LABELS = {"EXCHANGE_HOT": {"cex": "Exchange", "name": "Exchange hot wallet 1"}}


def _tx(sig, slot, signers, sol=None, tokens=(), ix=(), progs=(SYSTEM,)):
    """A jsonParsed transaction. sol: {account: SOL change}; tokens: [(owner, mint, before, after)]."""
    sol = dict(sol or {})
    payer = signers[0]
    keys = list(signers)
    for a in list(sol) + [o for o, *_ in tokens]:
        if a not in keys:
            keys.append(a)
    token_accounts = [f"{o}:{m[:6]}" for o, m, *_ in tokens]
    keys += token_accounts + [p for p in progs if p not in keys]
    pre = [1000 * 10**9 for _ in keys]
    post = list(pre)
    for a, d in sol.items():
        post[keys.index(a)] += round(d * 1e9)
    post[0] -= FEE
    bal = lambda i, o, m, x: {"accountIndex": i, "mint": m, "owner": o,
                              "uiTokenAmount": {"uiAmount": x, "decimals": 6, "amount": str(int(x * 1e6))}}
    pre_tok = [bal(keys.index(ta), o, m, a) for ta, (o, m, a, b) in zip(token_accounts, tokens)]
    post_tok = [bal(keys.index(ta), o, m, b) for ta, (o, m, a, b) in zip(token_accounts, tokens)]
    return {"slot": slot, "blockTime": T0 + slot * 60,
            "transaction": {"signatures": [sig], "message": {
                "accountKeys": [{"pubkey": k, "signer": k in signers, "writable": True} for k in keys],
                "instructions": [{"programId": p} for p in progs]}},
            "meta": {"err": None, "fee": FEE, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": pre_tok, "postTokenBalances": post_tok,
                     "logMessages": [f"Program log: Instruction: {n}" for n in ix], "loadedAddresses": {}}}


def chain():
    """The synthetic transactions, oldest first."""
    return [
        _tx("sig01_exchange_withdrawal", 100, ["EXCHANGE_HOT"], {"EXCHANGE_HOT": -500, "HUB_2": 500}),
        _tx("sig02_hub2_to_hub1", 110, ["HUB_2"], {"HUB_2": -200, "HUB_1": 200}),
        _tx("sig03_hub1_to_fresh", 120, ["HUB_1"], {"HUB_1": -50, "FRESH_W": 50}),
        _tx("sig04_memo_cosigned", 125, ["ONE_OFF_COSIGNER", "HUB_1"], {"HUB_1": -20, "FRESH_W": 20},
            progs=(SYSTEM, MEMO)),
        _tx("sig05_buy", 130, ["FRESH_W"], {"FRESH_W": -40, "POOL": 40}, [("FRESH_W", MEME, 0, 1_000_000)],
            ix=("Buy",), progs=(PUMPSWAP,)),
        _tx("sig06_sell", 140, ["FRESH_W"], {"FRESH_W": 45, "POOL": -45}, [("FRESH_W", MEME, 1_000_000, 0)],
            ix=("Sell",), progs=(PUMPSWAP,)),
        _tx("sig07_fresh_to_collector", 150, ["FRESH_W"], {"FRESH_W": -74, "COLLECTOR": 74}),
        _tx("sig08_collector_to_deposit", 160, ["COLLECTOR"], {"COLLECTOR": -74, "DEPOSIT_D1": 74}),
        _tx("sig09_sweep_to_exchange", 170, ["EXCHANGE_HOT", "DEPOSIT_D1"], {"DEPOSIT_D1": -73.99,
                                                                             "EXCHANGE_HOT": 73.99}),
        _tx("sig10_usdc_to_bridge", 180, ["COLLECTOR"], tokens=[("COLLECTOR", USDC, 1000, 0), ("BRIDGE", USDC, 0, 1000)],
            ix=("Transfer",), progs=(TOKEN,)),
        _tx("sig11_usdc_from_hub1", 190, ["HUB_1"], tokens=[("HUB_1", USDC, 300, 0), ("COLLECTOR", USDC, 0, 300)],
            ix=("Transfer",), progs=(TOKEN,)),
        # The return path: four sellers funded straight from the exchange (funding links nothing), then the profit
        # converges. A and D pay HUB_2 directly, B goes through a one-off wallet, C keeps its money.
        _tx("sig12_withdraw_a", 200, ["EXCHANGE_HOT"], {"EXCHANGE_HOT": -30, "SELLER_A": 30}),
        _tx("sig13_withdraw_b", 201, ["EXCHANGE_HOT"], {"EXCHANGE_HOT": -25, "SELLER_B": 25}),
        _tx("sig14_withdraw_c", 202, ["EXCHANGE_HOT"], {"EXCHANGE_HOT": -10, "SELLER_C": 10}),
        _tx("sig15_withdraw_d", 203, ["EXCHANGE_HOT"], {"EXCHANGE_HOT": -20, "SELLER_D": 20}),
        _tx("sig16_sell_a", 210, ["SELLER_A"], {"SELLER_A": 40, "POOL": -40}, [("SELLER_A", MEME, 500_000, 0)],
            ix=("Sell",), progs=(PUMPSWAP,)),
        _tx("sig17_sell_b", 211, ["SELLER_B"], {"SELLER_B": 35, "POOL": -35}, [("SELLER_B", MEME, 400_000, 0)],
            ix=("Sell",), progs=(PUMPSWAP,)),
        _tx("sig18_sell_c", 212, ["SELLER_C"], {"SELLER_C": 5, "POOL": -5}, [("SELLER_C", MEME, 100_000, 0)],
            ix=("Sell",), progs=(PUMPSWAP,)),
        _tx("sig19_sell_d", 213, ["SELLER_D"], {"SELLER_D": 30, "POOL": -30}, [("SELLER_D", MEME, 300_000, 0)],
            ix=("Sell",), progs=(PUMPSWAP,)),
        _tx("sig20_a_to_hub2", 220, ["SELLER_A"], {"SELLER_A": -69.9, "HUB_2": 69.9}),
        _tx("sig21_b_to_mid", 221, ["SELLER_B"], {"SELLER_B": -59.9, "MID_1": 59.9}),
        _tx("sig22_mid_to_hub2", 222, ["MID_1"], {"MID_1": -59.89, "HUB_2": 59.89}),
        _tx("sig23_c_to_own_cold", 223, ["SELLER_C"], {"SELLER_C": -14.9, "C_COLD": 14.9}),
        _tx("sig24_d_to_hub2", 224, ["SELLER_D"], {"SELLER_D": -49.9, "HUB_2": 49.9}),
    ]


class Chain:
    """Answers getTransaction and getSignaturesForAddress like an RPC node would, from `chain()`."""

    def __init__(self, txs=None):
        self.txs = {t["transaction"]["signatures"][0]: t for t in (txs or chain())}
        self.calls = 0

    def transport(self, url, body):
        self.calls += 1
        req = json.loads(body)
        method, params = req["method"], req["params"]
        if method == "getTransaction":
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": self.txs.get(params[0])})
        if method == "getSignaturesForAddress":
            addr, opt = params[0], params[1] if len(params) > 1 else {}
            mine = sorted((t for t in self.txs.values()
                           if addr in [k["pubkey"] for k in t["transaction"]["message"]["accountKeys"]]),
                          key=lambda t: -t["slot"])
            rows = [{"signature": t["transaction"]["signatures"][0], "slot": t["slot"], "blockTime": t["blockTime"],
                     "err": None} for t in mine]
            if opt.get("before"):
                i = [r["signature"] for r in rows].index(opt["before"])
                rows = rows[i + 1:]
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": rows[:opt.get("limit", 1000)]})
        return json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}})
