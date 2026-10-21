"""Exchange address labels.

The tracer stops when it reaches a labelled exchange wallet, and a "deposit address" is one that sweeps money into
such a wallet. Labels are not shipped with this repository: `fundtrace labels` downloads Dune's open Spellbook list
of Solana exchange addresses (Business Source License 1.1, see the Spellbook repository) into a local labels.json.
You can add your own labels to the same file in the same format.
"""
import json
import os
import re

from .rpc import http

SPELLBOOK_URL = ("https://raw.githubusercontent.com/duneanalytics/spellbook/main/dbt_subprojects/hourly_spellbook/"
                 "models/_sector/cex/addresses/chains/solana/cex_solana_addresses.sql")
ROW = re.compile(r"\(\s*'solana'\s*,\s*'([1-9A-HJ-NP-Za-km-z]{32,44})'\s*,\s*'([^']*)'\s*,\s*'([^']*)'")


def parse_spellbook(sql):
    """{address: {"cex": name, "name": distinct name}} from the Spellbook model's VALUES rows."""
    return {a: {"cex": cex, "name": name} for a, cex, name in ROW.findall(sql)}


def fetch(dest="labels.json", url=SPELLBOOK_URL, get=http):
    labels = parse_spellbook(get(url))
    if not labels:
        raise RuntimeError(f"no labels found at {url}")
    mine = load(dest) if os.path.exists(dest) else {}
    mine.update(labels)
    with open(dest, "w") as f:
        json.dump(mine, f, indent=1, sort_keys=True)
    return len(labels)


def load(path):
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def name(labels, addr):
    x = labels.get(addr)
    return x.get("name") or x.get("cex") if x else ""
