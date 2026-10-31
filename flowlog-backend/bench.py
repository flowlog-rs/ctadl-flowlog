#!/usr/bin/env python3
"""Benchmark CTADL's index and query phases with its Souffle and FlowLog engines.

    bench.py [--jobs 1,4,16] [--backend BACKEND,...|both] [--lang LANG]
             [--query MODELS] [--format summary|sarif] [--out DIR]
             [--prune | --drop-dbs] PROG...

Each program is imported once, into OUT/NAME/facts, by the front end of
--lang: taint-front (the default, PROG.tnt), pcode (an executable, through
Ghidra: needs GHIDRA_HOME and a JDK) or JADX (an APK). An import already
there is reused. Each backend and worker count then indexes and queries a
fresh copy of the facts. The query is the front end's default one, or
--query's JSON models (CTADL's default P-code query names no source: use
src/ctadl/models/pcode/default-query.json). It writes CTADL's text summary;
with --format sarif it writes a SARIF report instead, which can take longer
than the analysis and hundreds of MB.
Backends: souffle, souffle-noplan, souffle-strict, flowlog, flowlog-noplan;
both (the default) is souffle,flowlog. The *-noplan and souffle-strict
variants run CTADL's program without its `.plan` hints (the FlowLog
translation keeps only the order of each hint's version 0, which 4 of them
give): souffle-noplan with Souffle's default SIPS (all-bound), souffle-strict
with `.pragma "RamSIPS" "strict"`, which joins every rule version in the
written order, as FlowLog's planner does for a rule without a `.plan` (bar
cross-product avoidance and semijoin folding).
Reported per phase:
  wall     the whole `ctadl` command (Python driver, model generation, views)
  analyze  the engine call: Souffle's compiled analysis, or FlowLog's input
           conversion + run + result conversion
  engine   FlowLog's binary alone (Souffle's analyze is its binary alone)
  rss      peak RSS of the largest child process (the engine)
Results of all runs are diffed against the first one (compare_db.py on the
final database, which holds the query results, and compare_sarif.py on the
SARIF reports), in full in the run's compare.txt. For pcode and JADX,
compare_pcode.py and compare_jadx.py rename the ids that CTADL computes with
`ord` first, in JADX's reports too.
A phase that fails (CTADL stops on errors its index derives, such as facts
its schema rejects) skips the run's query, and the diff column ends with the
error, or with whether it is the first run's; the databases written so far
are still diffed. Analyses must already be compiled (run each backend once
on a small program first).
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
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
# Variants on CTADL's program without its `.plan` hints, with the extra
# source text appended: Souffle's, and FlowLog's (the translation then has no
# `.plan`).
SOUFFLE_VARIANTS = {
    "souffle-noplan": "",  # Souffle's default SIPS (all-bound)
    "souffle-strict": '\n.pragma "RamSIPS" "strict"\n',  # the written order, like FlowLog
}
NOPLAN_VARIANTS = {**SOUFFLE_VARIANTS, "flowlog-noplan": ""}
BACKENDS = ["souffle", *SOUFFLE_VARIANTS, "flowlog", "flowlog-noplan"]
# front end -> the comparison of its databases
COMPARE = {"taint-front": "compare_db.py", "pcode": "compare_pcode.py", "JADX": "compare_jadx.py"}


def load_cli():
    """Loads CTADL's `ctadl` script as a module, to time its steps."""
    path = shutil.which("ctadl")
    if not path:
        sys.exit("error: cannot find CTADL's `ctadl` script on PATH")
    loader = importlib.machinery.SourceFileLoader("ctadl_cli", path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("ctadl_cli", loader))
    sys.modules["ctadl_cli"] = mod
    loader.exec_module(mod)
    return mod


def shim(backend: str, argv: list) -> None:
    """Runs the `ctadl` CLI with timing hooks around the engine call."""
    cli = load_cli()
    engine = backend.split("-")[0]
    cls = cli.FlowLogCompiledAnalysis if engine == "flowlog" else cli.SouffleCompiledAnalysis
    if backend in NOPLAN_VARIANTS:
        build = cli.build_analysis

        def build_variant(args, src):
            text = PLAN.sub("", Path(src).read_text())
            if ".plan" in text:
                sys.exit(f"error: a .plan directive in {src} was not removed")
            out = Path(src).with_name(f"{Path(src).stem}-{backend}.dl")
            out.write_text(text + NOPLAN_VARIANTS[backend])
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
    if engine == "flowlog":
        time_flowlog_steps(cli)
    # CTADL's own steps around the analysis (the rest of the wall time)
    for step in ("after_index", "after_query", "visualize_query_results"):
        def timed(*a, _f=getattr(cli, step), _step=step, **kw):
            t0 = time.time()
            r = _f(*a, **kw)
            print(f"[bench] {_step}={time.time() - t0:.3f}", file=sys.stderr, flush=True)
            return r

        setattr(cli, step, timed)
    sys.argv = ["ctadl"] + argv + ["--engine", engine]  # the CLI's argparse reads sys.argv
    cli.main(sys.argv)


def time_flowlog_steps(cli) -> None:
    """Prints the FlowLog engine's steps as `[flowlog] <step>: <seconds>s` (bench_reps.py reads
    them), and the output of its binary."""
    from ctadl import flowlog

    def timed(f, step):
        def g(*a, **kw):
            t0 = time.time()
            r = f(*a, **kw)
            print(f"[flowlog] {step}: {time.time() - t0:.2f}s", file=sys.stderr, flush=True)
            return r
        return g

    cls = flowlog.FlowLogAnalysis
    cls.read_inputs = timed(cls.read_inputs, "prepare inputs")
    cls.write_outputs = timed(cls.write_outputs, "write results")
    run = timed(cli.FlowLogCompiledAnalysis.run, "FlowLog run")

    def run_and_echo(self, *a, **kw):
        r = run(self, *a, **kw)
        # "<elapsed>: Dataflow executed": the rest of the run writes the results
        for line in (r.stdout or b"").decode(errors="replace").splitlines():
            print(f"[flowlog]   {line}", file=sys.stderr, flush=True)
        return r

    cli.FlowLogCompiledAnalysis.run = run_and_echo


def run(cmd: list, log: Path, may_fail: bool = False) -> tuple[float, dict]:
    """Runs CMD, its output in LOG. A failure ends the benchmark, or with MAY_FAIL is the
    result's "error": CTADL's first `** ` message, else the last line of the output."""
    t0 = time.time()
    with open(log, "w") as f:
        f.write(f"$ {' '.join(map(str, cmd))}\n")
        f.flush()
        r = subprocess.run(list(map(str, cmd)), stdin=subprocess.DEVNULL, stdout=f,
                           stderr=subprocess.STDOUT)
    wall = time.time() - t0
    text = log.read_text(errors="replace")
    out: dict = {"wall": wall}
    if r.returncode:
        if not may_fail:
            sys.exit(f"command failed (see {log}): {' '.join(map(str, cmd))}")
        lines = text.splitlines()
        out["error"] = next((s[3:] for s in lines if s.startswith("** ")), lines[-1])
    m = re.findall(r"\[bench\] analyze=([\d.]+) rss=(\d+)", text)
    e = re.findall(r"\[flowlog\] FlowLog run: ([\d.]+)s", text)
    if m:
        out["analyze"], out["rss"] = float(m[-1][0]), int(m[-1][1])
    out["engine"] = float(e[-1]) if e else out.get("analyze")
    return wall, out


def last_line(r: subprocess.CompletedProcess) -> str:
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()


def compare(ref: Path, other: Path, name: str, schemas: list, sarif: bool, lang: str) -> str:
    """The summary of OTHER's run against REF's; the comparisons' output goes to
    OTHER/compare.txt."""
    cmd = [sys.executable, HERE / COMPARE[lang], ref / "ctadlir.db", other / "ctadlir.db", "-q",
           "--keys", "VirtualAssign=0,1,2,3,4,5,6"]
    for s in schemas:
        cmd += ["--schema", s]
    r = subprocess.run(list(map(str, cmd)), capture_output=True, text=True)
    out, res = [r.stdout, r.stderr], last_line(r)
    if sarif:
        reports = [d / f"{name}.sarif" for d in (ref, other)]
        if lang == "JADX":  # rename the ord-based ids, by each report's database
            for d, report in zip((ref, other), reports):
                canon = subprocess.run([sys.executable, str(HERE / "compare_jadx.py"), "canon",
                                        str(d / "ctadlir.db"), str(report)],
                                       capture_output=True, text=True, check=True).stdout
                report.with_suffix(".canon.sarif").write_text(canon)
            reports = [r.with_suffix(".canon.sarif") for r in reports]
        r = subprocess.run([sys.executable, str(HERE / "compare_sarif.py"), *map(str, reports)],
                           capture_output=True, text=True)
        out += [r.stdout, r.stderr]
        res = f"{res}; {last_line(r)}"
    (other / "compare.txt").write_text("".join(out))
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
    ap.add_argument("--lang", choices=list(COMPARE), default="taint-front",
                    help="the programs' front end (default: taint-front)")
    ap.add_argument("--query", type=Path,
                    help="JSON models for `ctadl query` (default: the front end's query)")
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
        if not (facts.is_dir() and any(facts.iterdir())):
            facts.mkdir(parents=True, exist_ok=True)
            run(["ctadl", "import", args.lang, prog, "-o", facts, "-f"], out / name / "import.log")
        ref = ref_error = None
        for b in backends:
            for j in jobs:
                d = out / name / f"{b}-j{j}"
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(facts, d)
                drv = [sys.executable, __file__, "--shim", b]
                _, ix = run(drv + ["--directory", d, "index", "-f", "-j", j], d / "index.log",
                            may_fail=True)
                sarif = args.format == "sarif"
                report = d / (f"{name}.sarif" if sarif else "summary.txt")
                query = [args.query.resolve()] if args.query else []
                q: dict = {}
                if "error" not in ix:
                    _, q = run(drv + ["--directory", d, "query", *query, "-j", j,
                                      "--format", args.format, "-o", report], d / "query.log",
                               may_fail=True)
                error = ix.get("error") or q.get("error")
                if ref is None:
                    ref_error = error
                    diff = "(reference)" + (f"; failed: {error}" if error else "")
                else:
                    diff = compare(ref, d, name, args.schema, sarif and not (error or ref_error),
                                   args.lang)
                    if error and error == ref_error:
                        diff += "; failed as the first run"
                    elif error or ref_error:
                        diff += f"; failed: {error}" if error else "; the first run failed"
                if ref is not None and (args.drop_dbs or (
                        args.prune and ", 0 different" in diff
                        and (not sarif or diff.endswith("sarif: equal")))):
                    (d / "ctadlir.db").unlink(missing_ok=True)
                ref = ref or d

                def cols(r: dict) -> list:
                    if not r:
                        return ["-"] * 4
                    return [f"{r['wall']:.2f}", f"{r.get('analyze', 0):.2f}", f"{r.get('engine') or 0:.2f}",
                            f"{r.get('rss', 0)}M"]
                print(fmt.format(name, b, j, *cols(ix), *cols(q), diff), flush=True)
        if args.drop_dbs and ref is not None:
            (ref / "ctadlir.db").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
