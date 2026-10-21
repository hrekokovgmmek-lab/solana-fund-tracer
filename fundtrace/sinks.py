"""Where the money of a group of wallets converges.

Funding is easy to disguise: a fresh wallet can be funded from an exchange withdrawal, and that links nothing. Where
the profit goes afterwards is harder to hide, because the operator wants it back. Give `sinks` the wallets of one
suspected group (for example everyone who sold a token in its first minutes) and it returns the addresses that
received money from several of them. One shared sink is a lead; several wallets sending to the same hub after a sell
is the pattern that exposed the PumpSwap wash-trading group described in the README.
"""
from .labels import name
from .parse import WSOL, is_trade, summary, sol_with_wsol, token_accounts


def outflows(rpc, addr, n=100, min_sol=0.01, since=None):
    """Transfers that left `addr` (not trades): [(recipient, sol, time, signature)]. A transaction that pays several
    accounts gives one row per recipient; wrapped SOL counts as SOL."""
    out = []
    for x in rpc.sigs(addr, limit=n):
        if x.get("err") or (since and (x.get("blockTime") or 0) < since):
            continue
        t = rpc.tx(x["signature"])
        if not t:
            continue
        u = summary(t)
        if is_trade(u) or sol_with_wsol(u, addr) >= -min_sol:
            continue
        skip = token_accounts(t)
        gains = {k: v for k, v in u["sol"].items() if k not in skip}
        for (owner, mint), v in u["tok"].items():
            if mint == WSOL and owner and owner != addr:
                gains[owner] = gains.get(owner, 0.0) + v
        for who, v in gains.items():
            if who != addr and v >= min_sol:
                out.append((who, round(v, 9), u["time"], x["signature"]))
    return out


def sinks(rpc, wallets, labels, n=100, min_sol=0.01, min_senders=2, depth=1, busy=50, since=None):
    """Addresses that received money from at least `min_senders` of `wallets`.

    depth 2 also follows money through one-off intermediaries: a recipient that got money from a single wallet of the
    group and has fewer than `busy` transactions is walked one more hop, and what it forwards is credited to the
    wallet the money came from. Each row lists its transfers in `legs`, the hops through intermediaries included."""
    group = list(dict.fromkeys(wallets))
    seen_tx = set()
    flows = {}                                   # recipient -> {"from": {origin: sol}, "n", "first", "last", "via"}

    def credit(origin, who, sol, t, sig, via=None):
        if who in group or (sig, who) in seen_tx:
            return
        seen_tx.add((sig, who))
        f = flows.setdefault(who, {"from": {}, "n": 0, "first": None, "last": None, "via": set(), "sigs": [],
                                   "legs": []})
        f["from"][origin] = round(f["from"].get(origin, 0.0) + sol, 9)
        f["n"] += 1
        if t:
            f["first"] = t if f["first"] is None else min(f["first"], t)
            f["last"] = t if f["last"] is None else max(f["last"], t)
        if via:
            f["via"].add(via)
        if len(f["sigs"]) < 3:
            f["sigs"].append(sig)
        f["legs"].append({"from": via or origin, "to": who, "sol": sol, "t": t, "sig": sig})

    first_hop = {}
    for w in group:
        for who, sol, t, sig in outflows(rpc, w, n, min_sol, since):
            credit(w, who, sol, t, sig)
            first_hop.setdefault(who, set()).add(w)

    if depth >= 2:
        for mid, origins in list(first_hop.items()):
            if len(origins) != 1 or mid in labels:
                continue
            if len(rpc.sigs(mid, limit=busy)) >= busy:
                continue
            origin = next(iter(origins))
            for who, sol, t, sig in outflows(rpc, mid, n, min_sol, since):
                if who != origin:
                    credit(origin, who, sol, t, sig, via=mid)

    rows = []
    for who, f in flows.items():
        if len(f["from"]) < min_senders:
            continue
        rows.append({"addr": who, "label": name(labels, who), "senders": len(f["from"]), "of": len(group),
                     "sol": round(sum(f["from"].values()), 6), "transfers": f["n"], "first": f["first"],
                     "last": f["last"], "via": sorted(f["via"]), "from": f["from"], "sigs": f["sigs"],
                     "legs": f["legs"] + [g for v in sorted(f["via"]) for g in flows[v]["legs"]]})
    rows.sort(key=lambda r: (-r["senders"], -r["sol"]))
    return {"wallets": group, "depth": depth, "min_senders": min_senders, "sinks": rows}
