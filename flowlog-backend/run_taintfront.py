#!/usr/bin/env python3
"""Run CTADL's taint-front test suite with Souffle and/or FlowLog backends.

For every ctadl/tests/taint-front/*.tnt this mirrors tests/bin/check-taintfront:
import, index, query (SARIF), then CTADL's checks (test_ctadlir.py against the
.json, sarif_has_code_flows.py for .codeflows/.nocodeflows). Since
test_ctadlir.py checks a table's `excludes` against its first row only, the
.json is also checked strictly against all rows ("strict"). With both
backends (or when the other backend's database from the same phase is
already present) it also diffs the resulting databases relation by relation
(compare_db.py) and the SARIF reports (compare_sarif.py).

    run_taintfront.py [--backend souffle|flowlog|both] [--phase index|query]
                      [--testdir DIR] [--out DIR] [-j N] [--index-args ARGS]
                      [--query-args ARGS] [--prune] [TEST...]

--testdir runs another corpus of .tnt programs (e.g. ctadl/taint-front or
ctadl-rs/ctadl-ascent/tests/tnt); programs without a .json/.codeflows/
.nocodeflows expectation are only diffed between the backends. As in
check-taintfront, a test's .options file holds extra `ctadl index` arguments;
--index-args/--query-args add arguments to every test's `ctadl index`/`query`
(e.g. --index-args=--object-tracking, --query-args="--compute-slices all").
"""
from __future__ import annotations

import argparse
import json
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CTADL = HERE.parent  # the CTADL checkout this port is part of
TOP = CTADL.parent  # the workspace
TESTDIR = CTADL / "tests" / "taint-front"
FLOWLOG_DRIVER = [sys.executable, str(HERE / "flowlog_ctadl.py")]


def run(cmd: list, log, cwd=None, env=None) -> tuple[int, float]:
    t0 = time.time()
    log.write(f"$ {' '.join(map(str, cmd))}\n")
    log.flush()
    r = subprocess.run(list(map(str, cmd)), stdout=log, stderr=subprocess.STDOUT, cwd=cwd, env=env)
    return r.returncode, time.time() - t0


def run_backend(backend: str, test: Path, out: Path, phase: str, jobs: int,
                index_args: list, query_args: list) -> dict:
    name = test.stem
    d = out / name / backend
    d.mkdir(parents=True, exist_ok=True)
    ctadl = ["ctadl"] if backend == "souffle" else FLOWLOG_DRIVER
    res: dict = {"checks": []}
    (d / "phase").unlink(missing_ok=True)
    with open(d / "run.log", "w") as log:
        rc, _ = run(["ctadl", "import", "taint-front", test, "-o", d, "-f"], log)
        if rc:
            res["error"] = "import"
            return res
        opts = test.with_suffix(".options")
        extra = index_args + (shlex.split(opts.read_text()) if opts.exists() else [])
        rc, res["index_s"] = run(ctadl + ["--directory", d, "index", "-f", "-j", jobs] + extra, log)
        if rc:
            res["error"] = "index"
            return res
        if phase == "index":
            (d / "phase").write_text(phase)
            return res
        sarif = d / f"{name}.sarif"
        rc, res["query_s"] = run(ctadl + ["--directory", d, "query", "-j", jobs, "--format", "sarif", "-o", sarif]
                                 + query_args, log)
        if rc:
            res["error"] = "query"
            return res
        checks = []
        if test.with_suffix(".json").exists():
            rc, _ = run([sys.executable, CTADL / "tests" / "test_ctadlir.py", test.with_suffix(".json")], log, cwd=d)
            checks.append(("ctadlir", rc == 0))
            checks.append(("strict", strict_check(test.with_suffix(".json"), d / "ctadlir.db", log)))
        if test.with_suffix(".codeflows").exists():
            rc, _ = run([sys.executable, CTADL / "tests" / "bin" / "sarif_has_code_flows.py", sarif], log, cwd=d)
            checks.append(("codeflows", rc == 0))
        if test.with_suffix(".nocodeflows").exists():
            rc, _ = run([sys.executable, CTADL / "tests" / "bin" / "sarif_has_code_flows.py", sarif], log, cwd=d)
            checks.append(("nocodeflows", rc != 0))
        res["checks"] = checks
    (d / "phase").write_text(phase)
    return res


def strict_check(spec: Path, db: Path, log) -> bool:
    def match(tc: dict, row: sqlite3.Row) -> bool:
        return all(row[c] == v for c, v in tc.items())

    ok = True
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        for table, t in json.loads(spec.read_text()).items():
            rows = con.execute(f'SELECT * FROM "{table}"').fetchall()
            for tc in t.get("includes", []):
                if not any(match(tc, r) for r in rows):
                    log.write(f"strict: {table}: missing include {tc}\n")
                    ok = False
            for tc in t.get("excludes", []):
                for r in rows:
                    if match(tc, r):
                        log.write(f"strict: {table}: exclude {tc} violated by {dict(r)}\n")
                        ok = False
    return ok


def comparable(out: Path, name: str, phase: str) -> bool:
    marks = [out / name / b / "phase" for b in ("souffle", "flowlog")]
    return all(m.exists() and m.read_text().strip() == phase for m in marks)


def compare(out: Path, name: str, schemas: list, phase: str) -> str:
    s, f = out / name / "souffle" / "ctadlir.db", out / name / "flowlog" / "ctadlir.db"
    cmd = [sys.executable, HERE / "compare_db.py", s, f, "-q", "--keys", "VirtualAssign=0,1,2,3,4,5,6"]
    for sc in schemas:
        cmd += ["--schema", sc]
    r = subprocess.run(list(map(str, cmd)), capture_output=True, text=True)
    text = r.stdout + r.stderr
    res = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()
    sarifs = [out / name / b / f"{name}.sarif" for b in ("souffle", "flowlog")]
    if phase == "query" and all(p.exists() for p in sarifs):
        r = subprocess.run([sys.executable, str(HERE / "compare_sarif.py"), *map(str, sarifs)],
                           capture_output=True, text=True)
        text += r.stdout + r.stderr
        res += "; " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip())
    (out / name / "compare.txt").write_text(text)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tests", nargs="*")
    ap.add_argument("--backend", choices=["souffle", "flowlog", "both"], default="both")
    ap.add_argument("--phase", choices=["index", "query"], default="query")
    ap.add_argument("--testdir", type=Path, default=TESTDIR)
    ap.add_argument("--out", type=Path, default=TOP / "runs" / "taint-front")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("--schema", action="append", default=[], help="schema manifests for compare_db.py")
    ap.add_argument("--index-args", default="", help="extra `ctadl index` arguments for every test")
    ap.add_argument("--query-args", default="", help="extra `ctadl query` arguments for every test")
    ap.add_argument("--prune", action="store_true",
                    help="delete the ctadlir.db files (~15 MB each) of tests that pass on both backends "
                         "with identical results; logs, SARIF and compare.txt are kept")
    args = ap.parse_args()
    # ctadl resolves -o relative to --directory
    args.out, args.testdir = args.out.resolve(), args.testdir.resolve()

    tests = sorted(args.testdir.glob("*.tnt"))
    if args.tests:
        tests = [t for t in tests if t.stem in args.tests]
    backends = ["souffle", "flowlog"] if args.backend == "both" else [args.backend]
    failed = 0
    for t in tests:
        line = [f"{t.stem:22}"]
        passed = True
        for b in backends:
            r = run_backend(b, t, args.out, args.phase, args.jobs,
                            shlex.split(args.index_args), shlex.split(args.query_args))
            status = r.get("error", "ok")
            if "error" in r:
                failed += 1
                passed = False
                status = f"{r['error']} FAILED"
            elif r["checks"]:
                bad = [c for c, ok in r["checks"] if not ok]
                status = "checks FAILED: " + ",".join(bad) if bad else "checks ok (" + ",".join(c for c, _ in r["checks"]) + ")"
                failed += bool(bad)
                passed &= not bad
            times = " ".join(f"{k[:-2]}={r[k]:.1f}s" for k in ("index_s", "query_s") if k in r)
            line.append(f"{b}: {status} [{times}]")
        if comparable(args.out, t.stem, args.phase):
            diff = compare(args.out, t.stem, args.schema, args.phase)
            line.append("diff: " + diff)
            same = ", 0 different" in diff and ("sarif" not in diff or diff.endswith("sarif: equal"))
            if args.prune and passed and same:
                for b in backends:
                    (args.out / t.stem / b / "ctadlir.db").unlink(missing_ok=True)
        print(" | ".join(line), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
