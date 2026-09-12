"""GATE of FQ-12 Am.2+Am.3, at gated tip 13f50379 - clean room.

The deciding questions, constructed by the gate from the brief:

G1  a fake /proc/loadavg reader yields ITS number: the hook's loadavg line
    and the load gate read the fake file's one-minute load, the fake sysctl
    never runs;
G2  a fake sysctl on PATH yields ITS number when /proc is absent;
G3  BOTH readers broken: loadavg=unavailable AND the job does not start -
    load-gate=held with verdict queued-behind-unread-load, typed deadline
    at lock_held=false, exit non-zero, no substituted 0 anywhere;
G4  a readable load still gates exactly as before: above max queues,
    below max permits.

What the gate could not construct: a real Linux kernel's own
/proc/loadavg - the reader is exercised through FLOATI_HOOK_PROC_LOADAVG
pointing at a file of identical shape, so the kernel-side difference
(never a readable file on macOS) is unproven here; said in the receipt.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPOSITORY_ROOT / "scripts" / "hooks-job-started.sh"


class HookGateWorld(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.lock = self.base / "shared-hook.lock"
        self.ours = self.base / "floati-harbor-gate.lock"
        self.lock.write_text("", encoding="utf-8")
        self.ours.write_text("", encoding="utf-8")
        self.bindir = self.base / "bin"
        self.bindir.mkdir()
        self.proc_fake = self.base / "no-such-proc-loadavg"

    def _run_hook(self, **extra: str) -> tuple[int, str]:
        environ = os.environ.copy()
        environ.update(
            {
                "HOME": str(self.base),
                "FLOATI_HOOK_LOCK_PATH": str(self.lock),
                "FLOATI_HARBOR_GATE_LOCK_PATH": str(self.ours),
                "FLOATI_HOOK_MAX_WAIT": "2",
                "FLOATI_HOOK_PROC_LOADAVG": str(self.proc_fake),
                "PATH": str(self.bindir) + os.pathsep + environ["PATH"],
            }
        )
        environ.update(extra)
        completed = subprocess.run(
            [str(HOOK)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=environ,
            timeout=30,
        )
        return completed.returncode, completed.stdout + completed.stderr

    def _fake_sysctl(self, body: str) -> None:
        fake = self.bindir / "sysctl"
        fake.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        fake.chmod(0o700)

    def _fake_proc(self, content: str) -> None:
        self.proc_fake.write_text(content, encoding="utf-8")


class GateLoadReaderQuestions(HookGateWorld):
    def test_g1_fake_proc_loadavg_reader_yields_its_number(self) -> None:
        self._fake_sysctl('echo "sysctl must not run" >&2; exit 3\n')
        self._fake_proc("7.75 0.10 0.00 1/100 1\n")

        code, combined = self._run_hook(FLOATI_HOOK_MAX_LOAD="999")

        self.assertEqual(0, code, combined)
        self.assertIn("loadavg=7.75", combined, combined)
        self.assertIn("load-gate=start-permitted", combined, combined)
        self.assertNotIn("sysctl must not run", combined)

    def test_g2_fake_sysctl_yields_its_number_when_proc_absent(self) -> None:
        self._fake_sysctl('echo "{ 4.25 0.00 0.00 }"\n')

        code, combined = self._run_hook(FLOATI_HOOK_MAX_LOAD="999")

        self.assertEqual(0, code, combined)
        self.assertIn("loadavg=4.25", combined, combined)

    def test_g3_both_readers_broken_yields_unavailable_and_never_starts(
        self,
    ) -> None:
        self._fake_sysctl(
            'echo "sysctl: huh?" >&2; exit 7\n'
        )

        code, combined = self._run_hook(FLOATI_HOOK_MAX_LOAD="999")

        self.assertNotEqual(0, code, combined)
        self.assertIn(
            "verdict=queued-behind-unread-load", combined, combined
        )
        self.assertIn(
            "hook_wait_deadline lock_held=false loadavg=unavailable",
            combined,
            combined,
        )
        self.assertNotIn("load-gate=start-permitted", combined, combined)
        self.assertNotIn("loadavg=0", combined, combined)
        self.assertNotIn("sysctl: huh?", combined, combined)


class GateReadableLoadStillGatesAsBefore(HookGateWorld):
    def test_g4_readable_load_above_max_still_queues(self) -> None:
        self._fake_proc("9.50 0.00 0.00 1/100 1\n")

        code, combined = self._run_hook(FLOATI_HOOK_MAX_LOAD="2")

        self.assertNotEqual(0, code, combined)
        self.assertIn("load-gate=held load1=9.50 max=2", combined, combined)
        self.assertIn("verdict=queued-behind-load", combined, combined)
        self.assertIn(
            "hook_wait_deadline lock_held=false loadavg=9.50", combined, combined
        )

    def test_g4_readable_load_below_max_still_permits(self) -> None:
        self._fake_proc("1.25 0.00 0.00 1/100 1\n")

        code, combined = self._run_hook(FLOATI_HOOK_MAX_LOAD="2")

        self.assertEqual(0, code, combined)
        self.assertIn("load-gate=start-permitted load1=1.25 max=2", combined, combined)


if __name__ == "__main__":
    unittest.main()
