import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ctadl import timings


class TestTimings(unittest.TestCase):
    def test_exception_is_preserved_and_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "timings.jsonl"
            with timings.record_to(log):
                with self.assertRaisesRegex(ValueError, "original error"):
                    with timings.span("engine", engine="flowlog"):
                        raise ValueError("original error")
            record = json.loads(log.read_text())
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["phase"], "engine")
            self.assertGreaterEqual(record["seconds"], 0)

    def test_logs_keep_raw_bytes_and_do_not_collide(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name, output in (("index", b"\x00index\xff"), ("query", b"query\n")):
                with timings.record_to(base / f"{name}.jsonl"):
                    timings.subprocess_result(
                        subprocess.CompletedProcess(
                            ["analysis", "a b"], 7, output, b"failure"
                        ),
                        0.5,
                    )
                record = json.loads((base / f"{name}.jsonl").read_text())
                self.assertEqual(record["exit_code"], 7)
                self.assertEqual(
                    (base / record["streams"]["stdout"]).read_bytes(), output
                )
            self.assertEqual(
                (base / "index.jsonl.commands/command-001.stdout.log").read_bytes(),
                b"\x00index\xff",
            )

    def test_disabled_recording_keeps_exceptions(self):
        with timings.record_to(None):
            with self.assertRaisesRegex(RuntimeError, "unchanged"):
                with timings.span("analysis"):
                    raise RuntimeError("unchanged")


if __name__ == "__main__":
    unittest.main()
