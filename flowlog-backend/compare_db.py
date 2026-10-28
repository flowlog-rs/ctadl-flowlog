#!/usr/bin/env python3
"""Compare the relations of two CTADL SQLite DBs (Souffle vs FlowLog).

Either DB may use Souffle's layout (each relation `R` is a view over `_R`
joined with `__SymbolTable`; detected by the presence of `__SymbolTable`) or
FlowLog's native layout (plain tables). Rows are compared as sets of
stringified tuples, by column position.

Relations whose Souffle declaration has a `choice-domain` (CHOICE_KEYS) are
compared on full rows, but if they differ only in the dependent columns --
both sides hold exactly one row per key and the same keys -- the relation is
counted as equal up to choice-domain witnesses: Souffle keeps the first
candidate its evaluation order finds, so the witness is engine-specific, and
any candidate is a valid result. `--keys R=0,1,2` compares a projection
instead; by default (DEFAULT_KEYS) AliasedBy and IntraObjectAliasedBy, whose
choice-domain the translator's policy drops, and VirtualAssign are compared
on their keys (run_taintfront.py and bench.py compare VirtualAssign on full
rows with `--keys VirtualAssign=0,1,2,3,4,5,6`). With
`--schema`, record-typed columns (stored as engine-specific record ids) are
left out. Timestamp rows of CTADLConfig are ignored. Slice vertex ids are
computed with `ord`, i.e. are engine-specific symbol ids: they are replaced
by the vertex they denote. Views over raw symbol ids (`_R`) are skipped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys


DEFAULT_KEYS = {
    # index phase
    "VirtualAssign": [1, 2, 3, 4, 6],
    "AliasedBy": [0, 1, 2, 3],
    "IntraObjectAliasedBy": [0, 1, 2],
}


# {(relation base name, arity): choice-domain key columns} of CTADL's
# choice-domain relations whose dependent columns are not functions of the
# key (the slices' ReachableVertex ids and suspicion are)
CHOICE_KEYS = {
    ("VirtualAssign", 7): [1, 2, 3, 4, 6],
    ("AliasedBy", 5): [0, 1, 2, 3],
    ("IntraObjectAliasedBy", 4): [0, 1, 2],
    ("ContextObj", 4): [0, 1, 2],
    ("ParthianVertex", 3): [0, 1],
    ("LostAssign", 3): [0],
}


def same_up_to_choice(name: str, s: set, f: set) -> bool:
    """Both sides choose one witness for the same set of choice keys."""
    arity = len(next(iter(s or f), ()))
    key = CHOICE_KEYS.get((name.rsplit(".", 1)[-1], arity))
    if key is None:
        return False
    sk = [tuple(r[i] for i in key) for r in s]
    fk = [tuple(r[i] for i in key) for r in f]
    return len(set(sk)) == len(sk) and len(set(fk)) == len(fk) and set(sk) == set(fk)


IGNORED_ROWS = {
    # (relation, column 0 value): written by the CTADL driver with the current
    # time or the (per-backend) facts directory
    ("CTADLConfig", "CTADL_Index_Timestamp"),
    ("CTADLConfig", "CTADL_Query_Timestamp"),
    ("CTADLConfig", "CTADL_Import_Path"),
}


# {edge relation: vertex relation}; column 0 of a vertex relation is its id
# and columns 0 and 1 of an edge relation are vertex ids. flow.* and
# {natural,reverse}_flow.* are views CTADL creates after the query.
ID_EDGES = {
    "forward_flow.ReachableEdge": "forward_flow.ReachableVertex",
    "backward_flow.ReachableEdge": "backward_flow.ReachableVertex",
    "natural_flow.ReachableEdge": "flow.ReachableVertex",
    "reverse_flow.ReachableEdge": "flow.ReachableVertex",
}
ID_VERTICES = set(ID_EDGES.values())

# CTADL writes the numeric values of this symbol-typed input as raw integers,
# so its view shows whatever symbol has that id; compare the raw values.
RAW_SQL = {
    "CTADLConfig_Input": 'SELECT s.symbol, r."1" FROM "_CTADLConfig_Input" r '
                         'JOIN "__SymbolTable" s ON r."0" = s.id',
}


def normalize_ids(db: sqlite3.Connection, rels: dict, name: str, rs: list) -> list:
    if name in ID_VERTICES:
        return [r[1:] for r in rs]
    if name in ID_EDGES:
        vrel = ID_EDGES[name]
        vertex = {r[0]: ":".join(r[1:]) for r in rows(db, vrel)} if vrel in rels else {}
        return [(vertex.get(r[0], f"?{r[0]}"), vertex.get(r[1], f"?{r[1]}")) + r[2:] for r in rs]
    return rs


def relations(db: sqlite3.Connection) -> dict[str, str]:
    souffle = db.execute("SELECT 1 FROM sqlite_master WHERE name='__SymbolTable'").fetchone() is not None
    kind = "view" if souffle else "table"
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type=?", (kind,))]
    return {n: n for n in names if not n.startswith("_") and not n.startswith("sqlite_")}


def rows(db: sqlite3.Connection, name: str) -> list[tuple]:
    souffle = db.execute("SELECT 1 FROM sqlite_master WHERE name='__SymbolTable'").fetchone() is not None
    cur = db.execute(RAW_SQL[name] if souffle and name in RAW_SQL else f'SELECT * FROM "{name}"')
    out = [tuple("" if v is None else str(v) for v in r) for r in cur]
    return [r for r in out if not r or (name, r[0]) not in IGNORED_ROWS]


def record_columns(schema_paths: list) -> dict:
    """{relation: [indexes of non-record columns]} for relations with records."""
    res: dict = {}
    for p in schema_paths:
        with open(p) as fh:
            s = json.load(fh)
        for e in s.get("outputs", []) + s.get("inputs", []):
            kinds = [c["kind"] for c in e["columns"]]
            if "record" in kinds:
                res[e["name"]] = [i for i, k in enumerate(kinds) if k != "record"]
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("souffle_db")
    ap.add_argument("flowlog_db")
    ap.add_argument("--keys", action="append", default=[], help="R=i,j,... compare a projection")
    ap.add_argument("--only", action="append", default=[], help="restrict to these relations")
    ap.add_argument("--schema", action="append", default=[], help="translator schema manifest(s)")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("-q", "--quiet", action="store_true", help="only print mismatches and the summary")
    args = ap.parse_args()

    keys = dict(record_columns(args.schema))
    keys.update(DEFAULT_KEYS)
    for k in args.keys:
        rel, cols = k.split("=", 1)
        keys[rel] = [int(c) for c in cols.split(",")]

    sdb = sqlite3.connect(args.souffle_db)
    fdb = sqlite3.connect(args.flowlog_db)
    srels = relations(sdb)
    frels = relations(fdb)
    names = sorted(set(srels) | set(frels))
    if args.only:
        names = [n for n in names if n in args.only]

    n_ok = n_bad = n_wit = 0
    for name in names:
        s = normalize_ids(sdb, srels, name, rows(sdb, name)) if name in srels else None
        f = normalize_ids(fdb, frels, name, rows(fdb, name)) if name in frels else None
        note = ""
        if s is None:
            if f:
                print(f"EXTRA    {name}: only in FlowLog ({len(f)} rows)")
                n_bad += 1
            continue
        if f is None:
            if s:
                print(f"MISSING  {name}: absent from the FlowLog DB, Souffle has {len(s)} rows")
                n_bad += 1
            else:
                n_ok += 1
                if not args.quiet:
                    print(f"ok       {name}: empty (table absent from the FlowLog DB)")
            continue
        if s and f and len(s[0]) != len(f[0]):
            print(f"ARITY    {name}: souffle {len(s[0])} columns, flowlog {len(f[0])}")
            n_bad += 1
            continue
        if name in keys:
            proj = keys[name]
            s_full, f_full = set(s), set(f)
            s = [tuple(r[i] for i in proj) for r in s]
            f = [tuple(r[i] for i in proj) for r in f]
            note = f" [projection {proj}; full rows souffle={len(s_full)} flowlog={len(f_full)}]"
        ss, fs = set(s), set(f)
        if ss == fs:
            n_ok += 1
            if not args.quiet:
                print(f"ok       {name}: {len(ss)} rows{note}")
            continue
        only_s, only_f = sorted(ss - fs), sorted(fs - ss)
        arity = len(next(iter(ss | fs)))
        unprojected = name not in keys or keys[name] == list(range(arity))
        if unprojected and same_up_to_choice(name, ss, fs):
            n_ok += 1
            n_wit += 1
            print(f"WITNESS  {name}: {len(ss)} rows, same choice-domain keys, "
                  f"{len(only_s)} with another witness")
        else:
            n_bad += 1
            print(f"DIFF     {name}: souffle {len(ss)}, flowlog {len(fs)}, "
                  f"souffle-only {len(only_s)}, flowlog-only {len(only_f)}{note}")
        for r in only_s[: args.samples]:
            print(f"           - {r}")
        for r in only_f[: args.samples]:
            print(f"           + {r}")
    wit = f" ({n_wit} up to choice-domain witnesses)" if n_wit else ""
    print(f"summary: {n_ok} equal{wit}, {n_bad} different")
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
