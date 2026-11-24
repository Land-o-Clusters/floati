"""Confinement and bypass-writer controls for payload-free wake hints."""
from __future__ import annotations

import json
import os
import stat
import unittest

from floati.bus_epoch import epoch_guard, roll_bus_epoch
from floati.codex_wait_contract import (
    CodexWaitConsentLedger, CodexWaitSessionLedger, write_workspace_map,
)
from floati.cursor_presentations import CursorPresentationLedger
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.wake_control import WakeController
from floati.wake_hints import publish_recipient_hint
from floati.wake_hold import wake_coordination_guard
from tests import test_codex_wait as baseline


OLD_NS = 1000000000


class WakeHintConfinementTests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.node = self.f.participant.binding.node_id
        self.hint = self.f.root.tenant_home / "state" / "wake-hints" / self.node / "generation"
        self.root_hint = self.f.root.tenant_home / "state" / "wake-hints" / "root-generation"
        self.outside = self.f.base / "outside-root"
        self.outside.mkdir()
        self.foreign = self.outside / "foreign"
        self.foreign.write_bytes(b"foreign-bytes-must-remain")

    def _foreign_identity(self):
        info = self.foreign.lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                self.foreign.read_bytes())

    def _assert_message_committed(self, message):
        rows = [row for row in EventLog(self.f.root).event_records()
                if row.get("kind") == "message_envelope"]
        self.assertIn(message["id"], [row["id"] for row in rows])

    def test_successful_hint_stays_inside_the_root_and_carries_no_payload(self):
        message = self.f.send("hint-inside")
        self.assertTrue(self.hint.is_relative_to(self.f.root.tenant_home))
        info = self.hint.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(1, info.st_nlink)
        self.assertEqual(os.geteuid(), info.st_uid)
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        self.assertEqual(b"", self.hint.read_bytes())
        self.assertNotIn(message["id"].encode(), self.hint.read_bytes())
        self.assertEqual([self.foreign], list(self.outside.iterdir()))
        # Independent old mtimes avoid clock granularity assumptions.
        peer = self.hint.parent.parent / "unrelated-seat" / "generation"
        peer.parent.mkdir(parents=True)
        peer.write_bytes(b"")
        os.utime(peer, ns=(OLD_NS, OLD_NS))
        os.utime(self.hint, ns=(OLD_NS, OLD_NS))
        self.f.send("hint-next")
        after = self.hint.lstat()
        self.assertNotEqual(OLD_NS, after.st_mtime_ns)
        self.assertTrue(stat.S_ISREG(after.st_mode))
        self.assertEqual(1, after.st_nlink)
        self.assertEqual(os.geteuid(), after.st_uid)
        self.assertEqual(0o600, stat.S_IMODE(after.st_mode))
        self.assertEqual(b"", self.hint.read_bytes())
        self.assertEqual(OLD_NS, peer.stat().st_mtime_ns,
                         "send woke an unrelated recipient")

    def test_symlink_and_hardlink_hints_do_not_follow_to_a_foreign_target(self):
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                if self.hint.exists() or self.hint.is_symlink():
                    self.hint.unlink()
                self.hint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if kind == "symlink":
                    self.hint.symlink_to(self.foreign)
                else:
                    os.link(self.foreign, self.hint)
                before = self._foreign_identity()
                message = self.f.send("hint-" + kind)
                self.assertEqual(before, self._foreign_identity())
                self.assertTrue(self.hint.is_symlink() if kind == "symlink" else self.hint.exists())
                self._assert_message_committed(message)

    def test_symlinked_hint_parent_cannot_create_a_file_outside_the_root(self):
        parent = self.hint.parent
        if parent.exists() or parent.is_symlink():
            if parent.is_symlink() or parent.is_file():
                parent.unlink()
            else:
                for child in parent.iterdir():
                    child.unlink()
                parent.rmdir()
        parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent.symlink_to(self.outside)
        before = self._foreign_identity()
        outside_before = sorted(path.name for path in self.outside.iterdir())
        message = self.f.send("hint-parent-symlink")
        self.assertEqual(before, self._foreign_identity())
        self.assertEqual(outside_before, sorted(path.name for path in self.outside.iterdir()))
        self._assert_message_committed(message)

    def test_failed_hint_publish_returns_false_without_raising(self):
        self.hint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.hint.exists() or self.hint.is_symlink():
            self.hint.unlink()
        self.hint.symlink_to(self.foreign)
        before = self._foreign_identity()
        self.assertIs(False, publish_recipient_hint(self.f.root, self.node))
        self.assertEqual(before, self._foreign_identity())

    def test_direct_publish_rejects_a_node_id_that_could_escape_the_root(self):
        for node in ("../outside", "state/other", "\x2ftmp/elsewhere", ""):
            with self.subTest(node=node):
                self.assertIs(False, publish_recipient_hint(self.f.root, node))
        self.assertEqual([self.foreign], list(self.outside.iterdir()))


class WakeHintBypassWriterTests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.node = self.f.participant.binding.node_id
        self.hint = self.f.root.tenant_home / "state" / "wake-hints" / self.node / "generation"
        self.root_hint = self.f.root.tenant_home / "state" / "wake-hints" / "root-generation"

    def _freeze(self, path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600)
        os.utime(path, ns=(OLD_NS, OLD_NS))
        return path.stat().st_mtime_ns

    def test_pause_and_resume_publish_the_recipient_hint(self):
        session = "hint-pause-session"
        self.f.arm_session(session)
        frozen = self._freeze(self.hint)
        paused = WakeController(self.f.root).pause(
            self.node, session, idempotency_key="hint-pause")
        self.assertNotEqual(frozen, self.hint.stat().st_mtime_ns,
                            "pause left the recipient hint unchanged")
        self.assertEqual(b"", self.hint.read_bytes())
        frozen = self._freeze(self.hint)
        WakeController(self.f.root).resume(
            self.node, session, idempotency_key="hint-resume",
            expected_pause_receipt_id=paused["receipt"]["id"])
        self.assertNotEqual(frozen, self.hint.stat().st_mtime_ns,
                            "resume left the recipient hint unchanged")

    def test_workspace_map_replacement_publishes_the_root_hint(self):
        extra = self.f.base / "second-workspace"
        extra.mkdir()
        frozen = self._freeze(self.root_hint)
        write_workspace_map(self.f.root.path, extra, self.node)
        self.assertTrue(self.root_hint.is_relative_to(self.f.root.tenant_home))
        self.assertNotEqual(frozen, self.root_hint.stat().st_mtime_ns,
                            "workspace map replacement left the root hint unchanged")
        self.assertEqual(b"", self.root_hint.read_bytes())

    def test_cursor_presentation_prepare_publishes_the_recipient_hint(self):
        session = "hint-present-session"
        self.f.arm_session(session)
        self.f.send("hint-present")
        frozen = self._freeze(self.hint)
        plane = CursorPresentationLedger(self.f.root)
        with epoch_guard(self.f.root, exclusive=False), wake_coordination_guard(self.f.root, self.node):
            binding = self.f.participant.binding
            consent = CodexWaitConsentLedger(self.f.root).require_armed(binding)
            classification = CodexWaitSessionLedger(self.f.root)._participate_already_guarded(
                binding, consent, session, classified=True)
            selection = plane.select(binding, consent, classification, harness="cursor")
            plane.prepare(selection, {"followup_message": "native presentation hint"})
        self.assertNotEqual(frozen, self.hint.stat().st_mtime_ns,
                            "presentation left the recipient hint unchanged")
        self.assertEqual(b"", self.hint.read_bytes())

    def test_epoch_roll_publishes_the_root_hint(self):
        self.f.send("hint-epoch-seed")
        frozen = self._freeze(self.root_hint)
        policy = self.f.root.tenant_home / "state" / "ledger-policy.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({"schema_version": 0, "max_bytes": 1, "max_age_days": 7}))
        self.f.send("hint-epoch-roll")
        self.assertTrue(any(self.f.root.tenant_home.glob("archive-*")))
        self.assertNotEqual(frozen, self.root_hint.stat().st_mtime_ns,
                            "epoch roll left the root hint unchanged")
        self.assertEqual(b"", self.root_hint.read_bytes())
        with self.assertRaises(ProtocolRefusal):
            roll_bus_epoch(self.f.root, actor=self.node, idempotency_key="hint-manual-denied")
