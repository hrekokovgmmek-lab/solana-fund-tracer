import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from fundtrace import labels as lab
from fundtrace import ledger, parse, relay, report, sinks, trace
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


class RelayTest(Base):
    AMOUNT = 187.4416

    def walk(self, sig, rpc=None, labels=LABELS, **kw):
        rpc = rpc or self.rpc
        return relay.relay(rpc, relay.transfer(rpc, sig), labels, **kw)

    def test_walks_both_ways_from_the_middle(self):
        r = self.walk("sig28_relay2_to_relay3")
        self.assertEqual([h["sig"][:5] for h in r["hops"]], ["sig25", "sig26", "sig28", "sig29"])
        self.assertEqual((r["first"], r["last"], r["seconds"]), ("HUB_2", "DEPOSIT_D2", 9))
        self.assertEqual([h["dt"] for h in r["hops"]], [None, 3, 3, 3])
        self.assertEqual((r["stop_back"], r["stop_forward"], r["round"]), ("no match", "no match", False))

    def test_a_wider_window_follows_the_sweep_into_the_exchange(self):
        r = self.walk("sig26_relay1_to_relay2", window=3600)
        self.assertEqual((r["last"], r["stop_forward"]), ("EXCHANGE_HOT", "exchange"))
        self.assertAlmostEqual(r["hops"][-1]["sol"], self.AMOUNT - 0.000005)     # the exchange paid the fee
        self.assertEqual(r["labels"], {"EXCHANGE_HOT": "Exchange hot wallet 1"})

    def test_a_short_window_cuts_the_chain(self):
        self.assertEqual([h["sig"] for h in self.walk("sig26_relay1_to_relay2", window=2)["hops"]],
                         ["sig26_relay1_to_relay2"])

    def test_find_by_address_and_amount(self):
        self.assertEqual(relay.find(self.rpc, "RELAY_3", self.AMOUNT)["sig"], "sig29_relay3_to_deposit")
        with self.assertRaises(ValueError):
            relay.find(self.rpc, "RELAY_3", 99.0)

    def test_amount_must_match_within_tolerance(self):
        txs = [_tx("a", 1, ["A"], {"A": -12.3456, "B": 12.3456}, t=T0),
               _tx("b", 2, ["B"], {"B": -12.3455, "C": 12.3455}, t=T0 + 2),     # a fee taken on the way: same money
               _tx("c", 3, ["C"], {"C": -12.3556, "D": 12.3556}, t=T0 + 4)]     # 0.01 SOL more: other money
        rpc, _ = rpc_for(txs, self.tmp)
        self.assertEqual(self.walk("a", rpc, {})["last"], "C")
        self.assertEqual(self.walk("a", rpc, {}, tol=0.02)["last"], "D")

    def test_money_coming_back_is_a_loop_and_round_sums_are_flagged(self):
        txs = [_tx("a", 1, ["A"], {"A": -5, "B": 5}, t=T0), _tx("b", 2, ["B"], {"B": -5, "A": 5}, t=T0 + 1)]
        rpc, _ = rpc_for(txs, self.tmp)
        r = self.walk("a", rpc, {})
        self.assertEqual((len(r["hops"]), r["stop_forward"], r["round"]), (2, "loop", True))
        self.assertTrue(relay.is_round(2500.000005))                # fee crumbs do not make a sum non-round
        self.assertFalse(relay.is_round(187.4416))

    def test_a_transfer_the_wallet_only_signed_is_not_its_own(self):
        txs = [_tx("a", 1, ["A"], {"A": -3.3333, "B": 3.3333}, t=T0),
               _tx("x", 2, ["B", "X"], {"X": -3.3333, "C": 3.3333}, t=T0 + 1)]      # B pays the fee, X the money
        rpc, _ = rpc_for(txs, self.tmp)
        self.assertEqual(self.walk("a", rpc, {})["last"], "B")

    def test_reads_inside_the_window_are_capped_nearest_first(self):
        txs = [_tx("in", 1, ["A"], {"A": -4.4444, "B": 4.4444}, t=T0)]
        txs += [_tx(f"n{i}", 2 + i, ["B"], {"B": -0.2, f"N{i}": 0.2}, t=T0 + 1) for i in range(5)]
        txs += [_tx("fwd", 10, ["B"], {"B": -4.4444, "C": 4.4444}, t=T0 + 2)]
        rpc, chain = rpc_for(txs, self.tmp)
        self.assertEqual(self.walk("in", rpc, {}, reads=6)["last"], "C")
        r = self.walk("in", rpc, {}, reads=4)
        self.assertEqual((r["last"], r["stop_forward"]), ("B", "busy address"))
        rpc, chain = rpc_for(txs, os.path.join(self.tmp, "cold"))
        r = self.walk("fwd", rpc, {}, reads=2, window=3)
        self.assertEqual((r["first"], r["stop_back"]), ("B", "busy address"))
        self.assertEqual(chain.calls, 5)          # the start, B's page, its 2 nearest transactions, C's page

    def test_a_trade_is_not_a_transfer(self):
        with self.assertRaises(ValueError):
            relay.transfer(self.rpc, "sig05_buy")

    def test_wrapped_sol_is_followed_to_its_owner(self):
        tx = _tx("wsol", 5, ["S"], {"HUB:So1111": 10}, [("S", WSOL, 10, 0), ("HUB", WSOL, 0, 10)], ix=("Transfer",))
        rpc, _ = rpc_for([tx], self.tmp)
        h = relay.transfer(rpc, "wsol")
        self.assertEqual((h["from"], h["to"], h["sol"]), ("S", "HUB", 10.0))

    def busy_chain(self, in_window):
        """B gets 7.77 SOL and sends it on 5 s later; `in_window` other payments come first, and B stays busy for
        hours after, so its newest signatures say nothing about the window. A stranger's block lands later,
        after two skipped slots."""
        txs = [_tx("a", 10, ["A"], {"A": -7.77, "B": 7.77}, t=T0)]
        txs += [_tx(f"w{i}", 11 + i, ["B"], {"B": -0.1, f"Y{i}": 0.1}, t=T0 + 1) for i in range(in_window)]
        txs += [_tx("fwd", 20, ["B"], {"B": -7.77, "C": 7.77}, t=T0 + 5),
                _tx("stranger", 312, ["S"], {"S": -1, "Z": 1}, t=T0 + 120)]
        txs += [_tx(f"late{i}", 500 + i, ["B"], {"B": -0.1, f"X{i}": 0.1}, t=T0 + 3600 + i) for i in range(5)]
        return rpc_for(txs, self.tmp)[0]

    def test_a_busy_address_is_read_back_from_a_later_block(self):
        with mock.patch.object(relay, "PAGE", 3):
            r = self.walk("a", self.busy_chain(0), {})
        self.assertEqual((r["last"], r["stop_forward"]), ("C", "no match"))

    def test_too_busy_inside_the_window_stops_the_walk(self):
        with mock.patch.object(relay, "PAGE", 3):
            r = self.walk("a", self.busy_chain(3), {})
        self.assertEqual((r["last"], r["stop_forward"]), ("B", "busy address"))


class ReportTest(Base):
    def results(self):
        return [relay.relay(self.rpc, relay.transfer(self.rpc, "sig28_relay2_to_relay3"), LABELS),
                trace.back(self.rpc, "FRESH_W", LABELS), trace.forward(self.rpc, "FRESH_W", LABELS),
                sinks.sinks(self.rpc, SinksTest.GROUP, LABELS, depth=2),
                trace.funding(self.rpc, "FRESH_W", "sig05_buy"), trace.deposit(self.rpc, "DEPOSIT_D1", LABELS)]

    def test_kind_is_read_from_the_keys(self):
        self.assertEqual([report.kind(r) for r in self.results()],
                         ["relay", "back", "forward", "sinks", "funding", "deposit"])

    def test_edges_follow_the_money(self):
        rs = self.results()
        self.assertEqual([(e["from"], e["to"]) for e in report.edges(rs[1])],
                         [("EXCHANGE_HOT", "HUB_2"), ("HUB_2", "HUB_1"), ("HUB_1", "FRESH_W")])
        self.assertEqual({(e["from"], e["to"]) for e in report.edges(rs[3])},
                         {("SELLER_A", "HUB_2"), ("SELLER_B", "MID_1"), ("MID_1", "HUB_2"), ("SELLER_D", "HUB_2")})
        self.assertEqual(report.edges(rs[0])[2]["note"], "start · +3 s")
        self.assertEqual([e["note"] for e in report.edges(rs[3]) if e["from"] == "MID_1"], ["via a one-off wallet"])

    def test_every_transfer_links_to_solscan(self):
        page = report.render(self.results(), LABELS)
        for sig in ("sig25_hub2_to_relay1", "sig29_relay3_to_deposit", "sig01_exchange_withdrawal",
                    "sig09_sweep_to_exchange", "sig21_b_to_mid", "sig22_mid_to_hub2", "sig04_memo_cosigned"):
            self.assertIn(f'href="https://solscan.io/tx/{sig}"', page)
        self.assertIn('href="https://solscan.io/account/DEPOSIT_D2"', page)
        self.assertEqual(page.count("<svg"), 5)                  # deposit has no transfers to draw

    def test_addresses_shared_by_sections(self):
        self.assertEqual(report.where(self.results())["HUB_2"], [1, 2, 4])

    def test_a_cycle_is_drawn_underneath(self):
        r = trace.back(self.rpc, "FRESH_W", {})                  # no labels: the walk comes back to FRESH_W
        svg = report.diagram(report.edges(r), report.roles(r), lambda a: "", 1)
        self.assertEqual(svg.count('class="edge back"'), 1)

    def test_text_from_the_chain_is_escaped(self):
        bad = {"EXCHANGE_HOT": {"cex": "x", "name": "<script>alert(1)</script>"}}
        r = trace.forward(self.rpc, "FRESH_W", bad)
        r["chain"][0]["out"]["sig"] = 'x" onmouseover="alert(1)'
        page = report.render([r], bad)
        self.assertNotIn("<script>alert", page)
        self.assertNotIn('" onmouseover', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("/tx/x%22%20onmouseover%3D%22alert%281%29", page)

    def test_load_reads_several_values_per_file(self):
        path = os.path.join(self.tmp, "r.json")
        with open(path, "w") as f:
            f.write(json.dumps({"a": 1}, indent=1) + "\n" + json.dumps([{"b": 2}, {"c": 3}]) + "\n")
        self.assertEqual(report.load([path]), [{"a": 1}, {"b": 2}, {"c": 3}])


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

    def test_before_and_until_may_be_any_transaction(self):
        # what `relay` relies on to read a busy address back from a stranger's later block
        self.assertEqual([x["signature"] for x in self.rpc.sigs("HUB_2", until="sig24_d_to_hub2")],
                         ["sig25_hub2_to_relay1"])
        self.assertEqual([x["signature"][:5] for x in self.rpc.sigs("HUB_2", before="sig23_c_to_own_cold")],
                         ["sig22", "sig20", "sig02", "sig01"])

    def test_rate_limit_error_is_retried(self):
        answers = [json.dumps({"error": {"code": 429, "message": "Too many requests"}}),
                   json.dumps({"result": {"ok": 1}})]
        slept = []
        rpc = Rpc(cache_dir=os.path.join(self.tmp, "c2"), rps=0, transport=lambda u, b: answers.pop(0),
                  sleep=slept.append)
        self.assertEqual(rpc.call("getX", [])["result"], {"ok": 1})
        self.assertEqual(slept, [5])

    def test_long_term_storage_hiccup_is_retried(self):
        answers = [json.dumps({"error": {"code": -32019, "message": "Failed to query long-term storage; please try again"}}),
                   json.dumps({"result": []})]
        rpc = Rpc(cache_dir=os.path.join(self.tmp, "c5"), rps=0, transport=lambda u, b: answers.pop(0),
                  sleep=lambda s: None)
        self.assertEqual(rpc.call("getSignaturesForAddress", ["A", {}])["result"], [])

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
        self.assertIn("relay of 187.4416 SOL from sig28_relay2_to_relay3: 4 hop(s) in 9 s", out)

    def test_html_flag_and_report_command(self):
        with tempfile.TemporaryDirectory() as d:
            quiet = lambda: (contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()))
            page = os.path.join(d, "demo.html")
            o, e = quiet()
            with o, e:
                main(["--html", page, "demo"])
            with open(page) as f:
                self.assertIn("solscan.io/tx/sig29_relay3_to_deposit", f.read())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                main(["--json", "demo"])
            js = os.path.join(d, "demo.json")
            with open(js, "w") as f:
                f.write(buf.getvalue())
            out = os.path.join(d, "case.html")
            o, e = quiet()
            with o, e:
                main(["--labels", os.path.join(d, "none.json"), "report", js, "-o", out, "--title", "Case <1>"])
            with open(out) as f:
                html = f.read()
            self.assertIn("<title>Case &lt;1&gt;</title>", html)
            self.assertEqual(html.count('<section id="s'), 10)

    def test_sinks_needs_two_wallets(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            main(["sinks", "ONLY_ONE"])


if __name__ == "__main__":
    unittest.main()
