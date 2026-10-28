#!/usr/bin/env python3
"""Benchmark CTADL's index and query phases with the Souffle and FlowLog backends.

    bench.py [--jobs 1,4,16] [--backend BACKEND,...|both]
             [--format summary|sarif] [--out DIR] [--prune | --drop-dbs] PROG.tnt...

Each program is imported once (taint-front), then indexed and queried by
every backend and worker count in a fresh copy of the facts. The query writes
CTADL's text summary; with --format sarif it writes a SARIF report instead,
which can take longer than the analysis and hundreds of MB.
Backends: souffle, souffle-noplan, souffle-strict, flowlog; both (the default)
is souffle,flowlog. The souffle-* variants run CTADL's program without its
`.plan` hints, which the FlowLog translation drops: souffle-noplan with
Souffle's default SIPS (all-bound), souffle-strict with `.pragma "RamSIPS"
"strict"`, which joins every rule version in the written order, as FlowLog's
planner does (bar cross-product avoidance and semijoin folding).
Reported per phase:
  wall     the whole `ctadl` command (Python driver, model generation, views)
  analyze  the backend call: Souffle's compiled analysis, or FlowLog's input
           conversion + run + result conversion
  engine   FlowLog's binary alone (Souffle's analyze is its binary alone)
  rss      peak RSS of the largest child process (the engine)
Results of all runs are diffed against the first one (compare_db.py on the
final database, which holds the query results, and compare_sarif.py on the
SARIF reports). Analyses must already be compiled (run each backend once on
a small program first).
"""
from __future__ import annotations

import argparse
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOP = HERE.parent.parent  # the workspace: the CTADL checkout's parent


PLAN = re.compile(r"\.plan\s+\d+\s*:\s*\([\d\s,]*\)(?:\s*,\s*\d+\s*:\s*\([\d\s,]*\))*")
# Souffle variants on CTADL's program without its `.plan` hints (which the
# FlowLog translation drops), with the extra source text appended.
SOUFFLE_VARIANTS = {
    "souffle-noplan": "",  # Souffle's default SIPS (all-bound)
    "souffle-strict": '\n.pragma "RamSIPS" "strict"\n',  # the written order, like FlowLog
}
BACKENDS = ["souffle", *SOUFFLE_VARIANTS, "flowlog"]


def shim(backend: str, argv: list) -> None:
    """Runs the `ctadl` CLI with timing hooks around the backend call."""
    sys.path.insert(0, str(HERE))
    import flowlog_ctadl
    cli = flowlog_ctadl.load_ctadl_cli()
    if backend == "flowlog":
        cls = flowlog_ctadl.FlowLogAnalysis
        cli.build_analysis = lambda args, src: cls(cli, args, src)
    else:
        cls = cli.SouffleCompiledAnalysis
    if backend in SOUFFLE_VARIANTS:
        build = cli.build_analysis

        def build_variant(args, src):
            text = PLAN.sub("", Path(src).read_text())
            if ".plan" in text:
                sys.exit(f"error: a .plan directive in {src} was not removed")
            out = Path(src).with_name(f"{Path(src).stem}-{backend}.dl")
            out.write_text(text + SOUFFLE_VARIANTS[backend])
            return build(args, str(out))

        cli.build_analysis = build_variant
    orig = cls.analyze

    def analyze(self, *a, **kw):
        t0 = time.time()
        r = orig(self, *a, **kw)
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
        print(f"[bench] analyze={time.time() - t0:.3f} rss={rss:.0f}", file=sys.stderr, flush=True)
        return r

    cls.analyze = analyze
    sys.argv = ["ctadl"] + argv  # the CLI's argparse reads sys.argv
    cli.main(sys.argv)


def run(cmd: list, log: Path) -> tuple[float, dict]:
    t0 = time.time()
    with open(log, "w") as f:
        f.write(f"$ {' '.join(map(str, cmd))}\n")
        f.flush()
        r = subprocess.run(list(map(str, cmd)), stdout=f, stderr=subprocess.STDOUT,
                           env=dict(os.environ, FLOWLOG_CTADL_TIMING="1"))
    wall = time.time() - t0
    if r.returncode:
        sys.exit(f"command failed (see {log}): {' '.join(map(str, cmd))}")
    text = log.read_text()
    m = re.findall(r"\[bench\] analyze=([\d.]+) rss=(\d+)", text)
    e = re.findall(r"\[flowlog\] FlowLog run: ([\d.]+)s", text)
    out = {"wall": wall}
    if m:
        out["analyze"], out["rss"] = float(m[-1][0]), int(m[-1][1])
    out["engine"] = float(e[-1]) if e else out.get("analyze")
    return wall, out


def compare(ref: Path, other: Path, name: str, schemas: list, sarif: bool) -> str:
    cmd = [sys.executable, HERE / "compare_db.py", ref / "ctadlir.db", other / "ctadlir.db", "-q",
           "--keys", "VirtualAssign=0,1,2,3,4,5,6"]
    for s in schemas:
        cmd += ["--schema", s]
    r = subprocess.run(list(map(str, cmd)), capture_output=True, text=True)
    res = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()
    if not sarif:
        return res
    r = subprocess.run([sys.executable, str(HERE / "compare_sarif.py"),
                        str(ref / f"{name}.sarif"), str(other / f"{name}.sarif")], capture_output=True, text=True)
    res += "; " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip())
    return res


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--shim":
        shim(sys.argv[2], sys.argv[3:])
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("programs", nargs="+", type=Path)
    ap.add_argument("--jobs", default="1,4,16")
    ap.add_argument("--backend", default="both",
                    help=f"comma-separated list of {', '.join(BACKENDS)}; both = souffle,flowlog")
    ap.add_argument("--out", type=Path, default=TOP / "runs" / "bench")
    ap.add_argument("--schema", action="append", default=[str(HERE / "index_schema.json"),
                                                          str(HERE / "query_schema.json")])
    ap.add_argument("--prune", action="store_true",
                    help="delete the databases of runs identical to the first one")
    ap.add_argument("--drop-dbs", action="store_true",
                    help="delete every database once compared (the first run's after the last run)")
    ap.add_argument("--format", choices=["summary", "sarif"], default="summary",
                    help="query output (default: summary); sarif reports are also compared")
    args = ap.parse_args()
    out = args.out.resolve()
    jobs = [int(j) for j in args.jobs.split(",")]
    backends = ["souffle", "flowlog"] if args.backend == "both" else args.backend.split(",")
    for b in backends:
        if b not in BACKENDS:
            ap.error(f"unknown backend {b!r}")
    width = max(len("backend"), *map(len, backends))
    fmt = "{:10} {:%d} {:>3} | {:>7} {:>8} {:>7} {:>7} | {:>7} {:>8} {:>7} {:>7} | {}" % width
    print(fmt.format("program", "backend", "j", "idx", "analyze", "engine", "rss",
                     "query", "analyze", "engine", "rss", "diff vs first run"))
    for prog in args.programs:
        prog = prog.resolve()
        name = prog.stem
        facts = out / name / "facts"
        facts.mkdir(parents=True, exist_ok=True)
        run(["ctadl", "import", "taint-front", prog, "-o", facts, "-f"], out / name / "import.log")
        ref = None
        for b in backends:
            for j in jobs:
                d = out / name / f"{b}-j{j}"
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(facts, d)
                drv = [sys.executable, __file__, "--shim", b]
                _, ix = run(drv + ["--directory", d, "index", "-f", "-j", j], d / "index.log")
                sarif = args.format == "sarif"
                report = d / (f"{name}.sarif" if sarif else "summary.txt")
                _, q = run(drv + ["--directory", d, "query", "-j", j, "--format", args.format,
                                  "-o", report], d / "query.log")
                diff = "(reference)" if ref is None else compare(ref, d, name, args.schema, sarif)
                if ref is not None and (args.drop_dbs or (
                        args.prune and ", 0 different" in diff
                        and (not sarif or diff.endswith("sarif: equal")))):
                    (d / "ctadlir.db").unlink(missing_ok=True)
                ref = ref or d

                def cols(r: dict) -> list:
                    return [f"{r['wall']:.2f}", f"{r.get('analyze', 0):.2f}", f"{r.get('engine') or 0:.2f}",
                            f"{r.get('rss', 0)}M"]
                print(fmt.format(name, b, j, *cols(ix), *cols(q), diff), flush=True)
        if args.drop_dbs and ref is not None:
            (ref / "ctadlir.db").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
