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
    compare_jadx.py canon DB FILE   prints FILE (SARIF, GML) renamed by DB's ids"""
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


if len(sys.argv) == 4 and sys.argv[1] == "canon":
    import sqlite3

    sys.stdout.write(rename(open(sys.argv[3]).read(), build(sqlite3.connect(sys.argv[2]))))
    sys.exit(0)
c.rows = rows
sys.exit(c.main())
