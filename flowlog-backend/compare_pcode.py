#!/usr/bin/env python3
"""compare_db.py on two P-code DBs, with the address ids that pcode_lang.dl
computes with `ord` (engine-specific symbol ids) renamed in both DBs first:
each id is `ord(x)` of the `x` of the DB's `CSourceInfo_Location(x, 1, id)` row
(the rules that emit an id emit that row too) and is renamed `@x`.

    compare_pcode.py A.db B.db [compare_db.py options]

typeprop.dl's CVar_Type and VarnodePointerToAggregate depend on the order of
the `ord` ids, and through them, on larger programs, the data-flow IR; they are
compared as they are. They equal Soufflé's where FlowLog numbers the facts'
symbols in Soufflé's order, as on curl and nginx; ord_oracle.sh checks them
against Soufflé's results under FlowLog's numbering otherwise."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare_db as c  # noqa: E402

# relation -> column holding an address id
ID_COLS = {
    "CSourceInfo_Address": 0,
    "CAddress_AbsoluteAddress": 0,
    "CAddress_Kind": 0,
    "CAddress_Name": 0,
    "CAddress_FullyQualifiedName": 0,
    "CSourceInfo_Location": 2,
}
orig_rows = c.rows
maps: dict = {}


def build(db) -> dict:
    ids: dict = {}
    for x, f, i in orig_rows(db, "CSourceInfo_Location"):
        if f != "1" or ids.setdefault(i, "@" + x) != "@" + x:
            sys.exit(f"unexpected CSourceInfo_Location row {x, f, i}")
    if len(set(ids.values())) != len(ids):
        sys.exit("address id renaming is not injective")
    print(f"renamed {len(ids)} address ids", file=sys.stderr)
    return ids


def rows(db, name):
    if name not in ID_COLS:
        return orig_rows(db, name)
    if id(db) not in maps:
        maps[id(db)] = build(db)
    ids, col = maps[id(db)], ID_COLS[name]
    out = []
    for r in orig_rows(db, name):
        if r[col] not in ids:
            sys.exit(f"{name} row {r} has an unknown address id")
        out.append(r[:col] + (ids[r[col]],) + r[col + 1 :])
    return out


c.rows = rows
sys.exit(c.main())
