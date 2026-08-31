"""F3 roster parity battery — BEHAVIORAL conformance, per harness.

The name-surface oracle (test_roster_adapters) proves must-not-lack at
the attribute level. THIS battery proves each roster adapter INHERITS
every reference discipline behaviorally, over real subprocesses with
fake absolute-path harness scripts:

  stderr-evidence · bounded read · permission-marker refusal ·
  typed failure causes · native cancel · process-group isolation

No live harness binary is claimed anywhere: surface_verified stays
False (charter override 5); the fake scripts exercise the inherited
code paths without inventing vendor argument spellings.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from floati.adapters.cline import ClineAdapter
from floati.adapters.cursor import CursorAgentAdapter
from floati.adapters.grok_build import GrokBuildAdapter
from floati.adapters.headless_template import (
    EVIDENCE_DIRECTORY,
    HeadlessProfileAdapter,
    HarnessProfile,
    MAX_PROFILE_OUTPUT_BYTES,
    ProfileHandle,
)
from floati.adapters.opencode import OpenCodeAdapter
from floati.adapters.pi_observation import PiObservationAdapter
from floati.adapters.zcode import ZcodeAdapter
from floati.workers import WorkerAdapterFailure
from tests.temp_roots import REAL_TEMP_ROOT

ROSTER = [
    ("grok-build", GrokBuildAdapter),
    ("cursor", CursorAgentAdapter),
    ("opencode", OpenCodeAdapter),
    ("cline", ClineAdapter),
    ("pi-observation", PiObservationAdapter),
    ("zcode", ZcodeAdapter),
]


def _fake_harness(dirpath: Path, body: str) -> tuple[str, ...]:
    script = dirpath / "fake-harness.sh"
    script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return (str(script),)


class RosterParityBattery(unittest.TestCase):
    """CHECK-ONE, behavioral: one law, every roster member, really run."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.parent = self.tmp / "floati-work"
        # the workspace parent is patched per-module because the template
        # from-imports its own binding (house pattern from claude tests)
        for module in (
            "floati.adapters.codex_live",
            "floati.adapters.headless_template",
        ):
            patcher = mock.patch(module + "._WORKSPACE_PARENT", self.parent)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _adapter(self, name: str, cls, body: str, **profile_kwargs):
        command = _fake_harness(self.tmp, body)
        profile = HarnessProfile(
            name=name,
            command=command,
            headless_arguments=(),
            stderr_name=f"{name}.stderr",
            **profile_kwargs,
        )
        return cls(profile)

    def _spawn_drive(self, adapter, work_id):
        item = {
            "id": work_id,
            "workspace": str(self.parent / work_id),
            "title": "battery",
        }
        handle = adapter.spawn(item, deadline_seconds=30)
        try:
            result = adapter.drive(handle, deadline_seconds=30)
        finally:
            adapter.cancel()
        return handle, result

    def test_full_round_trip_yields_payload_stderr_evidence(self):
        for name, cls in ROSTER:
            with self.subTest(adapter=name):
                adapter = self._adapter(
                    name, cls,
                    'echo "to stderr" >&2\n'
                    'printf \'{"status":"done"}\'\n',
                )
                handle, result = self._spawn_drive(adapter, work_id=f"w-{name}")
                self.assertEqual(result["return_code"], 0)
                self.assertEqual(result["payload"]["status"], "done")
                self.assertEqual(result["work_id"], f"w-{name}")
                stderr_file = (
                    handle.filesystem_workspace / EVIDENCE_DIRECTORY
                    / f"{name}.stderr"
                )
                self.assertTrue(stderr_file.is_file())
                self.assertIn(b"to stderr", stderr_file.read_bytes())

    def test_permission_marker_refuses_even_on_clean_exit(self):
        for name, cls in ROSTER:
            with self.subTest(adapter=name):
                adapter = self._adapter(
                    name, cls,
                    'printf \'{"needs":"approval"}\'\n',
                )
                with self.assertRaises(WorkerAdapterFailure):
                    self._spawn_drive(adapter, work_id=f"w-perm-{name}")

    def test_nonzero_exit_is_process_failed_distinct_from_permission(self):
        """Law 12 per roster: an infra/content failure is NOT reported as
        a permission refusal — the two causes stay distinguishable."""
        for name, cls in ROSTER:
            with self.subTest(adapter=name):
                adapter = self._adapter(name, cls, "exit 3\n")
                with self.assertRaises(WorkerAdapterFailure) as failed:
                    self._spawn_drive(adapter, work_id=f"w-{name}")
                self.assertIn("process_failed", failed.exception.args)
                marked = self._adapter(
                    name, cls,
                    'echo "requires user" >&2\nexit 3\n',
                )
                with self.assertRaises(WorkerAdapterFailure) as permitted:
                    self._spawn_drive(marked, work_id=f"w-pm-{name}")
                self.assertIn("permission_required", permitted.exception.args)
                self.assertNotIn(
                    "process_failed", permitted.exception.args,
                    f"{name}: permission death collapsed into process death",
                )

    def test_output_limit_exceeded_refuses_typed(self):
        for name, cls in ROSTER:
            with self.subTest(adapter=name):
                # emit strictly more than the bound, then block forever on
                # stdout so nothing else ends the read first
                adapter = self._adapter(
                    name, cls,
                    f'head -c {MAX_PROFILE_OUTPUT_BYTES + 1} '
                    '/dev/zero\n'
                    'sleep 30\n',
                )
                item = {
                    "id": f"w-big-{name}",
                    "workspace": str(self.parent / f"w-big-{name}"),
                    "title": "battery",
                }
                handle = adapter.spawn(item, deadline_seconds=60)
                with self.assertRaises(WorkerAdapterFailure):
                    adapter.drive(handle, deadline_seconds=60)
                adapter.cancel()

    def test_process_group_isolated_by_default(self):
        probe = (
            'python3 -c \'import os;print(os.getpgid(0))\'\n'
        )
        for name, cls in ROSTER:
            with self.subTest(adapter=name):
                isolated = self._adapter(name, cls, probe)
                _, result = self._spawn_drive(
                    isolated, work_id=f"w-iso-{name}")
                child_pgid = int(result["payload"])
                self.assertNotEqual(
                    child_pgid, os.getpgid(0),
                    f"{name}: child shared the parent process group",
                )

    def test_prompt_shape_is_measured_per_member(self):
        """K4 finding: the template's `-- <title>` idiom is not universal —
        zcode refuses it and needs `--prompt <title>` (measured live,
        capture attempt1-stderr-helpdump.txt). Each member's DEFAULT
        profile declares its prompt form; the battery proves the spawned
        argv really carries it. Members without a measurement inherit the
        template default `--`."""
        for name, cls in ROSTER:
            with self.subTest(adapter=name):
                form = tuple(cls._default_profile().prompt_form)
                command = _fake_harness(
                    self.tmp, 'printf \'%s\\n\' "$@"\n')
                profile = HarnessProfile(
                    name=name,
                    command=command,
                    headless_arguments=(),
                    stderr_name=f"{name}.stderr",
                    prompt_form=form,
                )
                adapter = cls(profile)
                _, result = self._spawn_drive(
                    adapter, work_id=f"w-prompt-{name}")
                argv_lines = result["payload"]["raw"].splitlines()
                pair = list(form) + ["battery"]
                self.assertIn(
                    pair, [argv_lines[i:i + len(pair)]
                           for i in range(len(argv_lines))],
                    f"{name}: spawned argv does not carry the declared "
                    f"prompt form {form} + title")

    def test_template_default_prompt_form_is_the_unmeasured_idiom(self):
        """K4 companion pin: the template default stays `--` — a member
        that has not MEASURED its prompt form must not silently inherit a
        different idiom. Only a measured declaration moves off default."""
        profile = HarnessProfile(
            name="unmeasured", command=("/opt/h/unmeasured",),
            headless_arguments=(), stderr_name="unmeasured.stderr")
        self.assertEqual(("--",), tuple(profile.prompt_form))

    def test_roster_constructors_default_to_isolation(self):
        """P3 lesson: the TEMPLATE's default is overridden by every roster
        ctor's own keyword default, so a template-layer flip is invisible.
        The enforcement point is the roster signature — pinned here."""
        import inspect

        for name, cls in ROSTER:
            with self.subTest(adapter=name):
                sig = inspect.signature(cls.__init__)
                default = sig.parameters["isolate_process_group"].default
                self.assertIs(
                    default, True,
                    f"{name}: constructor no longer defaults to "
                    "process-group isolation",
                )

    def test_disable_isolation_shares_parent_group(self):
        for name, cls in ROSTER[:1]:
            with self.subTest(adapter=name):
                command = _fake_harness(
                    self.tmp,
                    'python3 -c \'import os;print(os.getpgid(0))\'\n',
                )
                profile = HarnessProfile(
                    name=name, command=command,
                    headless_arguments=(),
                    stderr_name=f"{name}.stderr",
                )
                adapter = cls(profile, isolate_process_group=False)
                _, result = self._spawn_drive(
                    adapter, work_id=f"w-join-{name}")
                self.assertEqual(int(result["payload"]), os.getpgid(0))

    def test_disable_isolation_cancels_only_the_child_process(self):
        profile = HarnessProfile(
            name="grok-build",
            command=_fake_harness(self.tmp, "sleep 30\n"),
            headless_arguments=(),
            stderr_name="grok-build.stderr",
        )
        adapter = GrokBuildAdapter(
            profile, isolate_process_group=False,
        )
        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 4242
        process.wait.side_effect = subprocess.TimeoutExpired(
            cmd="fake-harness", timeout=10,
        )
        adapter._active_process = process

        with mock.patch(
            "floati.adapters.headless_template.os.getpgid",
            return_value=4242,
        ), mock.patch(
            "floati.adapters.headless_template.os.killpg",
        ) as killpg:
            adapter.cancel()

        killpg.assert_not_called()
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertIsNone(adapter._active_process)

    def test_drive_converts_relative_timeout_to_absolute_deadline(self):
        adapter = self._adapter(
            "grok-build", GrokBuildAdapter, 'printf \'{}\'\n',
        )
        process = mock.Mock(spec=subprocess.Popen)
        handle = ProfileHandle(
            work_id="w-deadline",
            workspace=self.tmp,
            filesystem_workspace=self.tmp,
            process=process,
            stderr_path=self.tmp / "stderr",
            deadline=125.0,
            git_identity=(1, 2, 3),
        )

        with mock.patch(
            "floati.adapters.headless_template.time.monotonic",
            return_value=100.0,
        ), mock.patch.object(
            adapter, "_read_bounded", return_value=b"{}",
        ) as read_bounded, mock.patch.object(
            adapter, "_wait", return_value=0,
        ) as wait, mock.patch.object(adapter, "_validate_result"):
            adapter.drive(handle, deadline_seconds=30)

        read_bounded.assert_called_once_with(process, 125.0)
        wait.assert_called_once_with(process, 125.0)

    def test_native_cancels_a_running_child(self):
        for name, cls in ROSTER:
            with self.subTest(adapter=name):
                adapter = self._adapter(name, cls, "sleep 30\n")
                item = {
                    "id": f"w-slow-{name}",
                    "workspace": str(self.parent / f"w-slow-{name}"),
                    "title": "battery",
                }
                handle = adapter.spawn(item, deadline_seconds=30)
                started = time.monotonic()
                adapter.cancel()
                elapsed = time.monotonic() - started
                self.assertIsNone(adapter._active_process)
                self.assertLess(elapsed, 15)
                self.assertIsNotNone(handle.process.poll())


if __name__ == "__main__":
    unittest.main()
