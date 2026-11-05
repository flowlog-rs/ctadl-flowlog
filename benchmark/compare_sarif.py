#!/usr/bin/env python3
"""Compare two CTADL SARIF reports (Souffle vs FlowLog backend).

    compare_sarif.py A.sarif B.sarif

Reports are compared after normalization: references by index into a run's
`logicalLocations` and `addresses` (whose order follows database row order)
are replaced by the qualified names they denote, graph node ids (engine
specific vertex ids) are replaced by canonical names (see canon_graphs),
results whose locations CTADL picks arbitrarily are compared without them
(REPRESENTATIVE_RULES), object keys are sorted, run timestamps, the report's
date (`properties.day`) and paths of the run directories are dropped, and
arrays are compared as multisets, except thread-flow locations, which are
paths.
Exit status 0 iff the normalized reports are equal; otherwise the places where
they differ are listed, with a few example items of arrays that differ, and
the verdict says whether the results are equal without their code flows. A
code flow is one source-to-sink path, through the instructions that CTADL's
choice-domain relations pick as witnesses (VirtualAssign's `insn`), which
may differ between engines and runs.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter

VOLATILE_KEYS = {
    "CTADL_Index_Timestamp", "CTADL_Query_Timestamp", "CTADL_Import_Path",
    "startTimeUtc", "endTimeUtc",
}
# Dropped from the report's top-level `properties` only: CTADL's formatter
# writes the date of the run there (`day`), which differs when two runs
# straddle midnight.
VOLATILE_REPORT_PROPERTIES = {"day"}
ORDERED_ARRAYS = {"threadFlows.locations"}
# C0006 ("Source-sink function", sarif+all) reports one (forward, backward)
# instruction pair per function, chosen by `GROUP BY function` over bare
# columns in CTADL's vis/sarif.py, i.e. by SQLite from whichever row comes
# last in table order. The locations and labels of such a result depend on
# the engine's row order, so only its function (the message) is compared.
REPRESENTATIVE_RULES = {"C0006": ("locations", "properties")}


INDEXED = ("logicalLocations", "addresses")


def resolve_run(run: dict) -> dict:
    tables = {k: run.get(k, []) for k in INDEXED}

    def qual(table: str, i: int, depth: int = 0) -> str:
        e = tables[table][i]
        name = f"{e.get('kind')}:{e.get('fullyQualifiedName') or e.get('name') or e.get('absoluteAddress')}"
        p = e.get("parentIndex")
        return name if p is None or depth > 32 else qual(table, p, depth + 1) + "/" + name

    def walk(x, key: str = "", top: bool = False):
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                if k == "parentIndex" and key in INDEXED:
                    out["parent"] = qual(key, v)
                elif k == "index" and key in INDEXED and not top:
                    out["ref"] = qual(key, v)
                else:
                    out[k] = walk(v, k)
            return out
        if isinstance(x, list):
            return [walk(v, key, top) for v in x]
        return x

    return {k: walk(v, k, top=True) if k in INDEXED else walk(v, k) for k, v in run.items()}


def norm(x, path: str = "", parent: str = ""):
    if isinstance(x, dict):
        drop = VOLATILE_KEYS | (VOLATILE_REPORT_PROPERTIES if (parent, path) == ("", "properties") else set())
        return {k: norm(v, k, path) for k, v in sorted(x.items()) if k not in drop}
    if isinstance(x, list):
        items = [norm(v, path, parent) for v in x]
        if f"{parent}.{path}" in ORDERED_ARRAYS:
            return items
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(x, str):
        return re.sub(r"/(souffle|flowlog)(/|$)", r"/BACKEND\2", x)
    return x


def canon_graphs(x):
    """Renames the nodes of every SARIF graph canonically.

    Graph node ids are CTADL vertex ids, which differ between engines (e.g.
    `ord`-based slice vertices), and several nodes may denote the same
    variable, so a node is named by Weisfeiler-Lehman refinement: a hash of
    its contents, refined with the labels and names of its in- and
    out-neighbours until the partition is stable. Edge ids
    (`source/target/label`) are rebuilt from the new names."""
    if isinstance(x, list):
        return [canon_graphs(v) for v in x]
    if not isinstance(x, dict):
        return x
    x = {k: canon_graphs(v) for k, v in x.items()}
    nodes, edges = x.get("nodes"), x.get("edges")
    if not (isinstance(nodes, list) and isinstance(edges, list)
            and all(isinstance(n, dict) and "id" in n for n in nodes)):
        return x

    def h(v) -> str:
        return hashlib.sha1(json.dumps(v, sort_keys=True).encode()).hexdigest()[:16]

    def label(e: dict) -> str:
        return json.dumps(e.get("label"), sort_keys=True)

    name = {n["id"]: h({k: v for k, v in n.items() if k != "id"}) for n in nodes}
    for _ in range(len(nodes) + 1):
        sig = {nid: [] for nid in name}
        for e in edges:
            s, t = e.get("sourceNodeId"), e.get("targetNodeId")
            if s in sig and t in name:
                sig[s].append("out" + label(e) + name[t])
            if t in sig and s in name:
                sig[t].append("in" + label(e) + name[s])
        new = {nid: h([name[nid]] + sorted(sig[nid])) for nid in name}
        stable = len(set(new.values())) == len(set(name.values()))
        name = new
        if stable:
            break
    x["nodes"] = [dict(n, id=name[n["id"]]) for n in nodes]
    out = []
    for e in edges:
        s, t = name.get(e.get("sourceNodeId")), name.get(e.get("targetNodeId"))
        e = dict(e, sourceNodeId=s, targetNodeId=t)
        if "id" in e:
            e["id"] = f"{s}/{t}/{(e.get('label') or {}).get('text', '')}"
        out.append(e)
    x["edges"] = out
    return x


def drop_representatives(run: dict) -> dict:
    for r in run.get("results", []):
        for k in REPRESENTATIVE_RULES.get(r.get("ruleId"), ()):
            r.pop(k, None)
    return run


def differences(a, b, path: str, out: list, examples: int = 3) -> None:
    """Describes where two normalized reports differ. Arrays other than
    singletons are compared as multisets (a line-based diff of large reports
    takes hours)."""
    if a == b:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in a and k in b:
                differences(a[k], b[k], f"{path}.{k}", out, examples)
            else:
                out.append(f"{path}.{k}: only in {'A' if k in a else 'B'}")
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) == len(b) == 1:
            differences(a[0], b[0], f"{path}[0]", out, examples)
            return
        ca = Counter(json.dumps(v, sort_keys=True) for v in a)
        cb = Counter(json.dumps(v, sort_keys=True) for v in b)
        only_a, only_b = ca - cb, cb - ca
        if not only_a and not only_b:
            out.append(f"{path}: same items in another order")
            return
        out.append(f"{path}: {sum(only_a.values())} items only in A, {sum(only_b.values())} only in B")
        for sign, c in (("-", only_a), ("+", only_b)):
            out += [f"  {sign} {s[:300]}" for s in sorted(c)[:examples]]
        return
    out.append(f"{path}: {json.dumps(a)[:200]} != {json.dumps(b)[:200]}")


def findings(doc: dict) -> list:
    """Each run's results without their code flows, as a multiset."""
    return [Counter(json.dumps({k: v for k, v in r.items() if k != "codeFlows"}, sort_keys=True)
                    for r in run.get("results", []))
            for run in doc.get("runs", [])]


def main() -> int:
    docs = [json.load(open(p)) for p in sys.argv[1:3]]
    for d in docs:
        d["runs"] = [drop_representatives(canon_graphs(resolve_run(r))) for r in d.get("runs", [])]
    a, b = (norm(d) for d in docs)
    if a == b:
        print("sarif: equal")
        return 0
    out: list = []
    differences(a, b, "", out)
    print(f"A: {sys.argv[1]}\nB: {sys.argv[2]}")
    print("\n".join(out[:60]))
    same = "; same results up to code flows" if findings(a) == findings(b) else ""
    print(f"sarif: different ({sum(not l.startswith(' ') for l in out)} places){same}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
