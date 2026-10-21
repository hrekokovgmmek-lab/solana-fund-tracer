import contextlib
import io
import json
import os
import tempfile
import unittest

from fundtrace import labels as lab
from fundtrace import ledger, parse, sinks, trace
from fundtrace.__main__ import main
from fundtrace.demo import LABELS, MEME, T0, USDC, Chain, _tx, chain
from fundtrace.parse import WSOL
from fundtrace.rpc import Rpc, RpcError


def rpc_for(txs=None, tmp=None):
    c = Chain(txs)
    return Rpc(cache_dir=os.path.join(tmp, "cache"), rps=0, transport=c.transport), c


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.rpc, self.chain = rpc_for(tmp=self.tmp)

    def tearDown(self):
        self._tmp.cleanup()


class ParseTest(unittest.TestCase):
    def test_buy_summary(self):
        t = {x["transaction"]["signatures"][0]: x for x in chain()}["sig05_buy"]
        u = parse.summary(t)
        self.assertAlmostEqual(u["sol"]["FRESH_W"], -40.000005)
        self.assertEqual(u["tok"][("FRESH_W", MEME)], 1_000_000)
        self.assertTrue(parse.is_trade(u))
        self.assertEqual(u["progs"], ["PumpSwap"])
        self.assertEqual(parse.counterparty(u, "FRESH_W", -40), "POOL")

    def test_loaded_addresses_count_as_keys(self):
        t = _tx("s", 1, ["A"], {"A": -1, "B": 1})
        t["meta"]["loadedAddresses"] = {"writable": ["C"], "readonly": ["D"]}
        self.assertEqual(parse.keys_of(t)[-2:], ["C", "D"])

    def test_counterparty_needs_opposite_sign(self):
        u = {"sol": {"A": 1.0, "B": 0.5}}
        self.assertIsNone(parse.counterparty(u, "A", 1.0))      # nobody paid: no source


class TraceTest(Base):
    def test_back_reaches_exchange(self):
        r = trace.back(self.rpc, "FRESH_W", LABELS)
        self.assertEqual([c["addr"] for c in r["chain"]], ["FRESH_W", "HUB_1", "HUB_2", "EXCHANGE_HOT"])
        self.assertEqual(r["stop"], "exchange")
        self.assertEqual(r["chain"][0]["in"]["sig"], "sig03_hub1_to_fresh")      # the earliest, not the largest

    def test_forward_reaches_exchange_and_skips_trades(self):
        r = trace.forward(self.rpc, "FRESH_W", LABELS)
        self.assertEqual([c["addr"] for c in r["chain"]], ["FRESH_W", "COLLECTOR", "DEPOSIT_D1", "EXCHANGE_HOT"])
        self.assertEqual(r["chain"][0]["out"]["sig"], "sig07_fresh_to_collector")  # the 40 SOL buy is not a transfer
        self.assertEqual(r["stop"], "exchange")

    def test_a_large_buy_is_not_followed_as_a_transfer(self):
        txs = [_tx("buy", 2, ["W"], {"W": -100, "POOL": 100}, [("W", MEME, 0, 5)], ix=("Buy",)),
               _tx("pay", 3, ["W"], {"W": -10, "X": 10})]
        rpc, _ = rpc_for(txs, self.tmp)
        self.assertEqual(trace.forward(rpc, "W", {})["chain"][0]["out"]["to"], "X")

    def test_busy_address_stops_the_walk(self):
        r = trace.back(self.rpc, "FRESH_W", LABELS, busy=5)
        self.assertEqual(len(r["chain"]), 1)
        self.assertEqual(r["stop"], "busy address")

    def test_loop_stops_the_walk(self):
        txs = [_tx("a1", 1, ["B"], {"B": -5, "A": 5}), _tx("b1", 0, ["A"], {"A": -9, "B": 9})]
        rpc, _ = rpc_for(txs, self.tmp)
        r = trace.back(rpc, "A", {})
        self.assertEqual([c["addr"] for c in r["chain"]], ["A", "B"])
        self.assertEqual(r["stop"], "loop")

    def test_without_labels_the_walk_passes_the_exchange_and_meets_the_loop(self):
        r = trace.back(self.rpc, "FRESH_W", {})
        self.assertEqual([c["addr"] for c in r["chain"]],
                         ["FRESH_W", "HUB_1", "HUB_2", "EXCHANGE_HOT", "DEPOSIT_D1", "COLLECTOR"])
        self.assertEqual(r["stop"], "loop")

    def test_deposit_address(self):
        d = trace.deposit(self.rpc, "DEPOSIT_D1", LABELS)
        self.assertTrue(d["is_deposit"])
        self.assertEqual(d["sweeps"], {"Exchange hot wallet 1": 1})
        self.assertAlmostEqual(d["sol_to_exchange"]["Exchange hot wallet 1"], 73.99 - 0.000005)
        self.assertFalse(trace.deposit(self.rpc, "COLLECTOR", LABELS)["is_deposit"])

    def test_funding_flags_memo(self):
        f = trace.funding(self.rpc, "FRESH_W", "sig05_buy")["funding"]
        self.assertEqual([(x["from"], x["sol"], x["memo"]) for x in f], [("HUB_1", 20.0, True), ("HUB_1", 50.0, False)])
        self.assertIn("ONE_OFF_COSIGNER", f[0]["signers"])


class SinksTest(Base):
    GROUP = ["SELLER_A", "SELLER_B", "SELLER_C", "SELLER_D"]

    def test_direct_convergence(self):
        r = sinks.sinks(self.rpc, self.GROUP, LABELS)
        self.assertEqual([(x["addr"], x["senders"], x["sol"]) for x in r["sinks"]], [("HUB_2", 2, 119.8)])
        self.assertEqual(sorted(r["sinks"][0]["from"]), ["SELLER_A", "SELLER_D"])

    def test_depth_two_follows_one_off_wallets(self):
        r = sinks.sinks(self.rpc, self.GROUP, LABELS, depth=2)
        top = r["sinks"][0]
        self.assertEqual((top["addr"], top["senders"], top["via"]), ("HUB_2", 3, ["MID_1"]))
        self.assertAlmostEqual(top["sol"], 179.69)

    def test_min_senders_one_lists_every_destination(self):
        got = {x["addr"] for x in sinks.sinks(self.rpc, self.GROUP, LABELS, min_senders=1)["sinks"]}
        self.assertEqual(got, {"HUB_2", "MID_1", "C_COLD"})

    def test_since_drops_earlier_transfers(self):
        since = T0 + 221 * 60
        self.assertEqual(sinks.sinks(self.rpc, self.GROUP, LABELS, since=since)["sinks"], [])

    def test_trades_and_group_internal_transfers_are_not_outflows(self):
        txs = [_tx("buy", 1, ["A"], {"A": -5, "POOL": 5}, [("A", MEME, 0, 9)], ix=("Buy",)),
               _tx("a_to_b", 2, ["A"], {"A": -3, "B": 3}),
               _tx("b_to_x", 3, ["B"], {"B": -2, "X": 2})]
        rpc, _ = rpc_for(txs, self.tmp)
        self.assertEqual(sinks.outflows(rpc, "A"), [("B", 3.0, T0 + 120, "a_to_b")])
        r = sinks.sinks(rpc, ["A", "B"], {}, min_senders=1)
        self.assertEqual([x["addr"] for x in r["sinks"]], ["X"])          # A -> B stays inside the group

    def test_wrapped_sol_credits_the_owner_not_the_token_account(self):
        tx = _tx("wsol", 5, ["S"], {"HUB:So1111": 10}, [("S", WSOL, 10, 0), ("HUB", WSOL, 0, 10)], ix=("Transfer",))
        rpc, _ = rpc_for([tx], self.tmp)
        self.assertEqual([w for w, *_ in sinks.outflows(rpc, "S")], ["HUB"])


class LedgerTest(Base):
    def test_build_is_resumable(self):
        path = os.path.join(self.tmp, "l.jsonl")
        r1 = ledger.build(self.rpc, "FRESH_W", path)
        r2 = ledger.build(self.rpc, "FRESH_W", path)
        self.assertEqual((r1["added"], r2["added"], r2["already"]), (5, 0, 5))
        rows = ledger.read(path)
        self.assertEqual([r["sig"][:5] for r in rows], ["sig03", "sig04", "sig05", "sig06", "sig07"])
        self.assertEqual(rows[2]["tok"], {MEME: 1_000_000})

    def test_summary(self):
        path = os.path.join(self.tmp, "l.jsonl")
        ledger.build(self.rpc, "FRESH_W", path)
        s = ledger.summarize(ledger.read(path), LABELS)
        self.assertEqual(s["kinds"], {"transfers": 3, "trades": 2})
        self.assertAlmostEqual(s["trade_net_sol"], 4.99999)
        self.assertEqual([(p["addr"], p["in"], p["n_in"]) for p in s["received_from"]], [("HUB_1", 70.0, 2)])
        self.assertEqual(s["sent_to"][0]["addr"], "COLLECTOR")

    def test_stables(self):
        path = os.path.join(self.tmp, "c.jsonl")
        ledger.build(self.rpc, "COLLECTOR", path)
        r = ledger.stables(self.rpc, "COLLECTOR", ledger.read(path), LABELS)
        self.assertEqual([(f["addr"], f["coin"], f["out"], f["in"]) for f in r["flows"]],
                         [("BRIDGE", "USDC", 1000.0, 0.0), ("HUB_1", "USDC", 0.0, 300.0)])


class RpcTest(Base):
    def test_cache_avoids_network(self):
        self.rpc.tx("sig01_exchange_withdrawal")
        n = self.chain.calls
        self.rpc.tx("sig01_exchange_withdrawal")
        self.assertEqual(self.chain.calls, n)
        self.assertEqual(self.rpc.stats["cache"], 1)

    def test_fresh_rereads_signatures_only(self):
        self.rpc.sigs("HUB_1")
        self.rpc.tx("sig03_hub1_to_fresh")
        self.rpc.fresh = True
        n = self.chain.calls
        self.rpc.sigs("HUB_1")
        self.rpc.tx("sig03_hub1_to_fresh")
        self.assertEqual(self.chain.calls, n + 1)

    def test_rate_limit_error_is_retried(self):
        answers = [json.dumps({"error": {"code": 429, "message": "Too many requests"}}),
                   json.dumps({"result": {"ok": 1}})]
        slept = []
        rpc = Rpc(cache_dir=os.path.join(self.tmp, "c2"), rps=0, transport=lambda u, b: answers.pop(0),
                  sleep=slept.append)
        self.assertEqual(rpc.call("getX", [])["result"], {"ok": 1})
        self.assertEqual(slept, [5])

    def test_real_error_is_raised_and_not_cached(self):
        rpc = Rpc(cache_dir=os.path.join(self.tmp, "c3"), rps=0,
                  transport=lambda u, b: json.dumps({"error": {"code": -32602, "message": "Invalid param"}}))
        with self.assertRaises(RpcError):
            rpc.call("getX", [])
        cache = os.path.join(self.tmp, "c3")
        self.assertEqual([f for f in (os.listdir(cache) if os.path.exists(cache) else []) if f.endswith(".json")], [])

    def test_rate_limit_waits_between_requests(self):
        slept = []
        rpc = Rpc(cache_dir=os.path.join(self.tmp, "c4"), rps=1, transport=self.chain.transport, sleep=slept.append)
        rpc.sigs("HUB_1")
        rpc.sigs("HUB_2")
        self.assertGreater(slept[-1], 0.9)


class LabelsTest(unittest.TestCase):
    SQL = """SELECT blockchain, address, cex_name, distinct_name FROM (VALUES
        ('solana', 'AAAAbbbbCCCCddddEEEEffffGGGGhhhhJJJJ', 'Some CEX', 'Some CEX 1', 'x', date '2025-01-01')
        , ('solana', 'KKKKmmmmNNNNppppQQQQrrrrSSSSttttUUUU', 'Other', 'Other 7', 'x', date '2025-01-01')
        , ('solana', 'not-base58!', 'Bad', 'Bad', 'x', date '2025-01-01'))"""

    def test_parse_spellbook(self):
        got = lab.parse_spellbook(self.SQL)
        self.assertEqual(got["AAAAbbbbCCCCddddEEEEffffGGGGhhhhJJJJ"], {"cex": "Some CEX", "name": "Some CEX 1"})
        self.assertEqual(len(got), 2)

    def test_fetch_keeps_own_labels(self):
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "labels.json")
            with open(dest, "w") as f:
                json.dump({"MINE": {"cex": "My", "name": "my label"}}, f)
            self.assertEqual(lab.fetch(dest, get=lambda url: self.SQL), 2)
            self.assertEqual(len(lab.load(dest)), 3)


class CliTest(unittest.TestCase):
    def test_demo_runs(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main(["demo"])
        out = buf.getvalue()
        self.assertIn("DEPOSIT_D1: DEPOSIT ADDRESS", out)
        self.assertIn("MEMO, signers: ONE_OFF_COSIGNER", out)
        self.assertIn("BRIDGE", out)
        self.assertIn("3/4 wallets", out)

    def test_sinks_needs_two_wallets(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["sinks", "ONLY_ONE"])


if __name__ == "__main__":
    unittest.main()
