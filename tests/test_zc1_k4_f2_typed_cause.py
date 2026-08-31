"""ZC1-K4-F2: markers inside a recognized help dump are quotation, not testimony."""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from floati.adapters.headless_template import (
    HeadlessProfileAdapter,
    HarnessProfile,
    _PERMISSION_MARKERS,
)
from floati.workers import WorkerAdapterFailure


# Measured fact from docs/evidence/gauntlet/captures/zc1-k4-zcode-live/attempt1-stderr-helpdump.txt
_ZCODE_HELP_DUMP_SIGNATURE = (
    "Usage:",
    "zcode [command] [options]",
    "--permission-mode",
)

_ZCODE_HELP_DUMP = """\
Unknown command: Reply with exactly: K4-LIVE-OK

zcode 0.16.5

Usage:
  zcode [command] [options]

Options:
  --mode <mode>    Permission mode for prompts: build, edit, plan, or yolo
  --permission-mode <mode>  Legacy permission mode alias: default, build, edit, plan, or yolo
"""


class _ZcodePinAdapter(HeadlessProfileAdapter):
    name = "zcode"


class Zc1K4F2TypedCauseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.parent = self.tmp / "floati-work"
        for module in (
            "floati.adapters.codex_live",
            "floati.adapters.headless_template",
        ):
            patcher = mock.patch(module + "._WORKSPACE_PARENT", self.parent)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_harness(self, body: str) -> tuple[str, ...]:
        script = self.tmp / "fake-harness.sh"
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(0o755)
        return (str(script),)

    def _zcode(self, body: str) -> _ZcodePinAdapter:
        profile = HarnessProfile(
            name="zcode",
            command=self._fake_harness(body),
            headless_arguments=(),
            stderr_name="zcode.stderr",
            help_dump_signature=_ZCODE_HELP_DUMP_SIGNATURE,
        )
        return _ZcodePinAdapter(profile)

    def _unsigned(self, body: str) -> HeadlessProfileAdapter:
        class _Unsigned(HeadlessProfileAdapter):
            name = "cursor"

        profile = HarnessProfile(
            name="cursor",
            command=self._fake_harness(body),
            headless_arguments=(),
            stderr_name="cursor-agent.stderr",
        )
        return _Unsigned(profile)

    def _drive(self, adapter: HeadlessProfileAdapter, work_id: str) -> None:
        item = {
            "id": work_id,
            "workspace": str(self.parent / work_id),
            "title": "battery",
        }
        handle = adapter.spawn(item, deadline_seconds=30)
        try:
            adapter.drive(handle, deadline_seconds=30)
        finally:
            adapter.cancel()

    def test_permission_marker_vocabulary_is_unchanged(self) -> None:
        self.assertEqual(
            ("approval", "permission", "requires user", "not allowed"),
            _PERMISSION_MARKERS,
        )

    def test_zcode_help_dump_refusal_is_process_failed_naming_the_dump(self) -> None:
        dump = self.tmp / "zcode-help.txt"
        dump.write_text(_ZCODE_HELP_DUMP, encoding="utf-8")
        adapter = self._zcode(f'cat "{dump}" >&2\nexit 3\n')
        with self.assertRaises(WorkerAdapterFailure) as failed:
            self._drive(adapter, work_id="w-zcode-dump")
        self.assertEqual("process_failed", failed.exception.code)
        self.assertTrue(failed.exception.help_dump_present)

    def test_zcode_genuine_marker_outside_a_dump_is_permission_required(self) -> None:
        adapter = self._zcode('echo "requires user" >&2\nexit 3\n')
        with self.assertRaises(WorkerAdapterFailure) as failed:
            self._drive(adapter, work_id="w-zcode-consent")
        self.assertEqual("permission_required", failed.exception.code)
        self.assertFalse(failed.exception.help_dump_present)

    def test_adapter_with_no_signature_still_types_help_dump_as_permission(self) -> None:
        dump = self.tmp / "unsigned-help.txt"
        dump.write_text(_ZCODE_HELP_DUMP, encoding="utf-8")
        adapter = self._unsigned(f'cat "{dump}" >&2\nexit 3\n')
        with self.assertRaises(WorkerAdapterFailure) as failed:
            self._drive(adapter, work_id="w-unsigned-dump")
        self.assertEqual("permission_required", failed.exception.code)
        self.assertFalse(failed.exception.help_dump_present)


if __name__ == "__main__":
    unittest.main()
