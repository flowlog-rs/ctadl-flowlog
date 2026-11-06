import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from records import command, phase_metrics


class TestRecords(unittest.TestCase):
    def test_legacy_adapter_timing_is_rejected_by_native_campaign(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "timings.jsonl").write_text(
                "\n".join(
                    json.dumps(
                        {"kind": "phase", "phase": phase, "status": "ok", "seconds": 1}
                    )
                    for phase in [
                        "prepare_inputs",
                        "engine",
                        "write_outputs",
                        "analysis",
                    ]
                )
            )
            with self.assertRaisesRegex(ValueError, "native SQLite"):
                phase_metrics(
                    directory,
                    {"status": "ok", "wall_seconds": 4},
                    expected_sqlite_io="native-shared-dictionary-v1",
                )

    def test_nonzero_exit_and_raw_streams_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "failure"
            result = command(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'out\\x00'); sys.stderr.write('err'); sys.exit(7)",
                ],
                directory,
                cwd=Path(tmp),
                env=os.environ.copy(),
                timeout=10,
            )
            self.assertEqual((result["status"], result["exit_code"]), ("failed", 7))
            self.assertEqual((directory / "stdout.log").read_bytes(), b"out\x00")
            self.assertEqual((directory / "stderr.log").read_text(), "err")
            self.assertIsNone(phase_metrics(directory, result)["engine_seconds"])

    def test_timeout_stops_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "timeout"
            marker = Path(tmp) / "unexpected"
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(30)"
            result = command(
                [sys.executable, "-c", parent, child],
                directory,
                cwd=Path(tmp),
                env=os.environ.copy(),
                timeout=0.2,
            )
            self.assertEqual(result["status"], "timeout")
            import time

            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_missing_metrics_and_compilation_are_not_valid_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            log = directory / "timings.jsonl"
            log.write_text("")
            with self.assertRaises(ValueError):
                phase_metrics(directory, {"status": "ok", "wall_seconds": 1})
            log.write_text(json.dumps({"kind": "phase", "phase": "compile"}) + "\n")
            with self.assertRaisesRegex(ValueError, "Compilation"):
                phase_metrics(
                    directory, {"status": "ok", "wall_seconds": 1}, measured=True
                )


if __name__ == "__main__":
    unittest.main()
