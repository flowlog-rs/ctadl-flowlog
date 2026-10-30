#!/usr/bin/env python3
"""Repeated benchmark runs, and their medians.

    bench_reps.py run --out DIR [--reps 3] [--jobs 1,32] [--backend BACKEND,...] PROG.tnt...
    bench_reps.py medians [--before BENCH_DIR] DIR...

`run` times the backends as bench.py does, several times over and without
comparing databases. It imports each program once. Then, for each rep R,
program, backend (default souffle,flowlog) and worker count, it indexes and
queries a fresh copy of the facts in DIR/rR/<program>/<backend>-j<workers>,
through bench.py's shim and with its logs. Even reps run the backends in
reverse order. DIR/rR/summary.txt has bench.py's columns, with the load
average before each run in place of bench.py's diff. The runs keep their
databases (1.1 GB each on syn25) for compare_db.py. Analyses must already be
compiled, as for bench.py.

`medians` reads the reps of each DIR, whatever their backends, and prints
medians over the reps: engine and wall seconds, peak RSS, FlowLog's split of
its time (from the logs), ratios between the backends, the effect of the
hints, and the speedup from the fewest workers to the most. --before compares
the medians with an earlier single bench.py run (its --out directory).
"""
from __future__ import annotations

import argparse
import re
import shutil
import statistics
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import bench

ROW = "{:10} {:%d} {:>3} | {:>7} {:>8} {:>7} {:>7} | {:>7} {:>8} {:>7} {:>7} | {}"
TITLES = {"souffle": "Soufflé, CTADL's hints", "flowlog": "FlowLog, their version 0",
          "souffle-noplan": "Soufflé, no hints", "flowlog-noplan": "FlowLog, no hints",
          "souffle-strict": "Soufflé, strict"}
# (Soufflé backend, FlowLog backend) for the ratios, (without, with hints) for their effect
PAIRS = (("souffle", "flowlog"), ("souffle-noplan", "flowlog-noplan"), ("souffle", "flowlog-noplan"))
HINTS = (("souffle-noplan", "souffle"), ("flowlog-noplan", "flowlog"))


def cols(r: dict) -> list:
    return [f"{r['wall']:.2f}", f"{r.get('analyze', 0):.2f}", f"{r.get('engine') or 0:.2f}", f"{r.get('rss', 0)}M"]


def run_reps(args: argparse.Namespace) -> None:
    out = args.out.resolve()
    progs = [p.resolve() for p in args.programs]
    jobs = [int(j) for j in args.jobs.split(",")]
    backends = args.backend.split(",")
    for b in backends:
        if b not in bench.BACKENDS:
            sys.exit(f"unknown backend {b!r}")
    fmt = ROW % max(len("backend"), *map(len, backends))
    for prog in progs:
        facts = out / prog.stem / "facts"
        facts.mkdir(parents=True, exist_ok=True)
        bench.run(["ctadl", "import", "taint-front", prog, "-o", facts, "-f"], out / prog.stem / "import.log")
    for rep in range(1, args.reps + 1):
        rd = out / f"r{rep}"
        rd.mkdir(parents=True, exist_ok=True)
        with open(rd / "summary.txt", "w") as summary:
            print(fmt.format("program", "backend", "j", "idx", "analyze", "engine", "rss",
                             "query", "analyze", "engine", "rss", "load"), file=summary, flush=True)
            for prog in progs:
                for b in (backends if rep % 2 else backends[::-1]):
                    for j in jobs:
                        d = rd / prog.stem / f"{b}-j{j}"
                        shutil.rmtree(d, ignore_errors=True)
                        shutil.copytree(out / prog.stem / "facts", d)
                        load = Path("/proc/loadavg").read_text().split()[0]
                        drv = [sys.executable, bench.HERE / "bench.py", "--shim", b]
                        _, ix = bench.run(drv + ["--directory", d, "index", "-f", "-j", j], d / "index.log")
                        _, q = bench.run(drv + ["--directory", d, "query", "-j", j, "--format", "summary",
                                                "-o", d / "summary.txt"], d / "query.log")
                        line = fmt.format(prog.stem, b, j, *cols(ix), *cols(q), f"load={load}")
                        print(line, file=summary, flush=True)
                        print(f"r{rep} {line}", flush=True)


def parse(rep: Path) -> dict:
    """The rows of a summary.txt (bench.py's or run's), with the step times in the runs' logs."""
    rows = {}
    for line in (rep / "summary.txt").read_text().splitlines()[1:]:
        left, idx, q, _ = [s.strip() for s in line.split("|")]
        prog, backend, j = left.split()
        iw, ia, ie, ir = idx.split()
        qw, qa, qe, qr = q.split()
        r = dict(iw=float(iw), ia=float(ia), ie=float(ie), ir=int(ir[:-1]),
                 qw=float(qw), qa=float(qa), qe=float(qe), qr=int(qr[:-1]))
        for ph in ("index", "query"):
            log = rep / prog / f"{backend}-j{j}" / f"{ph}.log"
            if not log.exists():
                continue
            text = log.read_text()
            for k, v in re.findall(r"\[flowlog\] ([a-zA-Z ]+): ([\d.]+)s", text):
                r[f"{ph}:{k}"] = float(v)
            for k, v in re.findall(r"\[bench\] (after_index|after_query|visualize_query_results)=([\d.]+)", text):
                r[f"{ph}:{k}"] = float(v)
            m = re.search(r"\[flowlog\]\s+([\d.]+)(ms|s):\s*Dataflow executed", text)
            if m:
                r[f"{ph}:dataflow"] = float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)
            m = re.findall(r"\[bench\] analyze=([\d.]+) rss=", text)
            if m:  # one more decimal than summary.txt; Soufflé's engine time is its analyze time
                a, e = ("ia", "ie") if ph == "index" else ("qa", "qe")
                if r[e] == r[a]:
                    r[e] = float(m[-1])
                r[a] = float(m[-1])
        if "index:dataflow" in r and "query:dataflow" in r:
            r["iwrite"] = r["ie"] - r["index:dataflow"]
            r["qwrite"] = r["qe"] - r["query:dataflow"]
        rows[(prog, backend, int(j))] = r
    return rows


def r1(x: float) -> str:
    return str(Decimal(repr(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def mb(x: float) -> str:
    return f"{r1(round(x / 1024, 4))} GB" if x >= 1000 else f"{int(x)} MB"


def medians(args: argparse.Namespace) -> None:
    med, per_rep = {}, {}
    for d in args.dirs:
        reps = sorted(d.glob("r[0-9]*"), key=lambda p: int(p.name[1:]))
        runs = [parse(r) for r in reps]
        print(f"{d}: reps {[r.name for r in reps]}")
        for k in runs[0]:
            med[k] = {m: statistics.median(run[k][m] for run in runs) for m in runs[0][k]}
            per_rep[k] = [(run[k]["ie"], run[k]["qe"]) for run in runs]
    progs = list(dict.fromkeys(p for p, _, _ in med))
    jobs = sorted({j for _, _, j in med})
    backends = list(dict.fromkeys(b for _, b, _ in med))
    present = [b for b in TITLES if b in backends] + [b for b in backends if b not in TITLES]
    workers = " / ".join(map(str, jobs))

    def cell(p: str, b: str, key: str, f) -> str:
        return " / ".join(f(med[(p, b, j)][key]) if (p, b, j) in med else "-" for j in jobs)

    print("\n## Per-rep engine seconds, index / query")
    for k, v in per_rep.items():
        print(f"{k[0]} {k[1]:15} j{k[2]:<2}", "   ".join(f"{i:6.2f} / {q:6.2f}" for i, q in v))

    for title, key, f in (("Engine seconds", "e", r1), ("Wall seconds", "w", r1), ("Peak RSS", "r", mb)):
        print(f"\n## {title}, medians, {workers} workers")
        print("| Program | Phase | " + " | ".join(TITLES.get(b, b) for b in present) + " |")
        print("|---|---|" + "---|" * len(present))
        for p in progs:
            for ph in ("i", "q"):
                cells = [cell(p, b, ph + key, f) for b in present]
                print(f"| {p} | {'index' if ph == 'i' else 'query'} | " + " | ".join(cells) + " |")

    print("\n## FlowLog's time, medians: engine = dataflow (load and compute) + write to sqlite; "
          "the query's conversions")
    for b in present:
        for p in progs:
            for j in jobs:
                r = med.get((p, b, j), {})
                if b.startswith("flowlog") and "qwrite" in r:
                    print(f"{b:15} {p} j{j:<2}: index dataflow {r['index:dataflow']:.2f} write {r['iwrite']:.2f}; "
                          f"query dataflow {r['query:dataflow']:.2f} write {r['qwrite']:.2f} "
                          f"convert in {r['query:prepare inputs']:.2f} out {r['query:write results']:.2f}")

    print("\n## CTADL's steps after the analysis, medians: after_index; after_query, report")
    for b in present:
        for p in progs:
            for j in jobs:
                r = med.get((p, b, j), {})
                if "query:after_query" in r:
                    print(f"{b:15} {p} j{j:<2}: {r['index:after_index']:.2f}; {r['query:after_query']:.2f}, "
                          f"{r['query:visualize_query_results']:.2f}")

    print("\n## Ratios of medians (Soufflé engine / FlowLog engine; > 1 means FlowLog is faster)")
    for sb, fb in PAIRS:
        for p in progs:
            for j in jobs:
                if (p, sb, j) in med and (p, fb, j) in med:
                    s, f = med[(p, sb, j)], med[(p, fb, j)]
                    print(f"{sb:14} / {fb:14} {p} j{j:<2}: engine index {s['ie'] / f['ie']:.2f}x query "
                          f"{s['qe'] / f['qe']:.2f}x | wall FL/S index {f['iw'] / s['iw']:.2f} query "
                          f"{f['qw'] / s['qw']:.2f} | RSS FL/S index {f['ir'] / s['ir']:.1f} query {f['qr'] / s['qr']:.1f}")

    print("\n## Hints' effect (no hints / hints, engine)")
    for a, b in HINTS:
        for p in progs:
            for j in jobs:
                if (p, a, j) in med and (p, b, j) in med:
                    x, y = med[(p, a, j)], med[(p, b, j)]
                    print(f"{a:15} {p} j{j:<2}: index {x['ie'] / y['ie']:.2f}x query {x['qe'] / y['qe']:.2f}x")

    print(f"\n## j{jobs[0]} -> j{jobs[-1]} engine speedup")
    for b in present:
        for p in progs:
            if (p, b, jobs[0]) in med and (p, b, jobs[-1]) in med:
                a, z = med[(p, b, jobs[0])], med[(p, b, jobs[-1])]
                print(f"{b:15} {p}: index {a['ie'] / z['ie']:.2f}x query {a['qe'] / z['qe']:.2f}x")

    if not args.before:
        return
    old = parse(args.before)
    print("\n## Before (one bench.py run) -> now (medians): engine index; query; RSS index / query (MB)")
    for k, n in med.items():
        if k in old:
            o = old[k]
            df = ""
            if "query:dataflow" in o and "query:dataflow" in n:
                df = (f"; dataflow {o['index:dataflow']:.2f} -> {n['index:dataflow']:.2f} / "
                      f"{o['query:dataflow']:.2f} -> {n['query:dataflow']:.2f}")
            print(f"{k[0]} {k[1]:15} j{k[2]:<2}: index {o['ie']:.2f} -> {n['ie']:.2f}; query {o['qe']:.2f} -> "
                  f"{n['qe']:.2f}; RSS {o['ir']} -> {n['ir']:.0f} / {o['qr']} -> {n['qr']:.0f}{df}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="time the backends several times")
    r.add_argument("programs", nargs="+", type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--jobs", default="1,32")
    r.add_argument("--backend", default="souffle,flowlog",
                   help=f"comma-separated list of {', '.join(bench.BACKENDS)}")
    m = sub.add_parser("medians", help="medians over the reps of each DIR")
    m.add_argument("dirs", nargs="+", type=Path, metavar="DIR")
    m.add_argument("--before", type=Path, help="an earlier bench.py run's --out directory")
    args = ap.parse_args()
    run_reps(args) if args.command == "run" else medians(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
