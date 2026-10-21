"""One HTML page for a case: a diagram of the money's path and a Solscan link for every transaction.

    python3 -m fundtrace --json relay SIG > relay.json
    python3 -m fundtrace --json sinks A B C --depth 2 > sinks.json
    python3 -m fundtrace report relay.json sinks.json -o case.html

or put `--html case.html` before any command. The page is a single file with no scripts and nothing loaded from the
network, so it opens offline and can be attached to a report as it is. Addresses that turn up in several sections are
listed at the end: that is where separate leads meet.
"""
import datetime
import html
import json
from urllib.parse import quote

from .labels import name

EXPLORER = "https://solscan.io"
BOX_W, BOX_H, GAP_X, GAP_Y, PAD, DIP = 156, 46, 84, 24, 18, 40
TITLES = {"back": "Where the money came from", "forward": "Where the money went",
          "relay": "One amount, wallet to wallet", "sinks": "Where a group's money converges",
          "funding": "Who paid just before a transaction", "deposit": "Exchange deposit address?",
          "summary": "Counterparties of a ledger", "stables": "Stablecoin counterparties"}


def load(paths):
    """Every JSON value in the files, in order. `--json` prints one value per result, so a file may hold several."""
    out, dec = [], json.JSONDecoder()
    for p in paths:
        with open(p, encoding="utf-8") as f:
            s = f.read()
        i = 0
        while True:
            while i < len(s) and s[i].isspace():
                i += 1
            if i >= len(s):
                break
            v, i = dec.raw_decode(s, i)
            out += v if isinstance(v, list) else [v]
    return out


def kind(r):
    """Which command produced a result, read from its keys."""
    if not isinstance(r, dict):
        return None
    for k, key in (("relay", "hops"), ("sinks", "sinks"), ("funding", "funding"), ("deposit", "is_deposit"),
                   ("summary", "sent_to"), ("stables", "flows")):
        if key in r:
            return k
    if "chain" in r:
        return "back" if any("in" in c for c in r["chain"]) else "forward"
    return None


def _e(frm, to, sol, t, sig, note=""):
    return {"from": frm, "to": to, "sol": sol, "t": t, "sig": sig, "note": note}


def edges(r):
    """The transfers behind a result, in the direction the money moved, oldest first."""
    k, out = kind(r), []
    if k == "relay":
        for h in r["hops"]:
            note = " · ".join(x for x in ("start" if h["sig"] == r["start"] else "",
                                          f"+{h['dt']} s" if h.get("dt") is not None else "") if x)
            out.append(_e(h["from"], h["to"], h["sol"], h["t"], h["sig"], note))
        return out
    if k == "back":
        out = [_e(c["from"], c["addr"], c["in"]["sol"], c["in"]["t"], c["in"]["sig"])
               for c in r["chain"] if c.get("in") and c.get("from")]
    elif k == "forward":
        out = [_e(c["addr"], c["out"]["to"], c["out"]["sol"], c["out"]["t"], c["out"]["sig"])
               for c in r["chain"] if c.get("out")]
    elif k == "funding":
        out = [_e(x["from"], r["addr"], x["sol"], x["t"], x["sig"],
                  "memo, signers: " + ", ".join(x["signers"]) if x["memo"] else "")
               for x in r["funding"] if x.get("from")]
    elif k == "sinks":
        seen = set()
        for row in r["sinks"]:
            legs = row.get("legs") or [{"from": o, "to": row["addr"], "sol": v, "t": None, "sig": None}
                                       for o, v in row["from"].items()]
            for g in legs:
                if (g["sig"], g["from"], g["to"]) not in seen:
                    seen.add((g["sig"], g["from"], g["to"]))
                    out.append(_e(g["from"], g["to"], g["sol"], g["t"], g["sig"],
                                  "via a one-off wallet" if g["from"] in row["via"] else ""))
    return sorted(out, key=lambda e: (e["t"] is None, e["t"] or 0))


def roles(r):
    """{address: (css class, caption)} for the addresses a result is about."""
    k = kind(r)
    if k == "relay":
        return {r["first"]: ("start", "first holder"), r["last"]: ("end", "last holder")}
    if k in ("back", "forward"):
        out = {r["start"]: ("start", "start")}
        if r["end"] != r["start"]:
            out[r["end"]] = ("end", "end: " + r["stop"])
        return out
    if k == "sinks":
        out = {w: ("group", "group wallet") for w in r["wallets"]}
        for x in r["sinks"]:
            out.update({v: ("via", "one-off wallet") for v in x["via"]})
        out.update({x["addr"]: ("sink", f"sink: {x['senders']}/{x['of']} wallets") for x in r["sinks"]})
        return out
    if k in ("funding", "deposit", "stables"):
        return {r["addr"]: ("start", "")}
    return {}


def labels_in(r, out=None):
    """Exchange labels a result already carries: {address: label}."""
    out = {} if out is None else out
    if isinstance(r, dict):
        if isinstance(r.get("addr"), str) and isinstance(r.get("label"), str) and r["label"]:
            out[r["addr"]] = r["label"]
        if isinstance(r.get("labels"), dict):
            out.update({a: v for a, v in r["labels"].items() if isinstance(v, str) and v})
        for v in r.values():
            labels_in(v, out)
    elif isinstance(r, list):
        for v in r:
            labels_in(v, out)
    return out


def where(results):
    """{address: [numbers of the sections it appears in]}, in order of first appearance."""
    out = {}
    for i, r in enumerate(results, 1):
        addrs = [a for e in edges(r) for a in (e["from"], e["to"])] + list(roles(r))
        for a in dict.fromkeys(addrs):
            out.setdefault(a, []).append(i)
    return out


# --- html helpers ---------------------------------------------------------------------------------------------------
def esc(x):
    return html.escape(str(x), quote=True)


def short(a):
    a = str(a)
    return a if len(a) <= 12 else a[:4] + "…" + a[-4:]


def url(path, key):
    return esc(f"{EXPLORER}/{path}/{quote(str(key), safe='')}")


def fmt(sol):
    return f"{sol:,.9f}".rstrip("0").rstrip(".")


def when(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if t else "—"


def addr_cell(a, label):
    tag = f' <span class="label">{esc(label)}</span>' if label else ""
    return f'<a class="mono" href="{url("account", a)}" title="{esc(a)}">{esc(short(a))}</a>{tag}'


def tx_cell(sig):
    if not sig:
        return "—"
    return f'<a class="mono" href="{url("tx", sig)}" title="{esc(sig)}">{esc(short(sig))}</a>'


# --- diagram --------------------------------------------------------------------------------------------------------
def _cycles(nodes, pairs):
    """Edges that close a cycle (found by depth-first search); they are drawn underneath, not used for layout."""
    nxt = {n: [] for n in nodes}
    for a, b in pairs:
        nxt[a].append(b)
    state, back = {}, set()
    for root in nodes:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(nxt[root]))]
        while stack:
            n, it = stack[-1]
            m = next(it, None)
            if m is None:
                state[n] = 2
                stack.pop()
            elif state.get(m) == 1:
                back.add((n, m))
            elif m not in state:
                state[m] = 1
                stack.append((m, iter(nxt[m])))
    return back


def _y_at(x1, y1, x2, y2, x):
    """Height of the arrow's curve at `x` (the curve leaves and enters horizontally)."""
    lo, hi = 0.0, 1.0
    for _ in range(30):
        t = (lo + hi) / 2
        if x1 + (x2 - x1) * (1.5 * t * (1 - t) + t ** 3) < x:
            lo = t
        else:
            hi = t
    return y1 + (y2 - y1) * (3 * t * t - 2 * t ** 3)


def diagram(es, role, label, sid):
    """An SVG of the transfers: one column per step, an arrow per sender-recipient pair, links to Solscan."""
    pairs = {}
    for e in es:
        if e["from"] == e["to"]:
            continue
        p = pairs.setdefault((e["from"], e["to"]), {"sol": 0.0, "n": 0, "sig": e["sig"], "t": e["t"]})
        p["sol"] += e["sol"]
        p["n"] += 1
    nodes = list(dict.fromkeys(a for pair in pairs for a in pair))
    back = _cycles(nodes, pairs)
    layer = dict.fromkeys(nodes, 0)
    for _ in nodes:                                     # longest path from a source, cycles left out
        moved = False
        for a, b in pairs:
            if (a, b) not in back and layer[b] <= layer[a]:
                layer[b], moved = layer[a] + 1, True
        if not moved:
            break
    cols = {}
    for n in nodes:
        cols.setdefault(layer[n], []).append(n)
    rows = max(len(c) for c in cols.values())
    ypos = {}
    for c in sorted(cols):                              # order each column by where its senders sit
        def key(i_n, c=c):
            ps = [ypos[a] for a, b in pairs if b == i_n[1] and a in ypos]
            return sum(ps) / len(ps) if ps else i_n[0] + (rows - len(cols[c])) / 2
        order = [n for _, n in sorted(enumerate(cols[c]), key=key)]
        ypos.update({n: i + (rows - len(order)) / 2 for i, n in enumerate(order)})
    X = lambda n: PAD + layer[n] * (BOX_W + GAP_X)
    Y = lambda n: PAD + ypos[n] * (BOX_H + GAP_Y)
    w = PAD * 2 + (max(layer.values()) + 1) * BOX_W + max(layer.values()) * GAP_X
    floor = PAD + rows * BOX_H + (rows - 1) * GAP_Y
    h = floor + PAD + (DIP if back else 0)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
         f'aria-label="Diagram of {len(pairs)} transfer(s) between {len(nodes)} addresses">',
         f'<defs><marker id="arrow{sid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
         f'orient="auto-start-reverse"><path class="tip" d="M0,0 L10,5 L0,10 z"/></marker></defs>']
    for (a, b), p in pairs.items():
        if (a, b) in back:
            x1, y1, x2, y2 = X(a) + BOX_W / 2, Y(a) + BOX_H, X(b) + BOX_W / 2, Y(b) + BOX_H + 2
            yc = floor + DIP - 6
            d = f"M{x1:.0f},{y1:.0f} C{x1:.0f},{yc:.0f} {x2:.0f},{yc:.0f} {x2:.0f},{y2:.0f}"
            lx, ly = (x1 + x2) / 2, (y1 + y2) / 8 + yc * 0.75 - 5
        else:
            # straight along the sender's row, bending only in the last gap, so a long arrow does not run
            # through the boxes it skips; the amount sits in the gap next to the sender
            x1, y1, x2, y2 = X(a) + BOX_W, Y(a) + BOX_H / 2, X(b) - 2, Y(b) + BOX_H / 2
            xa = max(x1, x2 - GAP_X + 2)
            dx = (x2 - xa) / 2
            d = f"M{x1:.0f},{y1:.0f} H{xa:.0f} C{xa + dx:.0f},{y1:.0f} {x2 - dx:.0f},{y2:.0f} {x2:.0f},{y2:.0f}"
            lx = x1 + GAP_X / 2
            ly = (_y_at(xa, y1, x2, y2, lx) if lx > xa else y1) - 6
        amount = f"{p['sol']:,.4f}" + (f" ×{p['n']}" if p["n"] > 1 else "")
        tip = f"{fmt(p['sol'])} SOL, {a} → {b}" + (f", {p['n']} transfers; first" if p["n"] > 1 else "") + \
              f" {when(p['t'])} UTC, {p['sig'] or 'no signature'}"
        body = (f'<g class="edge{" back" if (a, b) in back else ""}"><title>{esc(tip)}</title>'
                f'<path d="{d}" marker-end="url(#arrow{sid})"/>'
                f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="middle">{esc(amount)}</text></g>')
        o.append(f'<a href="{url("tx", p["sig"])}">{body}</a>' if p["sig"] else body)
    for n in nodes:
        cls, cap = role.get(n, ("", ""))
        lab = label(n)
        cap = lab or cap
        cls = (cls + " exchange" if lab else cls).strip()
        cap = cap if len(cap) <= 22 else cap[:21] + "…"
        x, y = X(n), Y(n)
        o.append(f'<a href="{url("account", n)}"><g class="node {cls}"><title>{esc(n)}{esc(" — " + lab) if lab else ""}'
                 f'</title><rect x="{x}" y="{y:.0f}" width="{BOX_W}" height="{BOX_H}" rx="8"/>'
                 f'<text class="addr" x="{x + 12}" y="{y + 19:.0f}">{esc(short(n))}</text>'
                 f'<text class="sub" x="{x + 12}" y="{y + 36:.0f}">{esc(cap)}</text></g></a>')
    o.append("</svg>")
    return "".join(o)


def table(es, label):
    out = ['<div class="wide"><table><thead><tr><th class="n">#</th><th>from</th><th>to</th><th class="n">SOL</th>'
           '<th>time, UTC</th><th>transaction</th><th>note</th></tr></thead><tbody>']
    for i, e in enumerate(es, 1):
        hl = ' class="hl"' if "start" in e["note"] else ""
        out.append(f'<tr{hl}><td class="n">{i}</td><td>{addr_cell(e["from"], label(e["from"]))}</td>'
                   f'<td>{addr_cell(e["to"], label(e["to"]))}</td><td class="n">{esc(fmt(e["sol"]))}</td>'
                   f'<td class="t">{when(e["t"])}</td><td>{tx_cell(e["sig"])}</td><td>{esc(e["note"])}</td></tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


CSS = """
:root{--bg:#ffffff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--card:#f6f8fa;--node:#ffffff;--accent:#0969da;
--accent-bg:#ddf4ff;--warn:#9a6700;--warn-bg:#fff8c5;--edge:#818b98;--hl:#fff8c5}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#3d444d;--card:#151b23;
--node:#0d1117;--accent:#4493f8;--accent-bg:#0c2d6b;--warn:#d29922;--warn-bg:#2e2410;--edge:#7d8590;--hl:#2e2410}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,
Arial,sans-serif}
main{max-width:1200px;margin:0 auto;padding:32px 16px 72px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:0 0 12px;display:flex;gap:10px;align-items:baseline}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.meta{color:var(--muted);margin:0 0 14px}
.note{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:0 0 18px}
nav{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0 0 8px}
section{border-top:1px solid var(--line);padding:26px 0 8px}
.num{color:var(--muted);font-variant-numeric:tabular-nums}
.tag{font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--muted);border:1px solid var(--line);
border-radius:6px;padding:1px 6px}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow-x:auto;
font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;margin:0 0 14px}
.diagram{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card);margin:0 0 14px}
.diagram svg{display:block}
.node rect{fill:var(--node);stroke:var(--line);stroke-width:1.2}
.node.start rect,.node.sink rect{stroke:var(--accent);stroke-width:2;fill:var(--accent-bg)}
.node.exchange rect{stroke:var(--warn);stroke-width:2;fill:var(--warn-bg)}
.node.end rect{stroke:var(--fg);stroke-dasharray:4 3}
.node .addr{font:600 12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;fill:var(--fg)}
.node .sub{font-size:11px;fill:var(--muted)}
.edge path{fill:none;stroke:var(--edge);stroke-width:1.6}.edge.back path{stroke-dasharray:5 4}
.tip{fill:var(--edge)}
.edge text{font-size:11.5px;fill:var(--fg);paint-order:stroke;stroke:var(--card);stroke-width:4px;
font-variant-numeric:tabular-nums}
a:hover .node rect,a:hover .edge path{stroke:var(--accent)}a:hover .edge text{fill:var(--accent)}
.wide{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}
th{color:var(--muted);font-weight:600}td:last-child{white-space:normal}
.n{text-align:right;font-variant-numeric:tabular-nums}.t{font-variant-numeric:tabular-nums}
tr.hl td{background:var(--hl)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
.label{font-size:12px;color:var(--warn);background:var(--warn-bg);border-radius:6px;padding:1px 6px;margin-left:4px}
.many td{font-weight:600}
footer{color:var(--muted);font-size:13px;margin-top:28px}
"""


def render(results, labels=None, title="Fund trace", text=None, now=None, version=""):
    """The whole page. `text(result)` may give the command's text output, shown above each diagram."""
    labels = labels or {}
    found = {}
    for r in results:
        labels_in(r, found)
    label = lambda a: found.get(a) or name(labels, a)
    seen = where(results)
    sigs, parts, nav = set(), [], []
    for i, r in enumerate(results, 1):
        k = kind(r)
        es = edges(r)
        sigs.update(e["sig"] for e in es if e["sig"])
        head = TITLES.get(k, "Result")
        nav.append(f'<a href="#s{i}">{i}. {esc(head)}</a>')
        p = [f'<section id="s{i}"><h2><span class="num">{i}</span><span>{esc(head)}</span>'
             f'<span class="tag">{esc(k or "unknown")}</span></h2>']
        txt = text(r) if text and k else ""
        if txt:
            p.append(f"<pre>{esc(txt.rstrip())}</pre>")
        if es:
            p.append(f'<div class="diagram">{diagram(es, roles(r), label, i)}</div>')
            p.append(table(es, label))
        elif not txt:
            p.append(f"<pre>{esc(json.dumps(r, indent=1)[:5000])}</pre>")
        p.append("</section>")
        parts.append("".join(p))
    order = sorted(seen, key=lambda a: -len(seen[a]))
    idx = ['<section id="addresses"><h2><span>Addresses</span></h2><p class="meta">Every address on this page. One that '
           'appears in several sections is where separate leads meet.</p><div class="wide"><table><thead><tr>'
           '<th>address</th><th>label</th><th>sections</th></tr></thead><tbody>']
    for a in order:
        many = ' class="many"' if len(seen[a]) > 1 else ""
        links = ", ".join(f'<a href="#s{j}">{j}</a>' for j in seen[a])
        idx.append(f'<tr{many}><td><a class="mono" href="{url("account", a)}">{esc(a)}</a></td>'
                   f'<td>{esc(label(a))}</td><td>{links}</td></tr>')
    idx.append("</tbody></table></div></section>")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>'
            f'<style>{CSS}</style></head><body><main><h1>{esc(title)}</h1>'
            f'<p class="meta">{len(results)} result(s) · {len(sigs)} transaction(s) · {len(seen)} address(es) · '
            f'made {now:%Y-%m-%d %H:%M} UTC with solana-fund-tracer {esc(version)}</p>'
            f'<p class="note">Every arrow is a transaction and every box an account: click to open it on Solscan. '
            f'Read from public data, no keys. A result is a lead, not proof: check every hop on the raw transaction '
            f'before relying on it, and do not publish accusations on the strength of a heuristic.</p>'
            f'<nav>{"".join(nav)}<a href="#addresses">Addresses</a></nav>{"".join(parts)}{"".join(idx)}'
            f'<footer>solana-fund-tracer · read-only · MIT</footer></main></body></html>\n')
