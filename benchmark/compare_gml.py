#!/usr/bin/env python3
"""Compare two CTADL GML exports (`ctadl export --format gml`). The exporter
makes a node of each (variable, access path) of CVar_Type and VirtualAssign,
and an edge of each VirtualAssign pair. It numbers the nodes (`id`, `label`)
in database row order, and where several rows give a node's `type` or an
edge's `ctx`, it keeps the last row's; the engines do not share row order.
Nodes and edges are compared on their other attributes, then on these two.

    compare_gml.py A.gml B.gml

Prints "gml: equal", or "gml: equal up to row order" when only types and ctxs
differ, with exit status 0; or "gml: different", with 1. For JADX, rename the
ids that CTADL computes with `ord` first (compare_jadx.py canon). Needs
networkx, which CTADL's GML export also needs (its networkx-exporter extra)."""
import json
import sys
from collections import Counter

import networkx as nx

LAST_ROW = ("type", "ctx")


def split(d: dict) -> tuple[str, str]:
    rest = {k: v for k, v in d.items() if k != "label" and k not in LAST_ROW}
    last = {k: v for k, v in d.items() if k in LAST_ROW}
    return json.dumps(rest, sort_keys=True), json.dumps(last, sort_keys=True)


def load(path: str) -> tuple[list, list]:
    """Sorted (node, its type) and (edge, its ctx) pairs."""
    g = nx.read_gml(path, label="id")
    key = {n: split(d) for n, d in g.nodes(data=True)}
    edges = []
    for u, v, d in g.edges(data=True):
        rest, last = split(d)
        edges.append((json.dumps([key[u][0], key[v][0], rest]), last))
    return sorted(key.values()), sorted(edges)


def n(c: Counter) -> int:
    return sum(c.values())


def count(k: int, what: str) -> str:
    return f"{k} {what}" if k == 1 else f"{k} {what}s"


def main() -> int:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    a, b = load(sys.argv[1]), load(sys.argv[2])
    if a == b:
        print(f"gml: equal ({count(len(a[0]), 'node')}, {count(len(a[1]), 'edge')})")
        return 0
    ids = [(Counter(k for k, _ in x), Counter(k for k, _ in y)) for x, y in zip(a, b)]
    if all(x == y for x, y in ids):
        diff = [
            [(k, s, t) for (k, s), (_, t) in zip(x, y) if s != t] for x, y in zip(a, b)
        ]
        for what, d in zip(("node", "edge"), diff):
            for k, s, t in d[:5]:
                print(f"  ~ {what} {k}: {s} / {t}")
        print(
            f"gml: equal up to row order ({count(len(a[0]), 'node')},"
            f" {count(len(a[1]), 'edge')}; {count(len(diff[0]), 'type')} and"
            f" {count(len(diff[1]), 'ctx')} differ)"
        )
        return 0
    for what, (x, y) in zip(("node", "edge"), ids):
        for sign, d in (("-", x - y), ("+", y - x)):
            for item in list(d)[:5]:
                print(f"  {sign} {what} {item}")
    (an, bn), (ae, be) = ids
    print(
        f"gml: different (nodes {n(an)}/{n(bn)}, {n(an - bn)} only in A, {n(bn - an)} in B;"
        f" edges {n(ae)}/{n(be)}, {n(ae - be)} only in A, {n(be - ae)} in B)"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
