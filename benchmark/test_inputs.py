import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import inputs


class TestInputs(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("dpkg-deb") and shutil.which("curl"), "Debian input tools required"
    )
    def test_same_basename_uses_pinned_binary_not_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package"
            for name in ("DEBIAN", "usr/bin", "etc"):
                (package / name).mkdir(parents=True)
            (package / "DEBIAN/control").write_text(
                "Package: native-input-test\nVersion: 1.0\nArchitecture: all\n"
                "Maintainer: Test <test@example.invalid>\nDescription: Test input\n"
            )
            binary = b"pinned workload bytes"
            (package / "usr/bin/workload").write_bytes(binary)
            (package / "etc/workload").write_text("different configuration bytes")
            archive = root / "input.deb"
            subprocess.run(
                ["dpkg-deb", "--build", package, archive],
                check=True,
                capture_output=True,
            )
            row = {
                "id": "test",
                "filename": "workload",
                "member": "workload",
                "deb": "input.deb",
                "urls": [archive.as_uri()],
                "sha256": hashlib.sha256(binary).hexdigest(),
            }
            with patch.object(inputs, "HERE", root / "benchmark"):
                result = inputs.prepare_input(
                    row, dict(os.environ), root / "logs", False
                )
            self.assertEqual(result.read_bytes(), binary)


if __name__ == "__main__":
    unittest.main()
