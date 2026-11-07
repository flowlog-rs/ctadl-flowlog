import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

import report

FIELDS = (
    "workload,language,backend,workers,repetition,mode,phase,status,exit_code,wall_seconds,"
    "analyze_seconds,engine_seconds,sqlite_io,process_peak_rss_mib,logs"
).split(",")


def fake_run(path, w, lang, wall, timeout=()):
    """A run of one workload as run.py leaves it. wall maps (engine, phase) to the measured
    median; the warm-up takes 5 times as long, the 3 measured runs 1, 1.1 and 0.9 times.
    """
    rows = []
    for rep, factor in enumerate((5, 1, 1.1, 0.9)):
        for e in ("souffle", "flowlog"):
            for p in ("index", "query"):
                x = wall[e, p] * factor
                rows.append(
                    dict(
                        zip(
                            FIELDS,
                            [w, lang, e, 16, rep, "measure" if rep else "warmup", p]
                            + [
                                "timeout" if (e, p, rep) in timeout else "ok",
                                0,
                                x,
                                x,
                                x / 2,
                            ]
                            + [
                                "souffle" if e == "souffle" else "native",
                                1024 * (1 + (e == "flowlog")),
                            ]
                            + [f"workloads/{w}/{e}/workers-16/rep-{rep:03d}/{p}"],
                        )
                    )
                )
    path.mkdir(parents=True)
    with (path / "measurements.csv").open("w") as f:
        writer = csv.DictWriter(f, FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    machine = {
        "platform": "Linux-6.8.0-x86_64-with-glibc2.35",
        "cpu": "Model name:  Test CPU\n",
        "cpu_affinity": list(range(24)),
        "memory": "MemTotal: 226760420 kB\n",
    }
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "started_at": "2026-11-07T07:05:27+00:00",
                "finished_at": "2026-11-07T07:20:59+00:00",
                "ctadl": {"commit": "8e2887dcaa13", "status": ""},
                "flowlog": {
                    "commit": "6d9a2b8532ed",
                    "rustc": "rustc 1.97.1 (x)",
                    "sqlite_io": "native",
                },
                "versions": {"souffle": "Version: 2.5\n"},
                "machine": machine,
                "options": {
                    "workers": [16],
                    "reps": 3,
                    "timeout": 1800,
                    "backends": ["souffle", "flowlog"],
                },
            }
        )
    )
    return path


def fake_comparison(run, w, tool, db, sarif=None):
    c = run / "workloads" / w / "checks/workers-16/flowlog"
    (c / "database").mkdir(parents=True)
    (c / "comparison.json").write_text(json.dumps({"status": "different"}))
    (c / "database/command.json").write_text(
        json.dumps({"argv": ["python3", f"/x/benchmark/{tool}"]})
    )
    (c / "database/stdout.log").write_text(db)
    if sarif:
        (c / "sarif").mkdir()
        (c / "sarif/stdout.log").write_text(sarif)


class TestReport(unittest.TestCase):
    def test_partial_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweep = Path(tmp) / "sweep"
            diary = fake_run(
                Path(tmp) / "run-diary",
                "diary",
                "JADX",
                {
                    ("souffle", "index"): 4,
                    ("souffle", "query"): 2,
                    ("flowlog", "index"): 4,
                    ("flowlog", "query"): 2,
                },
                timeout=[("flowlog", "query", 3)],
            )
            syn10 = fake_run(
                Path(tmp) / "run-syn10",
                "syn10",
                "taint-front",
                {
                    ("souffle", "index"): 12,
                    ("souffle", "query"): 6,
                    ("flowlog", "index"): 6,
                    ("flowlog", "query"): 3,
                },
            )
            fake_comparison(
                diary,
                "diary",
                "compare_jadx.py",
                "A: /x/a.db\nB: /x/b.db\nsummary: 160 equal (3 up to choice-domain witnesses), 6 different\n",
                "sarif: different (2 places); same results up to code flows\n",
            )
            fake_comparison(
                syn10, "syn10", "compare_db.py", "summary: 117 equal, 3 different\n"
            )
            check = sweep / "choice-model/diary"
            check.mkdir(parents=True)
            (check / "run").write_text(f"{diary}\n")
            (check / "same-index.txt").write_text(
                "DIFF     CTADLStats: souffle 40, flowlog 34, souffle-only 6, flowlog-only 0\n"
                "summary: 122 equal, 1 different\n"
            )
            (check / "check.log").write_text(
                "forced   VirtualAssign: 9416 FlowLog witnesses\nrenamed  39 clause heads\n"
                "summary: 164 equal, 0 different\nchoice model: yes, Souffle derives it\n"
            )
            (sweep / "status.tsv").write_text(
                "workload\texit\tstarted\tfinished\trun\n"
                f"diary\t4\t-\t-\t{diary}\n"
                f"syn10\t1\t-\t-\t{Path(tmp) / 'deleted'}\n"
                f"syn10\t4\t-\t-\t{syn10}\n"
                "libz\t2\t-\t-\t-\n"
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(report.main([str(sweep)]), 0)
            out = sweep / "report"
            readme = (out / "README.md").read_text()

            # Synthetic first; the latest of syn10's attempts; diary's query without the timeout.
            self.assertIn(
                "| Synthetic | syn10 | 12.0 / 6.0 | 2.00 | 6.0 / 3.0 | 2.00 | 2.00 / 2.00 | 1.0 / 2.0 | 117 / 3 | - |",
                readme,
            )
            self.assertIn(
                "| JADX | diary | 4.0 / 4.0 | 1.00 | 2.0 / 2.1 | 0.95 | 1.00 / 0.95 | 1.0 / 2.0 | 160 / 6 | same results |",
                readme,
            )
            self.assertIn(
                "| All | 2 | 1.41 | 1.38 | 1.41 | 1.38 | 24 / 15 | 1.59 | 2.0 |", readme
            )
            self.assertIn(
                "- diary: flowlog query, 2 of 3 measured runs ok (run 3 timeout)",
                readme,
            )
            self.assertIn("- libz: the latest attempt exited 2 (error)", readme)
            self.assertNotIn("- syn10", readme)
            self.assertIn("| diary | yes | 164 / 0 | yes |", readme)
            self.assertNotIn(tmp, "".join(p.read_text() for p in out.iterdir()))

            with (out / "measurements.csv").open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 32)
            self.assertNotIn("logs", rows[0])
            self.assertEqual(
                [rows[0]["workload"], rows[-1]["workload"]], ["syn10", "diary"]
            )
            self.assertEqual(
                (out / "choice-model.txt").read_text().splitlines()[-1],
                "diary (org.billthefarmer.diary_1105) | forced: VirtualAssign 9416"
                " | summary: 164 equal, 0 different | choice model: yes, Souffle derives it",
            )

    def test_trim_keeps_two_examples_per_side(self):
        lines = [
            "A: /x/a.db",
            "DIFF     R: souffle 3, flowlog 3, souffle-only 3, flowlog-only 3",
        ]
        lines += [f"           - ({i},)" for i in range(3)] + [
            f"           + ({i},)" for i in range(3)
        ]
        lines += ["DIFF     S: " + "x" * 300, "           - (9,)"]
        self.assertEqual(
            report.trim(lines),
            [
                lines[1],
                *lines[2:4],
                *lines[5:7],
                "DIFF     S: " + "x" * 185 + "...",
                lines[-1],
            ],
        )

    def test_same_index_allows_only_the_query_stats(self):
        stats = "DIFF     CTADLStats: souffle 40, flowlog 34, souffle-only 6, flowlog-only 0\n"
        self.assertEqual(
            report.same_index(stats + "summary: 122 equal, 1 different\n"), 6
        )
        self.assertEqual(report.same_index("summary: 123 equal, 0 different\n"), 0)
        witness = (
            "WITNESS  VirtualAssign: 9 rows, same choice-domain keys, other witnesses\n"
        )
        self.assertIsNone(
            report.same_index(
                witness
                + "summary: 123 equal (1 up to choice-domain witnesses), 0 different\n"
            )
        )
        self.assertIsNone(
            report.same_index(
                stats.replace("flowlog-only 0", "flowlog-only 1")
                + "summary: 122 equal, 1 different\n"
            )
        )


if __name__ == "__main__":
    unittest.main()
