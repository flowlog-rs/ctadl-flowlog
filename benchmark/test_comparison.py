import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run as benchmark_run


class TestComparison(unittest.TestCase):
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
