"""Follow one exact amount as it is passed from wallet to wallet within seconds.

A script that moves money through its own wallets forwards the whole sum at once, so the same amount leaves each
wallet seconds after it arrived. A round sum (10 SOL) seen twice can be a coincidence; 187.4416 SOL moving four times
in nine seconds is one operator. `relay` starts from one transfer and walks both ways: forward while the recipient
sends the same amount on within `window` seconds, back while the sender received it within `window` seconds before.
"""
from .labels import name
from .parse import WSOL, is_trade, summary, token_accounts
from .rpc import RpcError

PAGE = 1000                                            # signatures per getSignaturesForAddress page


def gains(t, u):
    """SOL change per account; token accounts are left out and wrapped SOL counts for its owner."""
    skip = token_accounts(t)
    g = {k: v for k, v in u["sol"].items() if k not in skip}
    for (owner, mint), v in u["tok"].items():
        if mint == WSOL and owner:
            g[owner] = g.get(owner, 0.0) + v
    return g


def _hop(frm, to, sol, u, sig):
    return {"from": frm, "to": to, "sol": round(sol, 9), "t": u["time"], "slot": u["slot"], "sig": sig}


def _read(rpc, sig):
    t = rpc.tx(sig)
    if not t:
        return None, None
    u = summary(t)
    return (None, None) if is_trade(u) else (gains(t, u), u)


def transfer(rpc, sig):
    """The main transfer of a transaction: to the account that gained most, from the one that paid most."""
    t = rpc.tx(sig)
    if not t:
        raise ValueError(f"transaction {sig} not found")
    u = summary(t)
    if is_trade(u):
        raise ValueError(f"{sig} is a trade, not a transfer")
    g = gains(t, u)
    to, frm = max(g, key=g.get, default=None), min(g, key=g.get, default=None)
    if to is None or g[to] <= 0 or g[frm] >= 0:
        raise ValueError(f"{sig} moves no SOL")
    return _hop(frm, to, g[to], u, sig)


def sent(rpc, sig, addr, amount, tol):
    """The transfer of `amount` (± tol) that `addr` paid in this transaction, or None."""
    g, u = _read(rpc, sig)
    if not g or g.get(addr, 0.0) > -(amount - tol):
        return None
    hits = sorted((abs(v - amount), k) for k, v in g.items() if k != addr and abs(v - amount) <= tol)
    return _hop(addr, hits[0][1], g[hits[0][1]], u, sig) if hits else None


def received(rpc, sig, addr, amount, tol):
    """The transfer of `amount` (± tol) that `addr` got in this transaction, or None."""
    g, u = _read(rpc, sig)
    if not g or abs(g.get(addr, 0.0) - amount) > tol:
        return None
    frm = min(g, key=g.get)
    return _hop(frm, addr, g[addr], u, sig) if frm != addr and g[frm] < 0 else None


def find(rpc, addr, amount, tol=0.001, n=100):
    """The latest transfer of `amount` into or out of `addr` among its `n` latest transactions."""
    for x in rpc.sigs(addr, limit=n):
        if x.get("err"):
            continue
        h = sent(rpc, x["signature"], addr, amount, tol) or received(rpc, x["signature"], addr, amount, tol)
        if h:
            return h
    raise ValueError(f"no transfer of {amount} SOL into or out of {addr} in its last {n} transactions")


def _anchor(rpc, hop, window, tries=8):
    """A signature from the first block found after the window. `before` accepts any transaction, so the history of
    a busy address can be read back from that point instead of from today."""
    slot = hop["slot"] + max(5, int(window / 0.2))      # slots last 0.2-0.4 s: this lands past the window
    for _ in range(tries):
        try:
            b = rpc.call("getBlock", [slot, {"transactionDetails": "signatures", "rewards": False,
                                             "maxSupportedTransactionVersion": 1}]).get("result")
        except RpcError:                                 # a skipped slot, or one the node does not have
            slot += 1
            continue
        if not b or not b.get("signatures"):
            slot += 1
        elif (b.get("blockTime") or 0) < hop["t"] + window:
            slot += slot - hop["slot"]
        else:
            return b["signatures"][0]
    return None


def step_forward(rpc, hop, tol, window, reads=30):
    """(the hop that sent the amount on from hop["to"], None) or (None, why the walk stops). Transactions are read
    nearest first, at most `reads` of them: a hub can do hundreds in a minute, and each one costs a request."""
    later = rpc.sigs(hop["to"], limit=PAGE, until=hop["sig"])
    if len(later) >= PAGE:                              # busy: the page holds today's signatures, not the window's
        anchor = _anchor(rpc, hop, window)
        later = rpc.sigs(hop["to"], before=anchor, limit=PAGE, until=hop["sig"]) if anchor else []
        if not anchor or len(later) >= PAGE:
            return None, "busy address"
    for x in reversed(later):
        bt = x.get("blockTime")
        if x.get("err") or bt is None or bt < hop["t"]:
            continue
        if bt > hop["t"] + window:
            break
        if reads <= 0:
            return None, "busy address"
        reads -= 1
        h = sent(rpc, x["signature"], hop["to"], hop["sol"], tol)
        if h:
            return h, None
    return None, "no match"


def step_back(rpc, hop, tol, window, reads=30):
    """(the hop that brought the amount to hop["from"], None) or (None, why the walk stops). Nearest first, at most
    `reads` transactions read, as in `step_forward`."""
    earlier = rpc.sigs(hop["from"], before=hop["sig"], limit=PAGE)
    for x in earlier:
        bt = x.get("blockTime")
        if x.get("err") or bt is None or bt > hop["t"]:
            continue
        if bt < hop["t"] - window:
            return None, "no match"
        if reads <= 0:
            return None, "busy address"
        reads -= 1
        h = received(rpc, x["signature"], hop["from"], hop["sol"], tol)
        if h:
            return h, None
    return None, "busy address" if len(earlier) >= PAGE else "no match"


def is_round(sol, tol=0.001):
    """A multiple of 0.1 SOL, give or take the fees taken on the way: people type such sums, so two equal ones prove
    little."""
    return abs(sol - round(sol, 1)) <= tol


def relay(rpc, start, labels, window=60, tol=0.001, max_hops=20, reads=30):
    """Walk both ways from `start` (a hop from `transfer` or `find`). Each hop may differ from the one before by `tol`
    SOL (a fee taken on the way) and must come within `window` seconds of it. A wallet with more than `reads`
    transactions inside the window is not read through: the walk stops there as "busy address"."""
    if start["t"] is None:
        raise ValueError(f"{start['sig']} has no block time")
    hops, seen = [start], {start["from"], start["to"]}

    def walk(step, end, grow):
        cur = start
        for _ in range(max_hops):
            if cur[end] in labels:
                return "exchange"
            nxt, why = step(cur)
            if not nxt:
                return why
            grow(nxt)
            other = nxt["from"] if end == "from" else nxt["to"]
            if other in seen:
                return "loop"
            seen.add(other)
            cur = nxt
        return "max hops"

    stop_back = walk(lambda h: step_back(rpc, h, tol, window, reads), "from", lambda h: hops.insert(0, h))
    stop_forward = walk(lambda h: step_forward(rpc, h, tol, window, reads), "to", hops.append)
    for i, h in enumerate(hops):
        h["dt"] = h["t"] - hops[i - 1]["t"] if i else None
    addrs = [hops[0]["from"]] + [h["to"] for h in hops]
    return {"start": start["sig"], "amount": start["sol"], "round": is_round(start["sol"], tol), "window": window,
            "tol": tol, "hops": hops, "first": hops[0]["from"], "last": hops[-1]["to"],
            "seconds": hops[-1]["t"] - hops[0]["t"], "stop_back": stop_back, "stop_forward": stop_forward,
            "labels": {a: name(labels, a) for a in addrs if name(labels, a)}}
