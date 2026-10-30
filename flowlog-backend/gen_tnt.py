#!/usr/bin/env python3
"""Generate a synthetic taint-front (.tnt) program for scaling benchmarks.

    gen_tnt.py --funcs 500 --stmts 12 --seed 1 -o bench.tnt

Functions f0..fN-1 take two parameters and perform field reads/writes,
direct calls (mostly to later functions, occasionally backwards, which
creates recursion), calls through function-valued fields, global reads and
writes, and rare sources, sinks and sanitizers. Main calls the first
functions with tainted arguments and sinks their results.
"""
from __future__ import annotations

import argparse
import random

LABELS = ["Network", "File", "Thing"]


def gen(funcs: int, stmts: int, fields: int, globals_: int, locals_: int,
        fanout: int, back: float, seed: int) -> str:
    rnd = random.Random(seed)
    out = [f"var g{i};" for i in range(globals_)]
    out.append("def id(x) {\n  return x;\n}")
    flds = [f"f{i}" for i in range(fields)]

    def path(v: str) -> str:
        k = rnd.choice([0, 0, 1, 1, 2])
        return v + "".join("." + rnd.choice(flds) for _ in range(k))

    def callee(i: int) -> str:
        if i + 1 < funcs and rnd.random() > back:
            return f"f{rnd.randint(i + 1, min(funcs - 1, i + fanout))}"
        return f"f{rnd.randint(0, max(0, i - 1))}" if i else "id"

    for i in range(funcs):
        vals = ["a", "b"]
        body = []
        for _ in range(stmts):
            t = f"t{rnd.randrange(locals_)}"
            src = rnd.choice(vals)
            r = rnd.random()
            if r < 0.25:
                body.append(f"{path(t)} = {path(src)};")
            elif r < 0.45:
                body.append(f"{t} = {callee(i)}({path(src)}, {rnd.choice(vals)});")
            elif r < 0.55:
                fp = f"t{rnd.randrange(locals_)}"
                body.append(f"{fp}.func = {callee(i)};")
                body.append(f"{t} = {fp}.func({src}, {rnd.choice(vals)});")
            elif r < 0.65 and globals_:
                g = f"g{rnd.randrange(globals_)}"
                body.append(f"{g} = {src};" if rnd.random() < 0.5 else f"{t} = {g};")
            elif r < 0.67:
                body.append(f"{t} = source({rnd.choice(LABELS)});")
            elif r < 0.69:
                body.append(f"sink({src}, {rnd.choice(LABELS)});")
            elif r < 0.70:
                body.append(f"{t} = sanitize({src}, {rnd.choice(LABELS)});")
            else:
                body.append(f"b.{rnd.choice(flds)} = {path(src)};")
            if t not in vals:
                vals.append(t)
        body.append(f"return {rnd.choice(vals)};")
        out.append(f"def f{i}(a, b) {{\n  " + "\n  ".join(body) + "\n}")
    main = []
    for j in range(min(funcs, 8)):
        lbl = rnd.choice(LABELS)
        main += [f"s{j} = source({lbl});", f"x{j} = f{j}(s{j}, s{j});",
                 f"sink(x{j}, {lbl});", f"sink(s{j}.{rnd.choice(flds)}, {lbl});"]
    out.append("def Main() {\n  " + "\n  ".join(main) + "\n}")
    return "\n\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--funcs", type=int, default=200)
    ap.add_argument("--stmts", type=int, default=10)
    ap.add_argument("--fields", type=int, default=4)
    ap.add_argument("--globals", type=int, default=3)
    ap.add_argument("--locals", type=int, default=5)
    ap.add_argument("--fanout", type=int, default=20, help="direct calls go up to N functions ahead")
    ap.add_argument("--back", type=float, default=0.05, help="probability of a backward (recursive) call")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-o", "--output", required=True)
    a = ap.parse_args()
    with open(a.output, "w") as fh:
        fh.write(gen(a.funcs, a.stmts, a.fields, a.globals, a.locals, a.fanout, a.back, a.seed))


if __name__ == "__main__":
    main()
