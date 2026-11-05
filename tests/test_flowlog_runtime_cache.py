import tempfile
import unittest
from pathlib import Path

from ctadl.flowlog import runtime_fingerprint


class TestRuntimeCache(unittest.TestCase):
    def test_sources_and_dependency_lock_invalidate_but_build_products_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            runtime = workspace / "flowlog-runtime"
            (runtime / "src/operators").mkdir(parents=True)
            source = runtime / "src/operators/example.rs"
            source.write_text("first")
            before = runtime_fingerprint(runtime)
            (runtime / "target").mkdir()
            (runtime / "target/generated.rs").write_text("temporary build output")
            self.assertEqual(before, runtime_fingerprint(runtime))
            source.write_text("second")
            changed = runtime_fingerprint(runtime)
            self.assertNotEqual(before, changed)
            (workspace / "Cargo.lock").write_text("new dependency resolution")
            self.assertNotEqual(changed, runtime_fingerprint(runtime))


if __name__ == "__main__":
    unittest.main()
