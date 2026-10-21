"""The full history of one address as a ledger, and summaries of it: counterparties and stablecoin flows."""
import collections
import json
import os

from .labels import name
from .parse import STABLES, WSOL, counterparty, is_trade, summary


def build(rpc, addr, path, cap=20000, progress=None):
    """Write one JSON line per successful transaction of `addr`, oldest first. Resumable: signatures already in
    `path` are skipped, so an interrupted run continues where it stopped."""
    done = set()
    if os.path.exists(path):
        with open(path) as f:
            done = {json.loads(line)["sig"] for line in f if line.strip()}
    ss = [x for x in rpc.all_sigs(addr, cap=cap)[::-1] if x.get("err") is None]
    added = 0
    with open(path, "a") as f:
        for i, x in enumerate(ss):
            if x["signature"] in done:
                continue
            t = rpc.tx(x["signature"])
            if not t:
                continue
            u = summary(t)
            toks = {m: v for (o, m), v in u["tok"].items() if o == addr and m != WSOL}
            sol = u["sol"].get(addr, 0.0) + u["tok"].get((addr, WSOL), 0.0)
            cp = counterparty(u, addr, sol) if sol else None
            f.write(json.dumps({"sig": x["signature"], "t": u["time"], "slot": u["slot"], "sol": round(sol, 9),
                                "cp": cp, "cp_sol": round(u["sol"].get(cp, 0.0), 9) if cp else 0.0,
                                "tok": {m: round(v, 9) for m, v in toks.items()}, "signers": u["signers"],
                                "ix": u["ix"][:8], "progs": u["progs"][:8]}) + "\n")
            added += 1
            if progress and i % 250 == 0:
                f.flush()
                progress(i, len(ss))
    return {"signatures": len(ss), "already": len(done), "added": added}


def read(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def summarize(rows, labels, min_sol=0.01):
    """Split the ledger into trades, dust and transfers; total transfers per counterparty in both directions."""
    by = collections.defaultdict(lambda: {"out": 0.0, "in": 0.0, "n_out": 0, "n_in": 0, "first": None, "last": None})
    kinds = collections.Counter()
    trade_sol = 0.0
    for r in rows:
        if is_trade({"ix": r["ix"]}):
            kinds["trades"] += 1
            trade_sol += r["sol"]
            continue
        if abs(r["sol"]) < min_sol:
            kinds["dust or token-only"] += 1
            continue
        kinds["transfers"] += 1
        b = by[r["cp"] or "?"]
        if r["sol"] < 0:
            b["out"] += -r["sol"]
            b["n_out"] += 1
        else:
            b["in"] += r["sol"]
            b["n_in"] += 1
        b["first"] = b["first"] or r["t"]
        b["last"] = r["t"]
    parties = [{"addr": a, "label": name(labels, a), **{k: (round(v, 6) if isinstance(v, float) else v)
                                                        for k, v in b.items()}} for a, b in by.items()]
    return {"rows": len(rows), "from": rows[0]["t"] if rows else None, "to": rows[-1]["t"] if rows else None,
            "kinds": dict(kinds), "trade_net_sol": round(trade_sol, 6),
            "sent_sol": round(sum(p["out"] for p in parties), 6), "received_sol": round(sum(p["in"] for p in parties), 6),
            "sent_to": sorted([p for p in parties if p["out"] > 0], key=lambda p: -p["out"]),
            "received_from": sorted([p for p in parties if p["in"] > 0], key=lambda p: -p["in"]),
            "exchanges": [p for p in parties if p["label"]]}


def stables(rpc, addr, rows, labels, min_amount=1.0):
    """USDC and USDT moved by `addr`: per counterparty, transfers only (swaps are counted apart)."""
    by = collections.defaultdict(lambda: {"out": 0.0, "in": 0.0, "n_out": 0, "n_in": 0, "last": 0})
    swapped = collections.Counter()
    for r in rows:
        if not any(m in STABLES for m in r["tok"]):
            continue
        t = rpc.tx(r["sig"])
        if not t:
            continue
        u = summary(t)
        for mint, coin in STABLES.items():
            mine = u["tok"].get((addr, mint), 0.0)
            if abs(mine) < min_amount:
                continue
            if is_trade(u):
                swapped[coin] += mine
                continue
            others = {o: v for (o, m), v in u["tok"].items() if m == mint and o != addr}
            if not others:
                continue
            cp = max(others, key=others.get) if mine < 0 else min(others, key=others.get)
            b = by[(cp, coin)]
            key = "out" if mine < 0 else "in"
            b[key] += abs(mine)
            b["n_" + key] += 1
            b["last"] = max(b["last"], r["t"] or 0)
    flows = [{"addr": cp, "coin": coin, "label": name(labels, cp), **b} for (cp, coin), b in by.items()]
    return {"addr": addr, "swapped": dict(swapped),
            "flows": sorted(flows, key=lambda x: -(x["out"] + x["in"]))}
