"""What a transaction did to balances: SOL per account, tokens per owner, programs and instruction names."""
import re

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
PROGRAMS = {
    "11111111111111111111111111111111": "System",
    "ComputeBudget111111111111111111111111111111": "ComputeBudget",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "ATA",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo",
    "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo": "Memo",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
}
# Instruction names that mean "a trade", not "a transfer" (read from program logs).
TRADES = {"Buy", "Sell", "BuyExactQuoteIn", "BuyExactSolIn", "Swap", "SwapBaseIn", "SwapBaseOut", "Route",
          "SharedAccountsRoute", "ExactOutRoute"}


def keys_of(t):
    """All account keys of a transaction, including those loaded from lookup tables."""
    msg = t["transaction"]["message"]
    ks = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    la = (t.get("meta") or {}).get("loadedAddresses") or {}
    return ks + la.get("writable", []) + la.get("readonly", [])


def token_accounts(t):
    """Keys of the token accounts a transaction touched. Their lamports move with wrapped SOL, so they are not
    counterparties in their own right: the owner is."""
    meta = t.get("meta") or {}
    ks = keys_of(t)
    idx = {x["accountIndex"] for x in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])}
    return {ks[i] for i in idx if i < len(ks)}


def _ui(x):
    return float(((x or {}).get("uiTokenAmount") or {}).get("uiAmount") or 0)


def token_deltas(t):
    """{(owner, mint): change in whole tokens} for every token account whose balance moved."""
    meta = t.get("meta") or {}
    pre = {(x["accountIndex"], x["mint"]): x for x in meta.get("preTokenBalances") or []}
    post = {(x["accountIndex"], x["mint"]): x for x in meta.get("postTokenBalances") or []}
    out = {}
    for k in set(pre) | set(post):
        d = _ui(post.get(k)) - _ui(pre.get(k))
        if d:
            owner = (post.get(k) or pre.get(k)).get("owner")
            out[(owner, k[1])] = out.get((owner, k[1]), 0.0) + d
    return out


def summary(t):
    """Signers, SOL change per account (in SOL), token change per (owner, mint), programs, instruction names."""
    meta = t.get("meta") or {}
    msg = t["transaction"]["message"]
    ks = keys_of(t)
    sol = {}
    for i, (a, b) in enumerate(zip(meta.get("preBalances", []), meta.get("postBalances", []))):
        if a != b:
            sol[ks[i]] = (b - a) / 1e9
    signers = [k["pubkey"] for k in msg["accountKeys"] if isinstance(k, dict) and k.get("signer")]
    progs = [PROGRAMS.get(ins.get("programId"), (ins.get("programId") or "?")[:8])
             for ins in msg.get("instructions", [])]
    names = re.findall(r"Program log: Instruction: (\w+)", "\n".join(meta.get("logMessages") or []))
    return {"signers": signers, "fee": meta.get("fee", 0) / 1e9, "err": meta.get("err"), "sol": sol,
            "tok": token_deltas(t), "progs": progs, "ix": names, "slot": t.get("slot"), "time": t.get("blockTime")}


def is_trade(u):
    return bool(TRADES & set(u["ix"]))


def sol_with_wsol(u, addr):
    """SOL change of an address, counting wrapped SOL it owns as SOL."""
    return u["sol"].get(addr, 0.0) + u["tok"].get((addr, WSOL), 0.0)


def counterparty(u, addr, delta):
    """Who took the money when addr paid (largest gain), or who paid when addr received (largest loss)."""
    others = {k: v for k, v in u["sol"].items() if k != addr}
    if not others:
        return None
    if delta < 0:
        k = max(others, key=others.get)
        return k if others[k] > 0 else None
    k = min(others, key=others.get)
    return k if others[k] < 0 else None
