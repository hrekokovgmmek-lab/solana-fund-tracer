"""Follow money between wallets: back to where it came from, forward to where it left, and exchange deposits."""
from .labels import name
from .parse import WSOL, counterparty, is_trade, summary, sol_with_wsol


def funder(rpc, addr, busy=50, min_sol=0.01):
    """The earliest incoming SOL transfer of a fresh address: (source, n_signatures, details).

    Addresses with `busy` or more transactions are not walked: they are services, exchanges or hubs, and their first
    incoming transfer says nothing about the money that passed through later."""
    s = rpc.sigs(addr, limit=busy)
    if len(s) >= busy:
        return None, len(s), None
    for x in reversed(s):
        if x.get("err"):
            continue
        t = rpc.tx(x["signature"])
        if not t:
            continue
        u = summary(t)
        d = u["sol"].get(addr, 0.0)
        if d > min_sol:
            src = counterparty(u, addr, d)
            return src, len(s), {"sol": round(d, 6), "t": u["time"], "sig": x["signature"], "signers": u["signers"]}
    return None, len(s), None


def back(rpc, start, labels, max_hops=40, busy=50, min_sol=0.01):
    """Walk funding backwards until an exchange label, a busy address, a loop or a dead end."""
    chain, cur, seen = [], start, {start}
    for _ in range(max_hops):
        src, n, info = funder(rpc, cur, busy, min_sol)
        chain.append({"addr": cur, "n": n, "label": name(labels, cur), "in": info, "from": src})
        if src is None or src in labels or n >= busy or src in seen:
            break
        seen.add(src)
        cur = src
    last = chain[-1]
    if last["from"] and last["from"] in labels:
        chain.append({"addr": last["from"], "label": name(labels, last["from"])})
    return {"start": start, "end": chain[-1]["addr"], "stop": _stop(chain, labels, busy), "chain": chain}


def biggest_out(rpc, addr, busy=50, min_sol=0.01):
    """The largest outgoing SOL transfer (wrapped SOL counted) of a fresh address, and its signature count."""
    s = rpc.sigs(addr, limit=busy)
    if len(s) >= busy:
        return None, len(s)
    best = None
    for x in s:
        if x.get("err"):
            continue
        t = rpc.tx(x["signature"])
        if not t:
            continue
        u = summary(t)
        if is_trade(u):
            continue
        d = sol_with_wsol(u, addr)
        if d < -min_sol:
            dst = counterparty(u, addr, d)
            if dst and (best is None or -d > best["sol"]):
                best = {"to": dst, "sol": round(-d, 6), "sig": x["signature"], "t": u["time"], "progs": u["progs"][:5]}
    return best, len(s)


def forward(rpc, start, labels, max_hops=40, busy=50, min_sol=0.01):
    """Follow the largest outgoing transfer until an exchange label, a busy address, a loop or a dead end."""
    chain, cur, seen = [], start, {start}
    for _ in range(max_hops):
        if cur in labels:
            chain.append({"addr": cur, "label": name(labels, cur)})
            break
        best, n = biggest_out(rpc, cur, busy, min_sol)
        chain.append({"addr": cur, "n": n, "out": best})
        if best is None or best["to"] in seen:
            break
        seen.add(best["to"])
        cur = best["to"]
    return {"start": start, "end": chain[-1]["addr"], "stop": _stop(chain, labels, busy), "chain": chain}


def _stop(chain, labels, busy):
    last = chain[-1]
    if last["addr"] in labels:
        return "exchange"
    if last.get("n", 0) >= busy:
        return "busy address"
    nxt = last.get("from") or (last.get("out") or {}).get("to")
    if nxt and any(nxt == c["addr"] for c in chain):
        return "loop"
    return "dead end"


def deposit(rpc, addr, labels, n=100):
    """Is this an exchange deposit address? Look for money it sends straight into a labelled exchange wallet.

    Exchanges give each customer a deposit address and sweep it into a hot wallet. A sweep is a transaction in
    which `addr` loses SOL or tokens and a labelled wallet gains them."""
    sweeps, sol_to = {}, {}
    s = rpc.sigs(addr, limit=n)
    for x in s:
        if x.get("err"):
            continue
        t = rpc.tx(x["signature"])
        if not t:
            continue
        u = summary(t)
        if sol_with_wsol(u, addr) < 0:
            for k, v in u["sol"].items():
                if k in labels and v > 0:
                    nm = name(labels, k)
                    sweeps[nm] = sweeps.get(nm, 0) + 1
                    sol_to[nm] = round(sol_to.get(nm, 0.0) + v, 6)
        mine = sum(v for (o, m), v in u["tok"].items() if o == addr and m != WSOL)
        if mine < 0:
            for (o, m), v in u["tok"].items():
                if o in labels and v > 0 and m != WSOL:
                    nm = name(labels, o) + " (token)"
                    sweeps[nm] = sweeps.get(nm, 0) + 1
    return {"addr": addr, "n": len(s), "is_deposit": bool(sweeps), "sweeps": sweeps, "sol_to_exchange": sol_to}


def funding(rpc, addr, before, n=12, min_sol=1.0):
    """Where the money came from just before a given transaction (a large buy, a payment): incoming SOL transfers
    among the `n` earlier transactions of `addr`, trades excluded. Memo usage is flagged: one-off co-signers and
    memos are a common way to hide who paid whom."""
    prev = rpc.call("getSignaturesForAddress", [addr, {"before": before, "limit": n}]).get("result") or []
    out = []
    for x in prev:
        if x.get("err"):
            continue
        t = rpc.tx(x["signature"])
        if not t:
            continue
        u = summary(t)
        d = u["sol"].get(addr, 0.0)
        if d > min_sol and not is_trade(u):
            out.append({"from": counterparty(u, addr, d), "sol": round(d, 6), "t": u["time"], "sig": x["signature"],
                        "memo": "Memo" in u["progs"], "signers": u["signers"]})
    return {"addr": addr, "before": before, "funding": out}
