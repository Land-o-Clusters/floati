"""FQ-11 / BL-1: lease-check.sh sees pytest (and kin), not only unittest.

The subject filter stays: argv[0] must be a python binary. The argument
filter widens to unittest|pytest|xctest|swift test|swift-build. RED: a
fake pytest sleeper is invisible to the unittest-only filter and BUSY
after the widen. Am.2: the instrument is the sleeper's own pid on the
BUSY line, never the host-wide suite-process total.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.temp_roots import REAL_TEMP_ROOT

REPO = Path(__file__).resolve().parents[1]
LEASE_CHECK = REPO / "scripts" / "lease-check.sh"
WIDE_ARM = "*unittest*|*pytest*|*xctest*|*swift\\ test*|*swift-build*"


class LeaseCheckFq11Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def _run(self, script: Path) -> tuple[int, str]:
        completed = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode, completed.stdout

    def _start_pytest_sleeper(self) -> subprocess.Popen[str]:
        # Basename carries "pytest" so the widened case matches; argv[0]
        # stays a python binary so the subject filter still applies.
        sleeper = self.base / "pytest_sleeper.py"
        sleeper.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
        return subprocess.Popen(
            [sys.executable, str(sleeper)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def test_fake_pytest_sleeper_is_invisible_before_and_busy_after(self) -> None:
        self.assertTrue(LEASE_CHECK.is_file(), LEASE_CHECK)
        source = LEASE_CHECK.read_text(encoding="utf-8")
        self.assertIn("*python*|*Python*", source)
        self.assertIn(WIDE_ARM, source)

        narrow = self.base / "lease-check-unittest-only.sh"
        narrow.write_text(source.replace(WIDE_ARM, "*unittest*", 1), encoding="utf-8")
        narrow.chmod(narrow.stat().st_mode | stat.S_IEXEC)

        child = self._start_pytest_sleeper()

        def _stop_sleeper() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

        self.addCleanup(_stop_sleeper)

        # Host-wide suite totals are not the instrument: a foreign pytest
        # on the same box moves them. Pin the sleeper this test spawned.
        sleeper_busy = re.compile(rf"BUSY pid\s+{child.pid}\b")
        before_out = after_out = ""
        after_status = 0
        deadline = time.time() + 5
        while time.time() < deadline:
            _before_status, before_out = self._run(narrow)
            after_status, after_out = self._run(LEASE_CHECK)
            if sleeper_busy.search(after_out) and not sleeper_busy.search(before_out):
                break
            time.sleep(0.2)

        self.assertIsNone(
            sleeper_busy.search(before_out),
            "unittest-only filter must miss this sleeper pid\n" + before_out,
        )
        self.assertIsNotNone(
            sleeper_busy.search(after_out),
            "widened filter must name this sleeper pid on a BUSY line\n" + after_out,
        )
        self.assertGreaterEqual(after_status, 1, after_out)


    def test_a_pid_narrower_than_the_widest_on_the_box_is_still_counted(self) -> None:
        """LC-PID-1: `ps -Ao pid=,command=` right-aligns the pid column, so a
        process whose pid is narrower than the widest pid on the box arrives with
        LEADING SPACES. A parser that takes `${line%% *}` as the pid reads an
        empty pid and the wrong first word, and the process is silently not
        counted. Both hosted legs and the shared Mac (after its pid counter
        wrapped) reported `suite processes: 0` with a live pytest sleeper on the
        box - the instrument saw nothing and the count said quiet.

        Constructed with a fake `ps` first on PATH that prints one padded line
        and one unpadded line; both must be counted and both pids named.
        """

        fake_bin = self.base / "fake-bin"
        fake_bin.mkdir()
        fake_ps = fake_bin / "ps"
        fake_ps.write_text(
            "#!/bin/bash\n"
            "printf '%s\\n' "
            "'  4321 /usr/bin/python3 \x2ftmp/foreign/pytest_padded.py' "
            "'98765 /usr/bin/python3 \x2ftmp/foreign/pytest_wide.py'\n",
            encoding="utf-8",
        )
        fake_ps.chmod(fake_ps.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ)
        env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
        completed = subprocess.run(
            ["bash", str(LEASE_CHECK)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        busy = [line for line in completed.stdout.splitlines() if "BUSY" in line]
        self.assertRegex(
            "\n".join(busy), r"BUSY pid\s+4321\b",
            "the padded (narrower) pid must be counted and named\n" + completed.stdout,
        )
        self.assertRegex(
            "\n".join(busy), r"BUSY pid\s+98765\b",
            "the unpadded pid must still be counted\n" + completed.stdout,
        )
        self.assertEqual(2, completed.returncode, completed.stdout)


if __name__ == "__main__":
    unittest.main()
