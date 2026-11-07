import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run as benchmark_run
import compare_db
import run_taintfront
from run_taintfront import comparison_equal


class TestComparison(unittest.TestCase):
    def test_taintfront_does_not_accept_choice_witnesses_or_report_differences(self):
        self.assertTrue(
            comparison_equal("summary: 120 equal, 0 different; sarif: equal")
        )
        self.assertFalse(
            comparison_equal(
                "summary: 120 equal (1 up to choice-domain witnesses), 0 different; sarif: equal"
            )
        )
        self.assertFalse(
            comparison_equal("summary: 120 equal, 0 different; sarif: DIFFERENT")
        )
        self.assertFalse(comparison_equal("comparison failed"))

    def test_taintfront_variants_are_not_held_to_default_option_expectations(self):
        commands = []

        def run(cmd, log, cwd=None, env=None):
            commands.append([str(c) for c in cmd])
            return int(str(cmd[1]).endswith("sarif_has_code_flows.py")), 0.0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "t.tnt").touch()
            (root / "t.codeflows").touch()
            argv = ["run_taintfront.py", "--backend", "flowlog", "--testdir", tmp]
            argv += ["--out", str(root / "out")]
            with patch.object(run_taintfront, "run", run), redirect_stdout(io.StringIO()):
                with patch.object(sys, "argv", argv):
                    self.assertEqual(run_taintfront.main(), 1)
                with patch.object(sys, "argv", argv + ["--variants", "star,qall"]):
                    self.assertEqual(run_taintfront.main(), 0)
            for variant in ("star", "qall"):
                self.assertTrue((root / "out" / variant / "t/flowlog/phase").exists())
        tails = [c[-2:] for c in commands]
        self.assertIn(["--star", "--match-star-fields"], tails)
        self.assertIn(["--compute-slices=all", "--format=sarif+all"], tails)

    def test_shared_dictionary_views_exclude_internal_metadata(self):
        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE TABLE R(value)")
            with self.assertRaisesRegex(ValueError, "shared-dictionary"):
                compare_db.relations(db)
            db.executescript("""
                DROP TABLE R;
                CREATE TABLE __SymbolTable(id INTEGER PRIMARY KEY, symbol TEXT);
                INSERT INTO __SymbolTable VALUES(1, 'value');
                CREATE TABLE _R("0" INTEGER);
                INSERT INTO _R VALUES(1);
                CREATE VIEW R AS SELECT symbol FROM _R JOIN __SymbolTable ON "0"=id;
                CREATE TABLE __FlowLogRecord(id INTEGER, fields TEXT);
                CREATE TABLE __FlowLogNativeSchema(relation TEXT, columns_json TEXT);
            """)
            self.assertEqual(compare_db.relations(db), {"R": "R"})
            self.assertEqual(compare_db.rows(db, "R"), [("value",)])
        finally:
            db.close()

    def test_success_exit_with_only_choice_witness_differences_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def comparison_command(argv, directory, **kwargs):
                directory.mkdir(parents=True)
                (directory / "stdout.log").write_text(
                    "WITNESS  LostAssign: 1 row, same choice-domain keys\n"
                    "summary: 1 equal (1 up to choice-domain witnesses), 0 different\n"
                )
                return {"status": "ok", "exit_code": 0}

            with patch.object(benchmark_run, "command", comparison_command):
                verdict = benchmark_run.compare(
                    root / "souffle",
                    root / "flowlog",
                    root / "check",
                    {"id": "example", "lang": "taint-front", "format": "summary"},
                    {},
                    root,
                    SimpleNamespace(timeout=10),
                )
            self.assertEqual(verdict, "different")
            self.assertEqual(
                json.loads((root / "check/comparison.json").read_text())["status"],
                "different",
            )
