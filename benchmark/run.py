#!/usr/bin/env python3
"""Run ordinary ctadl import/index/query commands, preserving logs and measurements."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from inputs import prepare_input, select
from records import command, digest, event, phase_metrics, require_ok, utc, write_json
from ctadl.flowlog import SQLITE_IO_MODE

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "benchmark"
COMPARE = {
    "taint-front": "compare_db.py",
    "pcode": "compare_pcode.py",
    "JADX": "compare_jadx.py",
}


def capture(argv, cwd=ROOT, env=None):
    return subprocess.check_output(
        list(map(str, argv)), cwd=cwd, env=env, stderr=subprocess.STDOUT, text=True
    ).strip()


def source_files(repo):
    names = subprocess.check_output(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ]
    )
    return sorted(
        {
            Path(os.fsdecode(n))
            for n in names.split(b"\0")
            if n and (repo / os.fsdecode(n)).is_file()
        }
    )


def source_hash(repo):
    h = hashlib.sha256()
    for name in source_files(repo):
        h.update(str(name).encode() + b"\0" + bytes.fromhex(digest(repo / name)))
    return h.hexdigest()


def check_environment(rows):
    needed = ["ctadl", "souffle", "rustc", "cargo", "c++", "curl", "taintfront"]
    if any(r["lang"] in ("pcode", "JADX") for r in rows):
        needed += ["java"]
    if any(r["lang"] == "pcode" for r in rows):
        needed += ["javac", "dpkg-deb"]
        ghidra = Path(os.environ.get("GHIDRA_HOME", "/missing"))
        if not (ghidra / "support/analyzeHeadless").exists():
            raise RuntimeError(
                "Set GHIDRA_HOME to Ghidra 11.2; see benchmark/README.md"
            )
    missing = [name for name in needed if not shutil.which(name)]
    if missing or not Path("/usr/bin/time").exists():
        raise RuntimeError(
            f"Missing tools: {missing}; run bash benchmark/setup.sh and source benchmark/env.sh"
        )
    compiler = Path(os.environ.get("FLOWLOG_COMPILER", "/missing"))
    runtime = Path(os.environ.get("FLOWLOG_RUNTIME_PATH", "/missing"))
    if not compiler.is_file() or not (runtime / "Cargo.toml").exists():
        raise RuntimeError(
            "Set FLOWLOG_COMPILER and FLOWLOG_RUNTIME_PATH to the same built main checkout"
        )
    flowlog = runtime.resolve().parent
    git = lambda *args: capture(["git", "-C", flowlog, *args])
    if git("branch", "--show-current") != "main" or git("status", "--porcelain"):
        raise RuntimeError("Use a clean FlowLog main checkout")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        raise RuntimeError(
            "FlowLog main differs from fetched origin/main; rerun benchmark/setup.sh"
        )
    build_file = HERE / "compiler.json"
    if not build_file.exists():
        raise RuntimeError(
            "Run bash benchmark/setup.sh to record the compiler build revision"
        )
    build = json.loads(build_file.read_text())
    if build.get("sqlite_io") != SQLITE_IO_MODE:
        raise RuntimeError(
            "Native SQLite compiler/runtime check is missing; rerun benchmark/setup.sh"
        )
    if build["commit"] != git("rev-parse", "HEAD") or build[
        "compiler_sha256"
    ] != digest(compiler):
        raise RuntimeError(
            "FlowLog binary does not match the recorded build; rerun benchmark/setup.sh"
        )
    import ctadl

    if not Path(ctadl.__file__).resolve().is_relative_to(ROOT / "src"):
        raise RuntimeError(
            "Python imported another CTADL checkout; source benchmark/env.sh"
        )
    installed = Path(shutil.which("ctadl")).read_bytes().split(b"\n", 1)[1]
    source = (ROOT / "bin/ctadl").read_bytes().split(b"\n", 1)[1]
    if installed != source:
        raise RuntimeError(
            "The installed ctadl CLI is stale; reinstall this checkout with pip install -e ."
        )
    if any(r["lang"] == "JADX" for r in rows):
        import importlib.metadata

        try:
            version = importlib.metadata.version("ctadl-jadx-fact-generator-plugin")
        except importlib.metadata.PackageNotFoundError:
            version = None
        if version != "0.6.2":
            raise RuntimeError(
                "Install ctadl-jadx-fact-generator-plugin==0.6.2; see benchmark/README.md"
            )
    return flowlog, compiler, build


def snapshot(run, rows, args, flowlog, compiler, build):
    metadata = {
        "schema_version": 1,
        "started_at": utc(),
        "status": "running",
        "ctadl": {
            "commit": capture(["git", "rev-parse", "HEAD"]),
            "source_sha256": source_hash(ROOT),
            "branch": capture(["git", "branch", "--show-current"]),
            "status": capture(["git", "status", "--porcelain"]),
        },
        "flowlog": {
            **build,
            "source_sha256": source_hash(flowlog),
            "sqlite_io": SQLITE_IO_MODE,
        },
        "options": vars(args),
        "workloads": rows,
        "environment": {
            k: os.environ[k]
            for k in (
                "PATH",
                "FLOWLOG_COMPILER",
                "FLOWLOG_RUNTIME_PATH",
                "CARGO_TARGET_DIR",
                "CARGO_HOME",
                "RUSTUP_HOME",
                "RUSTUP_TOOLCHAIN",
                "XDG_STATE_HOME",
                "TMPDIR",
                "GHIDRA_HOME",
                "JAVA_HOME",
            )
            if k in os.environ
        },
        "machine": {
            "platform": platform.platform(),
            "hostname": platform.node(),
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
            "numa_policy": (
                capture(["numactl", "--show"]) if shutil.which("numactl") else None
            ),
            "memory": Path("/proc/meminfo").read_text(),
            "cpu": capture(["lscpu"]),
            "disk_free_bytes": shutil.disk_usage(run).free,
        },
        "versions": {
            name: capture(cmd)
            for name, cmd in {
                "rustc": ["rustc", "--version"],
                "souffle": ["souffle", "--version"],
                "python": [sys.executable, "--version"],
                "c++": ["c++", "--version"],
                "python_packages": [sys.executable, "-m", "pip", "freeze", "--all"],
            }.items()
        },
    }
    with tarfile.open(run / "ctadl-source.tar.gz", "w:gz") as archive:
        for name in source_files(ROOT):
            archive.add(ROOT / name, arcname=str(name), recursive=False)
    if any(r["lang"] in ("pcode", "JADX") for r in rows):
        metadata["versions"]["java"] = capture(["java", "-version"])
    if any(r["lang"] == "pcode" for r in rows):
        properties = Path(os.environ["GHIDRA_HOME"]) / "Ghidra/application.properties"
        metadata["versions"]["ghidra"] = properties.read_text()
    write_json(run / "manifest.json", metadata)
    return metadata


def phases(
    work: Path,
    logs: Path,
    row: dict,
    backend: str,
    workers: int,
    rep: int,
    mode: str,
    run: Path,
    env: dict,
    args,
) -> bool:
    for phase in ("index", "query"):
        directory = logs / phase
        argv = [
            "ctadl",
            "--directory",
            work,
            phase,
            "--engine",
            backend,
            "--timings",
            directory / "timings.jsonl",
            "-j",
            str(workers),
        ]
        if phase == "index":
            argv += ["-f"]
        else:
            if row.get("query"):
                argv += [ROOT / row["query"]]
            argv += [
                "--format",
                row["format"],
                "-o",
                work / ("report.sarif" if row["format"] == "sarif" else "report.txt"),
            ]
        record = command(
            ["/usr/bin/time", "-v", "-o", directory / "resources.txt", "--", *argv],
            directory,
            cwd=ROOT,
            env=env,
            timeout=args.timeout,
            events=run / "events.jsonl",
        )
        result = {
            "schema_version": 1,
            "workload": row["id"],
            "language": row["lang"],
            "backend": backend,
            "sqlite_io": SQLITE_IO_MODE if backend == "flowlog" else "souffle",
            "workers": workers,
            "repetition": rep,
            "mode": mode,
            "phase": phase,
            "status": record["status"],
            "exit_code": record["exit_code"],
            "logs": str(directory.relative_to(run)),
            "wall_seconds": record["wall_seconds"],
            "analyze_seconds": None,
            "engine_seconds": None,
            "process_peak_rss_mib": None,
        }
        program = work / f"{phase}.dl"
        if program.exists():
            shutil.copyfile(program, directory / program.name)
        try:
            result.update(
                phase_metrics(
                    directory,
                    record,
                    measured=mode == "measure",
                    expected_sqlite_io=SQLITE_IO_MODE if backend == "flowlog" else None,
                )
            )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            result.update(status="invalid_metrics", error=str(exc))
        write_json(directory / "measurement.json", result)
        with (run / "measurements.jsonl").open("a") as stream:
            stream.write(json.dumps(result) + "\n")
        if result["status"] != "ok":
            event(run / "events.jsonl", kind="case_failed", **result)
            if phase == "index":
                skipped = {
                    **result,
                    "phase": "query",
                    "status": "skipped",
                    "reason": "index_failed",
                    "exit_code": None,
                    "wall_seconds": None,
                    "analyze_seconds": None,
                    "engine_seconds": None,
                    "process_peak_rss_mib": None,
                    "logs": str((logs / "query").relative_to(run)),
                }
                write_json(logs / "query/measurement.json", skipped)
                with (run / "measurements.jsonl").open("a") as stream:
                    stream.write(json.dumps(skipped) + "\n")
            return False
    return True


def compare(
    left: Path, right: Path, directory: Path, row: dict, env: dict, run: Path, args
) -> str:
    python = sys.executable
    argv = [
        python,
        HERE / COMPARE[row["lang"]],
        left / "ctadlir.db",
        right / "ctadlir.db",
        "--keys",
        "VirtualAssign=0,1,2,3,4,5,6",
        "-q",
    ]
    if row["lang"] == "taint-front":
        for name in ("index_schema.json", "query_schema.json"):
            argv += ["--schema", HERE / name]
    d = directory / "database"
    r = command(
        argv, d, cwd=ROOT, env=env, timeout=args.timeout, events=run / "events.jsonl"
    )
    output = (d / "stdout.log").read_text(errors="replace")
    if (
        r["status"] not in ("ok", "failed")
        or r["exit_code"] not in (0, 1)
        or " different" not in output
    ):
        verdict = "comparison_error"
    else:
        # Equal choice keys do not prove the selected witnesses are derivable.
        verdict = (
            "equal" if r["exit_code"] == 0 and "WITNESS" not in output else "different"
        )
    if row["format"] == "sarif" and verdict != "comparison_error":
        reports = [left / "report.sarif", right / "report.sarif"]
        if row["lang"] == "JADX":
            canonical = []
            for index, report in enumerate(reports):
                d = directory / f"canonical-{index}"
                r = command(
                    [
                        python,
                        HERE / "compare_jadx.py",
                        "canon",
                        report.parent / "ctadlir.db",
                        report,
                    ],
                    d,
                    cwd=ROOT,
                    env=env,
                    timeout=args.timeout,
                    events=run / "events.jsonl",
                )
                require_ok(r, d)
                canonical.append(d / "stdout.log")
            reports = canonical
        d = directory / "sarif"
        r = command(
            [python, HERE / "compare_sarif.py", *reports],
            d,
            cwd=ROOT,
            env=env,
            timeout=args.timeout,
            events=run / "events.jsonl",
        )
        output = (d / "stdout.log").read_text(errors="replace")
        if (
            r["status"] not in ("ok", "failed")
            or r["exit_code"] not in (0, 1)
            or "sarif:" not in output
        ):
            verdict = "comparison_error"
        elif r["exit_code"] == 1:
            verdict = "different"
    write_json(
        directory / "comparison.json",
        {
            "status": verdict,
            "workload": row["id"],
            "reference": str(left.relative_to(run)),
            "candidate": str(right.relative_to(run)),
            "note": "Differences may involve choice-domain witnesses; this does not certify a valid choice model.",
        },
    )
    return verdict


def export_csv(run: Path) -> None:
    fields = [
        "workload",
        "language",
        "backend",
        "workers",
        "repetition",
        "mode",
        "phase",
        "status",
        "exit_code",
        "wall_seconds",
        "analyze_seconds",
        "engine_seconds",
        "sqlite_io",
        "process_peak_rss_mib",
        "logs",
    ]
    with (run / "measurements.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        if (run / "measurements.jsonl").exists():
            for line in (run / "measurements.jsonl").read_text().splitlines():
                writer.writerow(json.loads(line))


def run_benchmark(rows, args):
    flowlog, compiler, build = check_environment(rows)
    env = {**os.environ, "LC_ALL": "C"}
    run = (
        HERE
        / "runs"
        / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}")
    )
    run.mkdir(parents=True, exist_ok=False)
    metadata = snapshot(run, rows, args, flowlog, compiler, build)
    failures, differences = [], []
    status = "failed"
    try:
        for row in rows:
            workroot = run / "workloads" / row["id"]
            workroot.mkdir(parents=True)
            input_path = prepare_input(row, env, workroot / "prepare", args.offline)
            write_json(
                workroot / "input.json",
                {**row, "path": str(input_path), "actual_sha256": digest(input_path)},
            )
            facts = workroot / "facts"
            d = workroot / "import"
            result = command(
                ["ctadl", "import", row["lang"], input_path, "-o", facts, "-f"],
                d,
                cwd=ROOT,
                env=env,
                timeout=args.timeout,
                events=run / "events.jsonl",
            )
            require_ok(result, d)
            for workers in args.workers:
                warm = {}
                for backend in args.backends:
                    base = workroot / backend / f"workers-{workers}" / "warmup"
                    work = base / "work"
                    shutil.copytree(facts, work)
                    if not phases(
                        work, base, row, backend, workers, 0, "warmup", run, env, args
                    ):
                        raise RuntimeError(f"Warmup failed: {base}")
                    warm[backend] = work
                reference_backend = "souffle" if "souffle" in warm else args.backends[0]
                reference = warm[reference_backend]
                differing = False
                for backend in (b for b in args.backends if b != reference_backend):
                    verdict = compare(
                        reference,
                        warm[backend],
                        workroot / "checks" / f"workers-{workers}" / backend,
                        row,
                        env,
                        run,
                        args,
                    )
                    if verdict == "comparison_error":
                        raise RuntimeError(f"Comparison failed: {row['id']} {backend}")
                    if verdict == "different":
                        differing = True
                        differences.append(f"{row['id']}/{backend}/workers-{workers}")
                if differences and args.suite == "smoke":
                    raise RuntimeError(
                        "Smoke correctness check differs; see checks directory"
                    )
                for backend, work in warm.items():
                    if not args.keep_work and not differing:
                        shutil.rmtree(work)
                for repetition in range(1, args.reps + 1):
                    order = args.backends if repetition % 2 else args.backends[::-1]
                    for backend in order:
                        base = (
                            workroot
                            / backend
                            / f"workers-{workers}"
                            / f"rep-{repetition:03d}"
                        )
                        work = base / "work"
                        shutil.copytree(facts, work)
                        ok = phases(
                            work,
                            base,
                            row,
                            backend,
                            workers,
                            repetition,
                            "measure",
                            run,
                            env,
                            args,
                        )
                        if not ok:
                            failures.append(str(base.relative_to(run)))
                        if ok and not args.keep_work:
                            shutil.rmtree(work)
                export_csv(run)
            if not args.keep_work:
                shutil.rmtree(facts)
        if (
            metadata["ctadl"]["source_sha256"] != source_hash(ROOT)
            or metadata["flowlog"]["source_sha256"] != source_hash(flowlog)
            or metadata["flowlog"]["compiler_sha256"] != digest(compiler)
        ):
            raise RuntimeError(
                "Sources changed during the run; measurements cannot share one revision"
            )
        status = "failed" if failures else "needs_review" if differences else "complete"
    except KeyboardInterrupt:
        status = "interrupted"
        failures.append("Interrupted by user")
    except Exception as exc:
        failures.append(str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
    finally:
        export_csv(run)
        metadata.update(
            status=status, finished_at=utc(), failures=failures, differences=differences
        )
        write_json(run / "manifest.json", metadata)
        # Compilation logs and translated programs survive cache eviction; binaries are omitted.
        cache = (
            Path(env.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "ctadl/analysis"
        )
        if cache.exists():
            with tarfile.open(run / "analysis-sources.tar.gz", "w:gz") as archive:
                for p in cache.rglob("*"):
                    if p.is_file() and p.suffix in (
                        ".log",
                        ".fl",
                        ".rs",
                        ".json",
                        ".dl",
                        ".cpp",
                        ".toml",
                        ".lock",
                    ):
                        archive.add(
                            p, arcname=str(p.relative_to(cache)), recursive=False
                        )
        event(run / "events.jsonl", kind="run_finished", status=status)
        print(
            f"\n{status.upper()}: {run}\nRaw logs: workloads/; records: manifest.json, measurements.jsonl, measurements.csv"
        )
    return {"complete": 0, "needs_review": 4, "interrupted": 130}.get(status, 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--suite",
        choices=["smoke", "synthetic", "pcode", "jadx", "realistic", "all"],
        default="synthetic",
    )
    ap.add_argument("--workloads", help="comma-separated IDs from workloads.json")
    ap.add_argument("--workers", default="32", help="comma-separated worker counts")
    ap.add_argument("--reps", type=int, help="default: 3, or 1 for smoke")
    ap.add_argument(
        "--backends",
        default="souffle,flowlog",
        help="souffle,flowlog (normal CTADL engines)",
    )
    ap.add_argument("--timeout", type=float, default=1800, help="seconds per command")
    ap.add_argument(
        "--offline", action="store_true", help="require cached workload inputs"
    )
    ap.add_argument(
        "--keep-work", action="store_true", help="keep successful databases and reports"
    )
    args = ap.parse_args()
    try:
        args.workers = [int(n) for n in args.workers.split(",")]
        if len(set(args.workers)) != len(args.workers) or any(
            n < 1 or n > len(os.sched_getaffinity(0)) for n in args.workers
        ):
            raise ValueError(
                "Worker counts must be unique, positive and within CPU affinity"
            )
        args.backends = args.backends.split(",")
        if len(set(args.backends)) != len(args.backends) or any(
            b not in ("souffle", "flowlog") for b in args.backends
        ):
            raise ValueError("Backends must be souffle and/or flowlog")
        args.reps = (
            args.reps if args.reps is not None else (1 if args.suite == "smoke" else 3)
        )
        if args.reps < 1 or not 0 < args.timeout < float("inf"):
            raise ValueError("Repetitions and timeout must be positive")
        rows = select(args.suite, args.workloads)
        with (HERE / ".run.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another benchmark is running in this checkout")
            return run_benchmark(rows, args)
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
