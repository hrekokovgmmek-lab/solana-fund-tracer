"""fundtrace — follow money on Solana from a wallet to the exchange where it came from or where it left.

    python3 -m fundtrace demo                       # offline, on a synthetic chain
    python3 -m fundtrace labels                     # download exchange labels into labels.json
    python3 -m fundtrace back ADDR                  # where did the money come from?
    python3 -m fundtrace forward ADDR               # where did it go?
    python3 -m fundtrace deposit ADDR [ADDR ...]    # is it an exchange deposit address?
    python3 -m fundtrace funding ADDR --before SIG  # who paid just before this transaction?
    python3 -m fundtrace sinks ADDR ADDR ...        # where does the money of a group converge?
    python3 -m fundtrace relay SIG                  # one exact amount passed from wallet to wallet within seconds
    python3 -m fundtrace report OUT.json ...        # one HTML page: a diagram and a Solscan link per transaction
    python3 -m fundtrace ledger ADDR                # full history -> ledger_<ADDR>.jsonl (resumable)
    python3 -m fundtrace summary ledger_<ADDR>.jsonl
    python3 -m fundtrace stables ADDR               # USDC / USDT counterparties, from the ledger
"""
import argparse
import contextlib
import datetime
import io
import json
import os
import sys
import tempfile

from . import __version__
from . import labels as lab
from . import ledger, relay, report, sinks, trace
from .rpc import Rpc


def ts(x):
    return datetime.datetime.fromtimestamp(x, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M") if x else "?"


def tss(x):
    return datetime.datetime.fromtimestamp(x, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if x else "?"


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


def show_sinks(r):
    found = r["sinks"]
    print(f"sinks of {len(r['wallets'])} wallets (depth {r['depth']}): {len(found)} address(es) got money from "
          f"{r['min_senders']}+ of them")
    w = _w(x["addr"] for x in found)
    for x in found:
        tag = f"  [{x['label']}]" if x["label"] else ""
        via = f"  via {', '.join(x['via'])}" if x["via"] else ""
        print(f"  {x['addr']:<{w}} {x['senders']}/{x['of']} wallets {x['sol']:>12,.4f} SOL  x{x['transfers']:<3} "
              f"{ts(x['first'])} .. {ts(x['last'])}{via}{tag}")
    if not found:
        print("  no shared destination: these wallets do not send their money to the same place")


def show_relay(r):
    h = r["hops"]
    print(f"relay of {report.fmt(r['amount'])} SOL from {r['start']}: {len(h)} hop(s) in {r['seconds']} s, "
          f"each within {r['window']:g} s of the one before")
    if r["round"]:
        print("  note: a round amount, so a match can be a coincidence")
    w = _w([x["from"] for x in h] + [x["to"] for x in h])
    for x in h:
        dt = f"+{x['dt']} s" if x["dt"] is not None else ""
        tag = f"  [{r['labels'][x['to']]}]" if x["to"] in r["labels"] else ""
        print(f"  {x['from']:<{w}} -> {x['to']:<{w}} {report.fmt(x['sol']):>15} SOL  {tss(x['t'])} {dt:>7}  "
              f"{x['sig']}{tag}")
    why = {"no match": f"no transfer of this amount within {r['window']:g} s"}
    print(f"  back: stopped at {r['first']}: {why.get(r['stop_back'], r['stop_back'])}")
    print(f"  forward: stopped at {r['last']}: {why.get(r['stop_forward'], r['stop_forward'])}")


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


SHOW = {"back": show_back, "forward": show_forward, "deposit": show_deposit, "funding": show_funding,
        "sinks": show_sinks, "relay": show_relay, "summary": show_summary, "stables": show_stables}


def as_text(r):
    """What the command printed for this result, for the HTML report."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        SHOW[report.kind(r)](r)
    return buf.getvalue()


def write_report(path, results, labels, title):
    with open(path, "w", encoding="utf-8") as f:
        f.write(report.render(results, labels, title, text=as_text, version=__version__))
    print(f"report: {path} ({len(results)} result(s))", file=sys.stderr)


def demo(out):
    from .demo import LABELS, Chain
    with tempfile.TemporaryDirectory() as d:
        rpc = Rpc(cache_dir=os.path.join(d, "cache"), rps=0, transport=Chain().transport)
        out(trace.back(rpc, "FRESH_W", LABELS), show_back)
        print()
        out(trace.forward(rpc, "FRESH_W", LABELS), show_forward)
        print()
        out(trace.deposit(rpc, "DEPOSIT_D1", LABELS), show_deposit)
        out(trace.deposit(rpc, "COLLECTOR", LABELS), show_deposit)
        print()
        out(trace.funding(rpc, "FRESH_W", "sig05_buy"), show_funding)
        print()
        group = ["SELLER_A", "SELLER_B", "SELLER_C", "SELLER_D"]
        out(sinks.sinks(rpc, group, LABELS), show_sinks)
        out(sinks.sinks(rpc, group, LABELS, depth=2), show_sinks)
        print()
        out(relay.relay(rpc, relay.transfer(rpc, "sig28_relay2_to_relay3"), LABELS), show_relay)
        print()
        path = os.path.join(d, "ledger_FRESH_W.jsonl")
        ledger.build(rpc, "FRESH_W", path)
        out(ledger.summarize(ledger.read(path), LABELS), show_summary)
        print()
        cpath = os.path.join(d, "ledger_COLLECTOR.jsonl")
        ledger.build(rpc, "COLLECTOR", cpath)
        out(ledger.stables(rpc, "COLLECTOR", ledger.read(cpath), LABELS), show_stables)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fundtrace", description=__doc__.split("\n")[0])
    ap.add_argument("--rpc", help="RPC URL (default: $FUNDTRACE_RPC_URL or the public mainnet endpoint)")
    ap.add_argument("--rps", type=float, help="requests per second, shared by all processes (default 1)")
    ap.add_argument("--cache", help="cache directory (default .fundtrace/cache)")
    ap.add_argument("--labels", default="labels.json", help="exchange labels file (default labels.json)")
    ap.add_argument("--fresh", action="store_true", help="re-read signature lists instead of using the cache")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    ap.add_argument("--html", metavar="PATH", help="also write the result as an HTML page with Solscan links")
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
    p = sub.add_parser("sinks", help="addresses that received money from several wallets of a group")
    p.add_argument("addr", nargs="*")
    p.add_argument("--file", help="a file with one address per line")
    p.add_argument("--depth", type=int, default=1, choices=(1, 2), help="2 also follows one-off intermediaries")
    p.add_argument("--min-senders", type=int, default=2)
    p.add_argument("-n", type=int, default=100, help="recent transactions read per wallet")
    p.add_argument("--min-sol", type=float, default=0.01)
    p.add_argument("--since", help="only transfers at or after this time (ISO, e.g. 2026-09-14T00:00:00Z)")
    p.add_argument("--busy", type=int, default=50)
    p = sub.add_parser("relay", help="follow one exact amount passed from wallet to wallet within seconds")
    p.add_argument("start", help="signature of one transfer (an address, with --amount)")
    p.add_argument("--amount", type=float, help="SOL: START is then an address and its latest transfer of this "
                                                "amount is where the walk starts")
    p.add_argument("--window", type=float, default=60, help="most seconds between two hops (default 60)")
    p.add_argument("--tol", type=float, default=0.001, help="most SOL two hops may differ by (default 0.001)")
    p.add_argument("--max-hops", type=int, default=20, help="per direction")
    p.add_argument("--reads", type=int, default=30, help="most transactions read per wallet inside the window; "
                                                          "a busier wallet ends the walk (default 30)")
    p.add_argument("-n", type=int, default=100, help="with --amount: recent transactions searched")
    p = sub.add_parser("report", help="one HTML page from saved --json output: a diagram and Solscan links")
    p.add_argument("path", nargs="+", help="files written with --json (several results in a file are fine)")
    p.add_argument("-o", "--out", default="report.html")
    p.add_argument("--title", default="Fund trace")
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
    results = []

    def out(r, show):
        results.append(r)
        if a.json:
            print(json.dumps(r, indent=1))
        else:
            show(r)

    if a.cmd == "demo":
        from .demo import LABELS
        if not a.json:
            print("Synthetic chain — every address and amount is made up (see fundtrace/demo.py).\n")
        demo(out)
        if a.html:
            write_report(a.html, results, LABELS, "fundtrace demo — synthetic chain")
        return
    if a.cmd == "labels":
        n = lab.fetch(a.labels)
        print(f"{n} exchange addresses written to {a.labels} (Dune Spellbook, Business Source License 1.1)")
        return
    labels = lab.load(a.labels)
    if a.cmd == "report":
        write_report(a.out, report.load(a.path), labels, a.title)
        return
    if not labels and a.cmd in ("back", "forward", "deposit"):
        print(f"note: no labels in {a.labels} — run `python3 -m fundtrace labels` to stop at exchanges", file=sys.stderr)
    rpc = Rpc(url=a.rpc, cache_dir=a.cache, rps=a.rps, fresh=a.fresh)

    if a.cmd == "back":
        out(trace.back(rpc, a.addr, labels, a.max_hops, a.busy, a.min_sol), show_back)
    elif a.cmd == "forward":
        out(trace.forward(rpc, a.addr, labels, a.max_hops, a.busy, a.min_sol), show_forward)
    elif a.cmd == "deposit":
        for x in a.addr:
            out(trace.deposit(rpc, x, labels, a.n), show_deposit)
    elif a.cmd == "funding":
        out(trace.funding(rpc, a.addr, a.before, a.n, a.min_sol), show_funding)
    elif a.cmd == "sinks":
        group = list(a.addr)
        if a.file:
            with open(a.file) as f:
                group += [line.strip() for line in f if line.strip() and not line.startswith("#")]
        if len(group) < 2:
            ap.error("sinks needs at least two wallets")
        since = datetime.datetime.fromisoformat(a.since.replace("Z", "+00:00")).timestamp() if a.since else None
        out(sinks.sinks(rpc, group, labels, a.n, a.min_sol, a.min_senders, a.depth, a.busy, since), show_sinks)
    elif a.cmd == "relay":
        try:
            start = (relay.find(rpc, a.start, a.amount, a.tol, a.n) if a.amount is not None
                     else relay.transfer(rpc, a.start))
            out(relay.relay(rpc, start, labels, a.window, a.tol, a.max_hops, a.reads), show_relay)
        except ValueError as e:
            sys.exit(f"relay: {e}")
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
    if a.html and results:
        write_report(a.html, results, labels, f"fundtrace {a.cmd}")
    print(f"rpc: {rpc.stats}", file=sys.stderr)


if __name__ == "__main__":
    main()
