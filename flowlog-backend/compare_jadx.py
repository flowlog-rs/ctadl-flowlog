#!/usr/bin/env python3
"""compare_db.py on two JADX DBs, with the ids that JADX's rules compute with
`ord` (engine-specific symbol ids) renamed in both DBs first:

* jadx_lang.dl's MakeObjectID names the object of a `_TypeInstance(_, var,
  type)` variable `type/<ord(var)>`: the `obj` of each DB's `CisAlloc(var, "",
  obj)` rows (in JADX, that rule's only) is renamed `type/@var`.
* android-intents.dl's MakeIntentKeyField names a key's field
  `.intent[dtype].<ord(key)>`: each id is renamed to the instructions of the
  `CInsn_ModelAssign` rows that use it (each put/get uses one key).

    compare_jadx.py A.db B.db [compare_db.py options]
    compare_jadx.py canon DB FILE   prints FILE (SARIF, GML) renamed by DB's ids
    compare_jadx.py ords DB         prints the engine's number of each symbol
                                    whose `ord` these rules take, and of the
                                    variables jadx_lang.dl names
                                    `autovar<ord(var)>` (for
                                    check_choice_model.py --ord-map)"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare_db as c  # noqa: E402

KEY = re.compile(r"\.intent\[([^\]]*)\]\.(\d+)")
orig_rows = c.rows
maps: dict = {}


def build(db) -> tuple:
    objs: dict = {}
    rels = c.relations(db)
    if "CisAlloc" in rels:
        for var, path, obj in orig_rows(db, "CisAlloc"):
            mt = re.fullmatch(r"(.*)/(\d+)", obj)
            if path == "" and mt:
                new = f"{mt.group(1)}/@{var}"
                if objs.setdefault(obj, new) != new:
                    sys.exit(f"object {obj} allocated by two variables")
    if len(set(objs.values())) != len(objs):
        sys.exit("object renaming is not injective")
    users: dict = {}
    if "CInsn_ModelAssign" in rels:
        for r in orig_rows(db, "CInsn_ModelAssign"):
            for cell in r[1:]:
                for m in KEY.finditer(cell):
                    users.setdefault(m.group(2), set()).add(r[0])
    keys = {n: "{" + ",".join(sorted(s)) + "}" for n, s in users.items()}
    if len(set(keys.values())) != len(keys):
        sys.exit("intent key renaming is not injective")
    print(f"renamed {len(objs)} MakeObjectID objects, {len(keys)} intent keys", file=sys.stderr)
    alts = sorted(objs, key=len, reverse=True)
    obj_re = re.compile("|".join(re.escape(o) + r"(?!\d)" for o in alts)) if objs else None
    return obj_re, objs, keys


def ords(db) -> dict:
    """{symbol: number} read back from the rows that the ids are part of. An
    intent key is paired with its number through the put/get instruction
    that uses it; an instruction whose key variable references several
    strings is paired once the other instructions have paired all but one."""
    out: dict = {}

    def put(sym: str, n: int) -> None:
        if out.setdefault(sym, n) != n:
            sys.exit(f"{sym!r} has two numbers, {out[sym]} and {n}")

    rels = c.relations(db)
    get = lambda r: orig_rows(db, r) if r in rels else []  # noqa: E731
    for var, path, obj in get("CisAlloc"):
        mt = re.fullmatch(r".*/(\d+)", obj)
        if path == "" and mt:
            put(var, int(mt.group(1)))
    for var, name in get("CVar_Name"):
        mt = re.fullmatch(r"autovar(\d+)", name)
        if mt:
            put(var, int(mt.group(1)))
    strs: dict = {}
    for key, s in get("VarReferencesString"):
        strs.setdefault(key, set()).add(s)
    pending: dict = {}  # instruction -> ([its key strings], {their numbers})
    for rel in ("IntentPutExtra", "IntentGetExtra"):
        for r in get(rel):
            pending.setdefault(r[0], (set(), set()))[0].update(strs.get(r[1], ()))
    for r in get("CInsn_ModelAssign"):
        for cell in r[1:]:
            for m in KEY.finditer(cell):
                pending.setdefault(r[0], (set(), set()))[1].add(int(m.group(2)))
    while True:
        paired = 0
        for insn, (ss, ns) in list(pending.items()):
            ss -= out.keys()
            ns -= set(out.values())
            if len(ss) == len(ns) == 1:
                put(ss.pop(), ns.pop())
                paired += 1
            if not ns:
                del pending[insn]
        if not paired:
            break
    if pending:
        sys.exit(f"cannot pair the intent keys of {len(pending)} instructions, e.g. {next(iter(pending))}")
    if len(set(out.values())) != len(out):
        sys.exit("two symbols have the same number")
    return out


def rename(text: str, maps: tuple) -> str:
    obj_re, objs, keys = maps
    if obj_re is not None:
        text = obj_re.sub(lambda m: objs[m.group(0)], text)
    return KEY.sub(
        lambda m: f".intent[{m.group(1)}].{keys.get(m.group(2), '?' + m.group(2))}", text
    )


def rows(db, name):
    if id(db) not in maps:
        maps[id(db)] = build(db)
    return [tuple(rename(v, maps[id(db)]) for v in r) for r in orig_rows(db, name)]


if len(sys.argv) == 3 and sys.argv[1] == "ords":
    import sqlite3

    for sym, n in ords(sqlite3.connect(sys.argv[2])).items():
        if "\t" in sym or "\n" in sym:
            sys.exit(f"symbol {sym!r} contains a tab or newline")
        print(f"{sym}\t{n}")
    sys.exit(0)
if len(sys.argv) == 4 and sys.argv[1] == "canon":
    import sqlite3

    sys.stdout.write(rename(open(sys.argv[3]).read(), build(sqlite3.connect(sys.argv[2]))))
    sys.exit(0)
c.rows = rows
sys.exit(c.main())
