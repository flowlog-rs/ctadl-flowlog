#!/usr/bin/env python3
"""Report of a sweep by benchmark/sweep.sh: Soufflé's and FlowLog's medians, their ratios,
and how the engines' results compare.

Reads SWEEP/status.tsv and the latest run of each workload in it, and writes to OUT
(default SWEEP/report):
  README.md         setup, ratios per suite and per workload, choice models, problems
  measurements.csv  all records of the runs, as run.py wrote them but without the log paths
  comparisons.txt   the engines' database and SARIF comparisons, without equal relations
  choice-model.txt  the choice-model checks, if the sweep ran any
"""

import argparse
import collections
import csv
import json
import math
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKLOADS = {
    r["id"]: r for r in json.loads((HERE / "workloads.json").read_text())["workloads"]
}
SUITES = (("Synthetic", "taint-front"), ("P-code", "pcode"), ("JADX", "JADX"))
ENGINES = ("souffle", "flowlog")
PHASES = ("index", "query")
METRICS = ("wall_seconds", "engine_seconds", "process_peak_rss_mib")
EXIT = {
    "0": "complete",
    "1": "failed",
    "2": "error",
    "4": "needs_review",
    "130": "interrupted",
}
SUMMARY = re.compile(
    r"^summary: (\d+) equal(?: \((\d+) up to choice-domain witnesses\))?, (\d+) different$",
    re.M,
)
EXAMPLES = 2  # example rows kept per side of a difference
WIDTH = 200  # longest line kept


def count(n, noun):
    return f"{n} {noun}" + ("" if n == 1 else "s")


def trim(lines):
    """A comparison's output without its inputs' paths, with EXAMPLES rows per side of each
    difference and lines cut to WIDTH characters."""
    out, shown = [], {}
    for line in lines:
        if re.match(r"^(A|B): ", line):
            continue
        m = re.match(r"^\s+([-+]) ", line)
        if m:
            shown[m[1]] = shown.get(m[1], 0) + 1
            if shown[m[1]] > EXAMPLES:
                continue
        else:
            shown = {}
        out.append(line if len(line) <= WIDTH else line[: WIDTH - 3] + "...")
    return out


def same_index(text):
    """The number of CTADLStats rows that only the benchmark's FlowLog database has, if they
    are all that tells it from the --all-outputs index (the query adds them); else None.
    """
    m = SUMMARY.search(text)
    found = re.findall(
        r"^(DIFF|WITNESS|MISSING|EXTRA|ARITY)\s+(\S+): (.*)$", text, re.M
    )
    if not m or int(m[3]) != len(found) or len(found) > 1:
        return None
    if not found:
        return 0
    kind, name, rest = found[0]
    x = re.fullmatch(
        r"souffle \d+, flowlog \d+, souffle-only (\d+), flowlog-only 0", rest
    )
    return int(x[1]) if (kind, name) == ("DIFF", "CTADLStats") and x else None


def find(pattern, text):
    m = re.search(pattern, text or "", re.M)
    return m[1].strip() if m else ""


def geomean(xs):
    return math.exp(sum(map(math.log, xs)) / len(xs))


def cell(x, spec):
    return "-" if x is None else format(x, spec)


class Report:
    def __init__(self, sweep):
        self.sweep = sweep
        self.problems = []
        self.run = {}  # workload -> its latest run directory, relative to ROOT
        attempts = {}
        with (sweep / "status.tsv").open() as f:
            for r in csv.DictReader(f, delimiter="\t"):
                attempts.setdefault(r["workload"], []).append(r)
        for w, rs in attempts.items():
            code = rs[-1]["exit"]
            if code not in ("0", "4"):
                self.problem(
                    f"{w}: the latest attempt exited {code} ({EXIT.get(code, 'stopped')}),"
                    f" see `logs/{w}.log`"
                )
            runs = [r["run"] for r in rs if r["run"] != "-"]
            if runs and w in WORKLOADS and (ROOT / runs[-1] / "manifest.json").exists():
                self.run[w] = runs[-1]
            elif runs:
                self.problem(f"{w}: its run `{runs[-1]}` is missing")

        self.manifest, self.rows, self.fields = {}, {}, None
        for w, run in self.run.items():
            self.manifest[w] = json.loads((ROOT / run / "manifest.json").read_text())
            self.rows[w] = []
            if not (ROOT / run / "measurements.csv").exists():
                continue
            with (ROOT / run / "measurements.csv").open() as f:
                reader = csv.DictReader(f)
                fields = [k for k in reader.fieldnames if k != "logs"]
                if self.fields not in (None, fields):
                    sys.exit(
                        "report.py: the runs' measurements.csv differ in their fields"
                    )
                self.fields = fields
                self.rows[w] = [
                    {k: r[k] for k in fields} for r in reader if r["workload"] == w
                ]
        workers = {n for m in self.manifest.values() for n in m["options"]["workers"]}
        if len(workers) > 1:
            sys.exit(f"report.py: the runs mix worker counts {sorted(workers)}")
        self.workers = workers.pop() if workers else None

        # Medians of the measured runs that succeeded.
        self.med = {}
        for w, rows in self.rows.items():
            options = self.manifest[w]["options"]
            for e in (e for e in ENGINES if e in options["backends"]):
                for p in PHASES:
                    measured = [
                        r
                        for r in rows
                        if (r["backend"], r["phase"], r["mode"]) == (e, p, "measure")
                    ]
                    ok = [r for r in measured if r["status"] == "ok"]
                    if len(ok) < options["reps"]:
                        bad = ", ".join(
                            f"run {r['repetition']} {r['status']}"
                            for r in measured
                            if r["status"] != "ok"
                        )
                        self.problem(
                            f"{w}: {e} {p}, {len(ok)} of {options['reps']} measured runs ok"
                            + (f" ({bad})" if bad else "")
                        )
                    for m in METRICS if ok else ():
                        self.med[w, e, p, m] = statistics.median(
                            float(r[m]) for r in ok
                        )
            for r in rows:
                if r["mode"] == "warmup" and r["status"] != "ok":
                    self.problem(
                        f"{w}: {r['backend']} {r['phase']} warm-up {r['status']}"
                    )

        # Each suite small to large: by Soufflé's index time, workloads without one last.
        def size(w):
            x = self.med.get((w, "souffle", "index", "wall_seconds"))
            return (x is None, x or 0.0)

        self.groups = [
            (
                name,
                sorted(
                    (
                        w
                        for w in WORKLOADS
                        if w in self.run and WORKLOADS[w]["lang"] == lang
                    ),
                    key=size,
                ),
            )
            for name, lang in SUITES
        ]
        self.order = [w for _, ws in self.groups for w in ws]

        # The engines' comparisons by run.py, of the warm-up runs' databases and reports.
        self.compared, self.verdict = {}, {}
        for w in self.order:
            c = (
                ROOT
                / self.run[w]
                / "workloads"
                / w
                / "checks"
                / f"workers-{self.workers}"
                / "flowlog"
            )
            if not (c / "comparison.json").exists():
                if set(ENGINES) <= set(self.manifest[w]["options"]["backends"]):
                    self.problem(f"{w}: the engines' results were not compared")
                continue
            if (
                json.loads((c / "comparison.json").read_text())["status"]
                == "comparison_error"
            ):
                self.problem(f"{w}: the comparison failed, see `{self.run[w]}`")
            tool = Path(
                json.loads((c / "database/command.json").read_text())["argv"][1]
            ).name
            db = (c / "database/stdout.log").read_text().splitlines()
            sarif = None
            if (c / "sarif/stdout.log").exists():
                sarif = (c / "sarif/stdout.log").read_text().splitlines()
            self.compared[w] = (tool, db, sarif)
            m = SUMMARY.search("\n".join(db))
            s = find(r"^sarif: (.*)$", "\n".join(sarif)) if sarif else None
            s = {None: "-", "equal": "equal"}.get(
                s, "same results" if s and "same results" in s else s
            )
            self.verdict[w] = (f"{m[1]} / {m[3]}" if m else "-", s or "-")

        # The choice-model checks by sweep.sh, of its latest run of each app.
        self.choice = {}
        for w in self.order:
            d = sweep / "choice-model" / w
            if not d.is_dir():
                continue
            log = (
                (d / "check.log").read_text().splitlines()
                if (d / "check.log").exists()
                else []
            )
            model = next((x for x in log if x.startswith("choice model:")), None)
            if (
                not (d / "run").exists()
                or (d / "run").read_text().strip() != self.run[w]
            ):
                self.problem(f"{w}: the choice-model check is of an older run")
            elif model is None:
                self.problem(
                    f"{w}: the choice-model check did not finish, see `choice-model/{w}/`"
                )
            else:
                index = d / "same-index.txt"
                same = same_index(index.read_text() if index.exists() else "")
                if same is None:
                    self.problem(
                        f"{w}: FlowLog's --all-outputs index differs from the benchmark's,"
                        f" see `choice-model/{w}/same-index.txt`"
                    )
                if not model.startswith("choice model: yes"):
                    self.problem(f"{w}: {model}, see `choice-model/{w}/check.log`")
                forced = re.findall(
                    r"^forced\s+(\S+): (\d+) FlowLog witnesses$", "\n".join(log), re.M
                )
                summary = next(
                    (x for x in log if x.startswith("summary:")), "summary: none"
                )
                self.choice[w] = (forced, summary, model, same)

        self.setup = self.versions()

    def problem(self, text):
        self.problems.append(text)

    def versions(self):
        """Each setup item's values, as the runs' manifests record them."""
        values = {}
        for w in self.order:
            m = self.manifest[w]
            fl, v, mach = m["flowlog"], m["versions"], m["machine"]
            memory = int(find(r"^MemTotal:\s*(\d+) kB", mach["memory"]) or 0) / 2**20
            for item, x in (
                (
                    "CTADL",
                    f"`{m['ctadl']['commit'][:7]}`"
                    + (" with uncommitted changes" if m["ctadl"]["status"] else ""),
                ),
                ("FlowLog", f"`{fl['commit'][:7]}`"),
                ("rustc", find(r"^rustc (\S+)", fl["rustc"])),
                ("SQLite I/O", f"`{fl['sqlite_io']}`"),
                ("Soufflé", find(r"^Version:(.*)$", v.get("souffle")) or "unknown"),
                ("Ghidra", find(r"^application\.version=(.*)$", v.get("ghidra"))),
                ("JDK", find(r'^\S+ version "([^"]+)"', v.get("java"))),
                (
                    "JADX plugin",
                    find(
                        r"^ctadl-jadx-fact-generator-plugin==(.*)$",
                        v.get("python_packages"),
                    ),
                ),
                (
                    "runs",
                    f"1 warm-up and {count(m['options']['reps'], 'measured run')}",
                ),
                ("timeout", f"{m['options']['timeout']:g} s"),
                ("CPU", find(r"^Model name:(.*)$", mach["cpu"])),
                ("CPUs", f"{len(mach['cpu_affinity'])} CPUs"),
                ("memory", f"{memory:.0f} GiB" if memory else ""),
                ("system", mach["platform"].split("-with-")[0]),
            ):
                if x and x not in values.setdefault(item, []):
                    values[item].append(x)
        for item, xs in values.items():
            if len(xs) > 1:
                self.problem(f"the runs differ in {item}: {', '.join(xs)}")
        return {item: ", ".join(xs) for item, xs in values.items()}

    def ratio(self, w, p, m):
        s, f = self.med.get((w, "souffle", p, m)), self.med.get((w, "flowlog", p, m))
        return s / f if s is not None and f else None

    def rss(self, w, e):
        xs = [
            self.med[w, e, p, METRICS[2]]
            for p in PHASES
            if (w, e, p, METRICS[2]) in self.med
        ]
        return max(xs) if xs else None

    def suites(self):
        lines = [
            "| Suite | Workloads | Index wall x | Query wall x | Index engine x | Query engine x "
            "| Total wall, Soufflé / FlowLog (s) | x | Peak RSS, FlowLog / Soufflé |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for name, ws in self.groups + [("All", self.order)]:
            done = [
                w
                for w in ws
                if all(
                    (w, e, p, m) in self.med
                    for e in ENGINES
                    for p in PHASES
                    for m in METRICS
                )
            ]
            if not ws:
                continue
            count = (
                f"{len(done)}" if len(done) == len(ws) else f"{len(done)} of {len(ws)}"
            )
            if not done:
                lines.append(f"| {name} | {count} |" + " - |" * 7)
                continue
            cells = [
                f"{geomean([self.ratio(w, p, m) for w in done]):.2f}"
                for p, m in (
                    ("index", "wall_seconds"),
                    ("query", "wall_seconds"),
                    ("index", "engine_seconds"),
                    ("query", "engine_seconds"),
                )
            ]
            tot = {
                e: sum(self.med[w, e, p, "wall_seconds"] for w in done for p in PHASES)
                for e in ENGINES
            }
            mem = geomean(
                [self.rss(w, "flowlog") / self.rss(w, "souffle") for w in done]
            )
            lines.append(
                f"| {name} | {count} | "
                + " | ".join(cells)
                + f" | {tot['souffle']:,.0f} / {tot['flowlog']:,.0f} | {tot['souffle'] / tot['flowlog']:.2f} | {mem:.1f} |"
            )
        return lines

    def workloads(self):
        lines = [
            "| Suite | Workload | Index wall, S / F (s) | x | Query wall, S / F (s) | x "
            "| Engine x, index / query | Peak RSS, S / F (GiB) | Relations, equal / different | SARIF |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        gib = lambda x: None if x is None else x / 1024  # noqa: E731
        for name, ws in self.groups:
            for w in ws:
                med = lambda e, p: self.med.get((w, e, p, "wall_seconds"))  # noqa: E731
                relations, sarif = self.verdict.get(w, ("-", "-"))
                lines.append(
                    f"| {name} | {w} "
                    f"| {cell(med('souffle', 'index'), '.1f')} / {cell(med('flowlog', 'index'), '.1f')} "
                    f"| {cell(self.ratio(w, 'index', 'wall_seconds'), '.2f')} "
                    f"| {cell(med('souffle', 'query'), '.1f')} / {cell(med('flowlog', 'query'), '.1f')} "
                    f"| {cell(self.ratio(w, 'query', 'wall_seconds'), '.2f')} "
                    f"| {cell(self.ratio(w, 'index', 'engine_seconds'), '.2f')}"
                    f" / {cell(self.ratio(w, 'query', 'engine_seconds'), '.2f')} "
                    f"| {cell(gib(self.rss(w, 'souffle')), '.1f')} / {cell(gib(self.rss(w, 'flowlog')), '.1f')} "
                    f"| {relations} | {sarif} |"
                )
        return lines

    def write(self, out):
        rows = [r for w in self.order for r in self.rows[w]]
        with (out / "measurements.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, self.fields or [], lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

        with (out / "comparisons.txt").open("w") as f:
            f.write(
                "# FlowLog's database and SARIF report against Soufflé's, per workload, as benchmark/run.py compared\n"
                "# them on the warm-up runs' outputs (compare_db.py, compare_pcode.py or compare_jadx.py, then\n"
                "# compare_sarif.py), with the equal relations left out and two example rows per side of a\n"
                "# difference, cut to 200 characters. Taint-front workloads have no SARIF step.\n"
            )
            for w, (tool, db, sarif) in self.compared.items():
                f.write(f"== {WORKLOADS[w]['lang']} {w} ({tool})\n")
                f.write("\n".join(trim(db)) + "\n")
                if sarif is not None:
                    f.write("\n".join(trim(sarif)) + "\n")

        (out / "choice-model.txt").unlink(missing_ok=True)
        if self.choice:
            added = "/".join(
                sorted(
                    {str(x[3]) for x in self.choice.values() if x[3] is not None},
                    key=int,
                )
            )
            with (out / "choice-model.txt").open("w") as f:
                f.write(
                    "# Is FlowLog's index a choice model of CTADL's index analysis? For each app, FlowLog indexed the\n"
                    f"# benchmark's facts again with --all-outputs ({self.workers} workers, --str-intern), whose index outputs equal\n"
                    f"# the benchmark's FlowLog index (all but CTADLStats' {added} rows that the query adds). check_choice_model.py\n"
                    "# then ran Soufflé on the index analysis with each choice-domain relation restricted to FlowLog's\n"
                    "# witnesses (--ord-map from compare_jadx.py ords), and compared what it derived with FlowLog's database.\n"
                )
                for w, (forced, summary, model, _) in self.choice.items():
                    name = WORKLOADS[w]["filename"].removesuffix(".apk")
                    f.write(
                        f"{w} ({name}) | forced: "
                        + ", ".join(f"{r} {n}" for r, n in forced)
                        + f" | {summary} | {model}\n"
                    )

        measured = [r for r in rows if r["mode"] == "measure" and r["status"] == "ok"]
        runs = collections.Counter(
            (r["workload"], r["backend"], r["phase"]) for r in measured
        )
        # A median of one run is that run: only medians of several runs deviate.
        dev = [
            abs(
                float(r["wall_seconds"])
                / self.med[r["workload"], r["backend"], r["phase"], "wall_seconds"]
                - 1
            )
            for r in measured
            if runs[r["workload"], r["backend"], r["phase"]] > 1
        ]
        manifests = list(self.manifest.values())
        start = min(m["started_at"] for m in manifests)[:16].replace("T", " ")
        end = max(m.get("finished_at") or m["started_at"] for m in manifests)[
            :16
        ].replace("T", " ")
        end = end[11:] if end[:10] == start[:10] else end
        s = self.setup
        frontends = ", ".join(
            f"{k} {s[k]}" for k in ("Ghidra", "JDK", "JADX plugin") if k in s
        )
        text = [
            f"# Soufflé and FlowLog on CTADL's workloads, {self.workers} workers",
            "",
            f"{count(len(self.order), 'workload')}, one `benchmark/run.py` run each by `benchmark/sweep.sh`,"
            f" from {start} to {end} UTC. Generated by `benchmark/report.py`.",
            "",
            "## Setup",
            "",
            "| | |",
            "|---|---|",
            f"| CTADL | {s['CTADL']} |",
            f"| FlowLog | {s['FlowLog']}, rustc {s.get('rustc', '?')}, string interning, SQLite I/O {s['SQLite I/O']} |",
            f"| Soufflé | {s['Soufflé']} |",
        ]
        text += [f"| Frontends | {frontends} |"] if frontends else []
        text += [
            f"| Runs | {self.workers} workers; for each engine, index then query, {s['runs']}; timeout {s['timeout']} |",
            "| Machine | "
            + ", ".join(s[k] for k in ("CPU", "CPUs", "memory", "system") if k in s)
            + " |",
            "",
            "Times are the medians of the measured runs"
            + (
                f"; {sum(x > 0.05 for x in dev)} of the {len(dev)} are more than 5% off their median,"
                f" none more than {max(dev) * 100:.0f}%."
                if dev
                else "."
            ),
            "`wall` is the whole `ctadl index` or `ctadl query` command; `engine` is the analysis binary, with its",
            "SQLite reads and writes. Peak RSS is the process tree's, the larger of index and query. x is Soufflé's",
            "time over FlowLog's: above 1, FlowLog is faster.",
            "",
            "## Suites",
            "",
            "Geometric means of the workloads' ratios; the totals add up the medians of index and query.",
            "",
            *self.suites(),
            "",
            "## Workloads",
            "",
            "Each suite is ordered by Soufflé's index time. Relations: equal / different, of the databases",
            "compared relation by relation (`comparisons.txt`); equal includes equal up to choice-domain",
            "witnesses, and differences in choice-domain relations are expected. SARIF: equal, or the same",
            "results with other code flows or statistics; taint-front workloads have no SARIF step.",
            "",
            *self.workloads(),
            "",
        ]
        if self.choice:
            text += [
                "## Choice model",
                "",
                "Is FlowLog's index a choice model of CTADL's index analysis (`choice-model.txt`)? Same index:",
                "FlowLog's `--all-outputs` index equals the benchmark's, but for the CTADLStats rows of the query.",
                "Relations: of FlowLog's database, those Soufflé derives with each choice-domain relation",
                "restricted to FlowLog's witnesses, equal / different.",
                "",
                "| Workload | Same index | Relations, equal / different | Choice model |",
                "|---|---|---|---|",
            ]
            for w, (_, summary, model, same) in self.choice.items():
                m = SUMMARY.search(summary)
                text.append(
                    f"| {w} | {'no' if same is None else 'yes'} | {m[1]} / {m[3]} "
                    f"| {'yes' if model.startswith('choice model: yes') else 'no'} |"
                    if m
                    else f"| {w} | {'no' if same is None else 'yes'} | - | no |"
                )
            text.append("")
        text += ["## Problems", ""]
        text += [f"- {p}" for p in self.problems] or ["None."]
        text += [
            "",
            "## Files",
            "",
            "| File | |",
            "|---|---|",
            f"| `measurements.csv` | all {len(rows)} records of the runs, warm-up and measured, with the fields of"
            " `run.py`'s `measurements.csv` but the log paths |",
            "| `comparisons.txt` | the database and SARIF comparisons of the warm-up runs, without the equal"
            f" relations, with {EXAMPLES} example rows per side of a difference |",
        ]
        if self.choice:
            text.append(
                f"| `choice-model.txt` | the choice-model checks of {count(len(self.choice), 'app')} |"
            )
        (out / "README.md").write_text("\n".join(text) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Report of a benchmark/sweep.sh sweep.")
    ap.add_argument(
        "sweep",
        nargs="?",
        type=Path,
        default=ROOT / "benchmark/runs/sweep",
        help="sweep directory (default benchmark/runs/sweep)",
    )
    ap.add_argument(
        "-o", "--out", type=Path, help="report directory (default SWEEP/report)"
    )
    args = ap.parse_args(argv)
    if not (args.sweep / "status.tsv").exists():
        sys.exit(f"report.py: no {args.sweep / 'status.tsv'}")
    report = Report(args.sweep)
    if not report.order:
        sys.exit(f"report.py: no runs in {args.sweep / 'status.tsv'}")
    out = args.out or args.sweep / "report"
    out.mkdir(parents=True, exist_ok=True)
    report.write(out)
    print("\n".join(report.suites()))
    print("".join(f"\nproblem: {p}" for p in report.problems) or "\nno problems")
    print(f"report: {out / 'README.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
