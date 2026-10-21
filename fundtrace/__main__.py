"""fundtrace — follow money on Solana from a wallet to the exchange where it came from or where it left.

    python3 -m fundtrace demo                       # offline, on a synthetic chain
    python3 -m fundtrace labels                     # download exchange labels into labels.json
    python3 -m fundtrace back ADDR                  # where did the money come from?
    python3 -m fundtrace forward ADDR               # where did it go?
    python3 -m fundtrace deposit ADDR [ADDR ...]    # is it an exchange deposit address?
    python3 -m fundtrace funding ADDR --before SIG  # who paid just before this transaction?
    python3 -m fundtrace ledger ADDR                # full history -> ledger_<ADDR>.jsonl (resumable)
    python3 -m fundtrace summary ledger_<ADDR>.jsonl
    python3 -m fundtrace stables ADDR               # USDC / USDT counterparties, from the ledger
"""
import argparse
import datetime
import json
import os
import sys
import tempfile

from . import labels as lab
from . import ledger, trace
from .rpc import Rpc


def ts(x):
    return datetime.datetime.fromtimestamp(x, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M") if x else "?"


def _w(addrs):
    return max([len(a or "") for a in addrs] + [8])


def show_back(r):
    print(f"back from {r['start']}: {len(r['chain']) - 1} hop(s), stopped: {r['stop']}")
    w = _w(c["addr"] for c in r["chain"])
    for c in r["chain"]:
        tag = f"  [{c['label']}]" if c.get("label") else ""
        if c.get("in"):
            i = c["in"]
            print(f"  {c['addr']:<{w}} {c['n']:>3} tx  <- {i['sol']:,.4f} SOL from {c['from']}  {ts(i['t'])}  {i['sig']}{tag}")
        else:
            print(f"  {c['addr']:<{w}} {str(c.get('n', '')):>3}{' tx' if 'n' in c else '   '}{tag}")


def show_forward(r):
    print(f"forward from {r['start']}: {len(r['chain']) - 1} hop(s), stopped: {r['stop']}")
    w = _w(c["addr"] for c in r["chain"])
    for c in r["chain"]:
        tag = f"  [{c['label']}]" if c.get("label") else ""
        if c.get("out"):
            o = c["out"]
            print(f"  {c['addr']:<{w}} {c['n']:>3} tx  -> {o['sol']:,.4f} SOL to {o['to']}  {ts(o['t'])}  {o['sig']}{tag}")
        else:
            print(f"  {c['addr']:<{w}} {str(c.get('n', '')):>3}{' tx' if 'n' in c else '   '}{tag}")


def show_deposit(r):
    if r["is_deposit"]:
        to = ", ".join(f"{k} x{v}" for k, v in r["sweeps"].items())
        sol = ", ".join(f"{k}: {v:,.4f} SOL" for k, v in r["sol_to_exchange"].items())
        print(f"{r['addr']}: DEPOSIT ADDRESS — sweeps to {to}; {sol} (last {r['n']} tx)")
    else:
        print(f"{r['addr']}: no sweeps into a labelled exchange wallet in the last {r['n']} tx")


def show_funding(r):
    print(f"funding of {r['addr']} before {r['before']}:")
    for x in r["funding"] or [{"none": 1}]:
        if "none" in x:
            print("  no incoming transfers above the threshold")
            continue
        flag = "  MEMO, signers: " + ", ".join(x["signers"]) if x["memo"] else ""
        print(f"  <- {x['sol']:,.4f} SOL from {x['from']}  {ts(x['t'])}  {x['sig']}{flag}")


def show_summary(s, top=15):
    print(f"{s['rows']} transactions, {ts(s['from'])} -> {ts(s['to'])}: {s['kinds']}")
    print(f"trading net {s['trade_net_sol']:+,.4f} SOL; transfers sent {s['sent_sol']:,.4f} SOL, "
          f"received {s['received_sol']:,.4f} SOL")
    w = _w(p["addr"] for p in s["sent_to"][:top] + s["received_from"][:top])
    for title, rows, key in (("SENT TO", s["sent_to"], "out"), ("RECEIVED FROM", s["received_from"], "in")):
        print(f"{title}:")
        for p in rows[:top]:
            tag = f"  [{p['label']}]" if p["label"] else ""
            print(f"  {p['addr']:<{w}} {p[key]:>14,.4f} SOL  x{p['n_' + key]:<4} {ts(p['first'])} .. {ts(p['last'])}{tag}")
    if s["exchanges"]:
        print("labelled exchange wallets met directly:", ", ".join(p["label"] for p in s["exchanges"]))


def show_stables(r):
    print(f"stablecoins of {r['addr']} (swaps apart: {r['swapped'] or 'none'}):")
    w = _w(f["addr"] for f in r["flows"])
    for f in r["flows"]:
        tag = f"  [{f['label']}]" if f["label"] else ""
        print(f"  {f['addr']:<{w}} {f['coin']}  sent {f['out']:>12,.2f} x{f['n_out']:<3} "
              f"received {f['in']:>12,.2f} x{f['n_in']:<3} last {ts(f['last'])}{tag}")


def demo():
    from .demo import LABELS, Chain
    with tempfile.TemporaryDirectory() as d:
        rpc = Rpc(cache_dir=os.path.join(d, "cache"), rps=0, transport=Chain().transport)
        print("Synthetic chain — every address and amount is made up (see fundtrace/demo.py).\n")
        show_back(trace.back(rpc, "FRESH_W", LABELS))
        print()
        show_forward(trace.forward(rpc, "FRESH_W", LABELS))
        print()
        show_deposit(trace.deposit(rpc, "DEPOSIT_D1", LABELS))
        show_deposit(trace.deposit(rpc, "COLLECTOR", LABELS))
        print()
        show_funding(trace.funding(rpc, "FRESH_W", "sig05_buy"))
        print()
        path = os.path.join(d, "ledger_FRESH_W.jsonl")
        ledger.build(rpc, "FRESH_W", path)
        show_summary(ledger.summarize(ledger.read(path), LABELS))
        print()
        cpath = os.path.join(d, "ledger_COLLECTOR.jsonl")
        ledger.build(rpc, "COLLECTOR", cpath)
        show_stables(ledger.stables(rpc, "COLLECTOR", ledger.read(cpath), LABELS))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fundtrace", description=__doc__.split("\n")[0])
    ap.add_argument("--rpc", help="RPC URL (default: $FUNDTRACE_RPC_URL or the public mainnet endpoint)")
    ap.add_argument("--rps", type=float, help="requests per second, shared by all processes (default 1)")
    ap.add_argument("--cache", help="cache directory (default .fundtrace/cache)")
    ap.add_argument("--labels", default="labels.json", help="exchange labels file (default labels.json)")
    ap.add_argument("--fresh", action="store_true", help="re-read signature lists instead of using the cache")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="offline demo on a synthetic chain")
    sub.add_parser("labels", help="download exchange labels from Dune Spellbook into --labels")
    for name, helptext in (("back", "walk funding backwards"), ("forward", "follow the largest outgoing transfers")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("addr")
        p.add_argument("--max-hops", type=int, default=40)
        p.add_argument("--busy", type=int, default=50, help="stop at addresses with this many transactions")
        p.add_argument("--min-sol", type=float, default=0.01)
    p = sub.add_parser("deposit", help="is it an exchange deposit address?")
    p.add_argument("addr", nargs="+")
    p.add_argument("-n", type=int, default=100, help="how many recent transactions to read")
    p = sub.add_parser("funding", help="incoming transfers just before a transaction")
    p.add_argument("addr")
    p.add_argument("--before", required=True, help="signature of the transaction")
    p.add_argument("-n", type=int, default=12)
    p.add_argument("--min-sol", type=float, default=1.0)
    p = sub.add_parser("ledger", help="full history of an address into a JSONL ledger (resumable)")
    p.add_argument("addr")
    p.add_argument("--out")
    p.add_argument("--cap", type=int, default=20000, help="at most this many signatures")
    p = sub.add_parser("summary", help="counterparties of a ledger file")
    p.add_argument("path")
    p.add_argument("--top", type=int, default=15)
    p = sub.add_parser("stables", help="USDC/USDT counterparties of an address, from its ledger")
    p.add_argument("addr")
    p.add_argument("--ledger")
    a = ap.parse_args(argv)

    if a.cmd == "demo":
        return demo()
    if a.cmd == "labels":
        n = lab.fetch(a.labels)
        print(f"{n} exchange addresses written to {a.labels} (Dune Spellbook, Business Source License 1.1)")
        return
    labels = lab.load(a.labels)
    if not labels and a.cmd in ("back", "forward", "deposit"):
        print(f"note: no labels in {a.labels} — run `python3 -m fundtrace labels` to stop at exchanges", file=sys.stderr)
    rpc = Rpc(url=a.rpc, cache_dir=a.cache, rps=a.rps, fresh=a.fresh)
    out = (lambda r, f: print(json.dumps(r, indent=1))) if a.json else (lambda r, f: f(r))

    if a.cmd == "back":
        out(trace.back(rpc, a.addr, labels, a.max_hops, a.busy, a.min_sol), show_back)
    elif a.cmd == "forward":
        out(trace.forward(rpc, a.addr, labels, a.max_hops, a.busy, a.min_sol), show_forward)
    elif a.cmd == "deposit":
        for x in a.addr:
            out(trace.deposit(rpc, x, labels, a.n), show_deposit)
    elif a.cmd == "funding":
        out(trace.funding(rpc, a.addr, a.before, a.n, a.min_sol), show_funding)
    elif a.cmd == "ledger":
        path = a.out or f"ledger_{a.addr}.jsonl"
        r = ledger.build(rpc, a.addr, path, a.cap,
                         progress=lambda i, n: print(f"{i}/{n} {rpc.stats}", file=sys.stderr, flush=True))
        print(f"{path}: {r['added']} added, {r['already']} already there, {r['signatures']} signatures; {rpc.stats}")
    elif a.cmd == "summary":
        s = ledger.summarize(ledger.read(a.path), labels)
        out(s, lambda s: show_summary(s, a.top))
    elif a.cmd == "stables":
        out(ledger.stables(rpc, a.addr, ledger.read(a.ledger or f"ledger_{a.addr}.jsonl"), labels), show_stables)
    print(f"rpc: {rpc.stats}", file=sys.stderr)


if __name__ == "__main__":
    main()
