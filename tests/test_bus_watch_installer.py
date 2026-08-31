from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY_ROOT / "scripts" / "bus-watch" / "install-floati-bus-watch.py"
SOURCE = REPOSITORY_ROOT / "scripts" / "bus-watch" / "floati-bus-watch.ts"


class BusWatchInstallerTests(unittest.TestCase):
    def test_install_writes_exact_copy_and_source_sha_sidecar(self) -> None:
        """Catches an install that leaves the deployed watcher provenance-free."""

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            environment = dict(os.environ)
            environment["HOME"] = str(home)

            result = subprocess.run(
                [sys.executable, str(INSTALLER)],
                cwd=REPOSITORY_ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            artifact = json.loads(result.stdout)
            destination = home / ".config" / "opencode" / "plugins" / "floati-bus-watch.ts"
            sidecar = destination.with_suffix(".sha256")
            source_bytes = SOURCE.read_bytes()
            source_sha = hashlib.sha256(source_bytes).hexdigest()
            self.assertEqual(source_bytes, destination.read_bytes())
            self.assertEqual(source_sha + "\n", sidecar.read_text(encoding="utf-8"))
            self.assertEqual(0o644, stat.S_IMODE(destination.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(sidecar.stat().st_mode))
            self.assertEqual("ok", artifact["status"])
            self.assertEqual(source_sha, artifact["evidence"]["source_sha256"])
            self.assertEqual(str(destination), artifact["evidence"]["destination"])
            self.assertEqual(str(sidecar), artifact["evidence"]["sidecar"])


if __name__ == "__main__":
    unittest.main()
