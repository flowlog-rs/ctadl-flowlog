#!/usr/bin/env python3
"""Check that a FlowLog database is a choice model of a CTADL analysis.

Souffle keeps, for each `choice-domain` key, the first candidate its
evaluation order derives. When a key gets two candidates from the same clause
in the same iteration, the port breaks the tie differently (README,
Limitations), and the two databases then differ in the choice relations and
in everything derived from them. Both are correct if each is a choice model of
the program. This tool checks that for a FlowLog database: it makes Souffle
take FlowLog's choices and checks that Souffle then derives exactly the
FlowLog database.

The program is rewritten so that, for every choice relation R (other than the
`--functional` ones, whose dependent columns are functions of the key), R's
clauses derive `R_cand` instead, and

    R(x..) :- R_cand(x..), FlChosen_R(x..).
    R(x..) :- R_cand(x..), !FlKey_R(keys..).

where `FlChosen_R` holds FlowLog's rows of R and `FlKey_R` their keys. The
rewritten program runs in Souffle's interpreter on the same facts. If its
outputs equal the FlowLog database, then every FlowLog witness is derivable
(bottom-up, so without circular support), no key that FlowLog left out has a
candidate, and every other relation is exactly what the program derives from
these choices: the FlowLog database is one of the program's choice models.

Every choice relation that is forced must be in the FlowLog database, so run
the index with `--all-outputs`.

When the program's results depend on `ord` (JADX numbers allocation sites,
unnamed variables and intent keys by it), Souffle must also number symbols as
FlowLog did: `--ord-map TSV` (symbol, number; `compare_jadx.py ords` writes it
for JADX) replaces each `ord(x)` by `@oord(x)` (ord_oracle.cpp), which returns
the map's number for x and numbers other symbols past the map's.

  check_choice_model.py PROGRAM.dl FACTS_DIR FLOWLOG_DB OUT_DIR [-j N]
                        [--ord-map TSV]
    PROGRAM.dl   the preprocessed analysis (index.dl in the CTADL directory)
    FACTS_DIR    its fact directory (facts/ in the CTADL directory)
    FLOWLOG_DB   ctadlir.db of the FlowLog run
Exit status 0 when the databases are equal.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_LIBDIR = Path.home() / ".local/state/ctadl/analysis/0.14.1"
DIRECTIVES = {"decl", "input", "output", "printsize", "limitsize", "type", "functor",
              "comp", "init", "override", "pragma", "plan", "include", "once"}
# outputs that CTADL's driver also writes to (metadata rows such as
# CTADL_Index_Models); they do not depend on choices
DRIVER_WRITTEN = {"CTADLConfig"}


def mask(text: str) -> str:
    """`text` with comments and string literal contents blanked (same length)."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                out[k] = " "
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def matching(m: str, i: int) -> int:
    """Index of the bracket closing the one at `i`."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack = []
    for j in range(i, len(m)):
        if m[j] in pairs:
            stack.append(pairs[m[j]])
        elif stack and m[j] == stack[-1]:
            stack.pop()
            if not stack:
                return j
    raise SystemExit(f"unbalanced bracket at offset {i}")


def is_directive_dot(m: str, i: int) -> bool:
    """`m[i]` is the dot of a directive such as `.decl` or `.output`."""
    w = re.match(r"\.([a-z]+)\b", m[i:])
    return w is not None and w.group(1) in DIRECTIVES and (i == 0 or m[i - 1] in " \t\r\n{}")


def is_head(m: str, p: int) -> bool:
    """The atom starting at `p` is a clause head: scanning backwards, the
    previous statement ends before any `:-`."""
    depth = 0
    i = p - 1
    while i >= 0:
        c = m[i]
        if c in ")]}":
            depth += 1
        elif c in "([{":
            if depth == 0:
                if c == "{" and not re.search(r":\s*$", m[:i]):
                    return True  # opening brace of a component
            else:
                depth -= 1
        elif depth == 0:
            if c == "-" and i > 0 and m[i - 1] == ":":
                return False
            if c == "." and (i + 1 == len(m) or m[i + 1].isspace()):
                return True  # end of the previous clause
            if c == "." and is_directive_dot(m, i):
                return True
        i -= 1
    return True


class Program:
    def __init__(self, text: str):
        self.text = text
        self.m = mask(text)
        m = self.m
        # component bodies: name -> (start, end) offsets of `{`..`}`
        self.comps: dict[str, tuple[int, int]] = {}
        for c in re.finditer(r"\.comp\s+(\w+)[^{]*\{", m):
            self.comps[c.group(1)] = (c.end() - 1, matching(m, c.end() - 1))
        self.inits: dict[tuple, str] = {}  # (scope, instance) -> component
        for c in re.finditer(r"\.init\s+(\w+)\s*=\s*(\w+)", m):
            self.inits[(self.scope_at(c.start()), c.group(1))] = c.group(2)
        self.decls: dict[tuple, int] = {}  # (scope, name) -> offset of `.decl`
        for d in re.finditer(r"\.decl\s+(\w+)\s*\(", m):
            self.decls[(self.scope_at(d.start()), d.group(1))] = d.start()

    def scope_at(self, pos: int) -> str | None:
        best = None
        for name, (s, e) in self.comps.items():
            if s < pos < e and (best is None or s > self.comps[best][0]):
                best = name
        return best

    def scopes_at(self, pos: int) -> list:
        """The components enclosing `pos`, innermost first, then None."""
        out = [self.scope_at(pos)]
        while out[-1] is not None:
            out.append(self.scope_at(self.comps[out[-1]][0]))
        return out

    def resolve(self, name: str, pos: int) -> tuple:
        """(component, relation) that `name`, used at `pos`, denotes: a name
        is the innermost enclosing scope's, and `i.R` is R of the component
        that instance i, of the innermost scope that has one, instantiates."""
        *path, base = name.split(".")
        scopes = self.scopes_at(pos)
        if not path:
            return next(((s, base) for s in scopes if (s, base) in self.decls), (None, base))
        comp = next((self.inits[(s, path[0])] for s in scopes if (s, path[0]) in self.inits), None)
        for inst in path[1:]:
            comp = self.inits.get((comp, inst)) if comp is not None else None
        if comp is None:
            raise SystemExit(f"cannot resolve {name} at offset {pos}")
        return (comp, base)

    def instances(self, comp: str) -> list[str]:
        """The full names of the instances of a component."""
        return [inst if scope is None else f"{outer}.{inst}"
                for (scope, inst), c in self.inits.items() if c == comp
                for outer in ([None] if scope is None else self.instances(scope))]

    def choice_relations(self) -> list[dict]:
        out = []
        for (scope, name), pos in self.decls.items():
            m = self.m
            lp = m.index("(", pos)
            rp = matching(m, lp)
            cd = re.match(r"(?:\s*(?:inline|no_inline|magic|no_magic|brie|btree|btree_delete|eqrel|"
                          r"override|overridable)\b)*\s*choice-domain\s*\(", m[rp + 1:])
            if cd is None:
                continue
            klp = rp + 1 + cd.end() - 1
            krp = matching(m, klp)
            if re.match(r"\s*,\s*\(", m[krp + 1:]):
                raise SystemExit(f"{name}: several choice-domain key sets are not supported")
            attrs = [" ".join(a.split()) for a in m[lp + 1:rp].split(",")]
            cols = [a.split(":")[0].strip() for a in attrs]
            keys = [k.strip() for k in m[klp + 1:krp].split(",")]
            out.append({"scope": scope, "name": name, "attrs": attrs, "cols": cols, "keys": keys,
                        "key_attrs": [attrs[cols.index(k)] for k in keys], "end": krp + 1})
        return out

    def output_names(self) -> list[str]:
        names = []
        for o in re.finditer(r"\.output\s+(\w+)", self.m):
            scope = self.scope_at(o.start())
            if scope is None:
                names.append(o.group(1))
            else:
                names += [f"{inst}.{o.group(1)}" for inst in self.instances(scope)]
        return names

    def rewrite(self, rels: list[dict], oord: bool = False) -> tuple[str, int]:
        """The program with the given choice relations restricted, and with
        `ord` replaced by the functor `oord` if asked; also the number of
        renamed clause heads."""
        ids = {(r["scope"], r["name"]): r for r in rels}
        edits = []  # (offset, length, replacement)
        if oord:
            edits.append((0, 0, ".functor oord(x: symbol): number\n"))
            edits += [(o.start(), 3, "@oord") for o in re.finditer(r"(?<![\w.@])ord(?=\s*\()", self.m)]
        names = sorted({r["name"] for r in rels})
        pat = re.compile(r"(?<![\w.@])((?:\w+\.)*(?:%s))\s*\(" % "|".join(map(re.escape, names)))
        heads = 0
        for a in pat.finditer(self.m):
            if re.search(r"\.(decl|output|input|printsize|limitsize)\s+$", self.m[:a.start()]):
                continue
            if self.resolve(a.group(1), a.start()) in ids and is_head(self.m, a.start()):
                edits.append((a.start() + len(a.group(1)), 0, "_cand"))
                heads += 1
        for r in rels:
            n, cols, keys = r["name"], ", ".join(r["cols"]), ", ".join(r["keys"])
            attrs, key_attrs = ", ".join(r["attrs"]), ", ".join(r["key_attrs"])
            edits.append((r["end"], 0, "\n"
                          f".decl {n}_cand({attrs})\n"
                          f".decl FlChosen_{n}({attrs})\n"
                          f".input FlChosen_{n}\n"
                          f".decl FlKey_{n}({key_attrs})\n"
                          f"FlKey_{n}({keys}) :- FlChosen_{n}({cols}).\n"
                          f"{n}({cols}) :- {n}_cand({cols}), FlChosen_{n}({cols}).\n"
                          f"{n}({cols}) :- {n}_cand({cols}), !FlKey_{n}({keys}).\n"))
        text = self.text
        for off, ln, rep in sorted(edits, key=lambda e: e[0], reverse=True):
            text = text[:off] + rep + text[off + ln:]
        return text, heads


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("program", type=Path)
    ap.add_argument("facts", type=Path)
    ap.add_argument("flowlog_db", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("-j", "--jobs", type=int, default=8)
    ap.add_argument("--functional", action="append", default=["ReachableVertex"],
                    help="choice relations whose dependent columns are functions of the key (not forced)")
    ap.add_argument("--libdir", type=Path, default=DEFAULT_LIBDIR, help="directory of libfunctors.so")
    ap.add_argument("--ord-map", type=Path, help="FlowLog's `ord` of each symbol (TSV: symbol, number)")
    ap.add_argument("--schema", action="append", default=[], help="passed to compare_db.py")
    args = ap.parse_args()

    prog = Program(args.program.read_text())
    rels = [r for r in prog.choice_relations() if r["name"] not in args.functional]
    text, heads = prog.rewrite(rels, oord=args.ord_map is not None)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "restricted.dl").write_text(text)
    env = dict(os.environ)
    if args.ord_map:
        # CTADL's functors and oord in one library, as ord_oracle.sh builds it
        inc = Path(shutil.which("souffle") or "souffle").resolve().parent.parent / "include"
        subprocess.run(["c++", "-O2", "--std=c++17", "-shared", "-fPIC", "-I", str(inc),
                        "-I", str(inc / "souffle"), str(HERE.parent / "src/ctadl/souffle-logic/functors.cpp"),
                        str(HERE / "ord_oracle.cpp"), "-o", str(args.out / "libfunctors.so")], check=True)
        args.libdir = args.out
        env["OORD_MAP"] = str(args.ord_map.resolve())

    facts = args.out / "facts"
    facts.mkdir(exist_ok=True)
    for f in args.facts.iterdir():
        link = facts / f.name
        if not link.exists():
            os.symlink(f.resolve(), link)
    db = sqlite3.connect(args.flowlog_db)
    have = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    for r in rels:
        insts = [""] if r["scope"] is None else [f"{i}." for i in prog.instances(r["scope"])]
        for inst in insts:
            rel = f"{inst}{r['name']}"
            if rel not in have:
                raise SystemExit(f"{rel} is not in {args.flowlog_db}; run the index with --all-outputs")
            rows = db.execute(f'SELECT * FROM "{rel}"').fetchall()
            with open(facts / f"{inst}FlChosen_{r['name']}.facts", "w") as fh:
                for row in rows:
                    vals = ["" if v is None else str(v) for v in row]
                    if any("\t" in v or "\n" in v for v in vals):
                        raise SystemExit(f"{rel}: a value contains a tab or newline")
                    fh.write("\t".join(vals) + "\n")
            print(f"forced   {rel}: {len(rows)} FlowLog witnesses")
    print(f"renamed  {heads} clause heads")

    dbdir = args.out / "db"
    dbdir.mkdir(exist_ok=True)
    (dbdir / "ctadlir.db").unlink(missing_ok=True)
    cmd = ["souffle", "-F", str(facts), "-D", str(dbdir), "-L", str(args.libdir), "-l", "functors",
           "-j", str(args.jobs), str(args.out / "restricted.dl")]
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    (args.out / "souffle.log").write_text(res.stdout + res.stderr)
    if res.returncode != 0:
        print(res.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"souffle failed ({res.returncode}), see {args.out / 'souffle.log'}")

    only = [a for n in prog.output_names() if n not in DRIVER_WRITTEN for a in ("--only", n)]
    schema = [a for s in args.schema for a in ("--schema", s)]
    cmp = subprocess.run([sys.executable, str(HERE / "compare_db.py"), str(dbdir / "ctadlir.db"),
                          str(args.flowlog_db), "-q", "--keys", "VirtualAssign=0,1,2,3,4,5,6"]
                         + schema + only, capture_output=True, text=True)
    (args.out / "compare.txt").write_text(cmp.stdout + cmp.stderr)
    print(cmp.stdout, end="")
    ok = cmp.returncode == 0 and "WITNESS" not in cmp.stdout
    print("choice model: " + ("yes, Souffle forced to FlowLog's choices derives the FlowLog database"
                              if ok else "NOT confirmed"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
