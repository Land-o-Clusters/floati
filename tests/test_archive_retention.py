"""Native archive-retention gaps on the train-26 car; scratch roots only."""
from __future__ import annotations

import tests  # Pin HOME/XDG before importing product code.

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from floati.bus_epoch import roll_automatic_epoch, validate_epoch_receipt_archive, verify_epoch_archive
from floati.cli import main
from floati.epoch_carry import build_carry_plan
from floati.epoch_rotation import maintenance
from floati.events import EventLog
from floati.errors import IntegrityFailure
from floati.framing import decode_frames
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class ArchiveRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.messages = [self.send("one"), self.send("two")]
        def planner(root, old):
            carry = build_carry_plan(root, old)
            return carry.live_payloads, {
                "trigger": "send", "policy": {"max_bytes": 1, "max_age_days": 7,
                    "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, _plane, raw in old if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None, "due_reasons": ["bytes"],
            }
        # Exercise the existing core transaction directly, before the separate
        # activity-boundary retention pass; preserve its exact legacy verifier.
        roll_automatic_epoch(self.root, actor="sender", planner=planner)
        self.receipt = decode_frames((self.root.path / "events.jsonl").read_bytes())[0]
        self.assertEqual(self.receipt["kind"], "bus_epoch_roll_receipt")
        self.archive = validate_epoch_receipt_archive(self.root, self.receipt)
        self.expected = {path.relative_to(self.archive).as_posix(): path.read_bytes()
                         for path in self.archive.rglob("*") if path.is_file()}
        self.assertTrue(self.expected, "the fixture must produce a real native archive")
        self.assertEqual(verify_epoch_archive(self.archive)["archive_sha256"],
                         self.receipt["archive_sha256"])

    def send(self, key):
        return EventLog(self.root).send("sender", "recipient", "floati", "a" * 40,
            "AGENTS.md", "scratch archive fixture " + key, idempotency_key=key)

    def assert_live_mail(self):
        self.assertEqual(EventLog(self.root).unacked_ids("recipient"),
                         [message["id"] for message in self.messages])

    def backup(self, destination):
        output, diagnostic = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostic):
            try:
                code = main(["backup", str(destination), "--root", str(self.root.path),
                             "--epoch", self.receipt["epoch_id"]])
            except SystemExit as failure:
                code = failure.code
        return code, output.getvalue(), diagnostic.getvalue()

    def test_backup_retained_epoch_exports_exact_selected_bytes(self):
        self.assert_live_mail()
        self.assertEqual(self.expected["events.jsonl"].count(b'"kind":"message_envelope"'), 2)
        destination = self.base / "exact epoch backup with spaces"
        code, output, diagnostic = self.backup(destination)
        self.assertEqual(code, 0,
            "retained epoch backup is unavailable: " + (output + diagnostic)[-700:])
        artifact = json.loads(output)
        self.assertEqual(artifact["status"], "ok")
        for relative, payload in self.expected.items():
            self.assertEqual((destination / relative).read_bytes(), payload)
        self.assert_live_mail()

    def test_relocated_root_retains_receipt_bound_archives_without_rewriting_history(self):
        from floati.archive_retention import _owned_archive

        original_events = (self.root.path / "events.jsonl").read_bytes()
        relocated = self.base / "restored" / self.root.path.name
        shutil.copytree(self.root.path, relocated)
        moved = FloatiRoot.open_direct_home(relocated)
        archive = moved.path / self.archive.name
        for representation in ("directory", "container"):
            with self.subTest(representation=representation):
                try:
                    maintenance(moved, actor="sender", trigger="send")
                    bound = validate_epoch_receipt_archive(moved, self.receipt)
                except IntegrityFailure as refused:
                    self.fail("relocated receipt-bearing archive must remain usable: " + refused.code)
                self.assertEqual(bound, archive)
                self.assertEqual(EventLog(moved).unacked_ids("recipient"),
                                 [message["id"] for message in self.messages])
                self.assertTrue(archive.is_file())
                self.assertEqual((moved.path / "events.jsonl").read_bytes(), original_events)
                self.assertEqual((self.root.path / "events.jsonl").read_bytes(), original_events)
        # The original path must not be used even while another copy exists.
        self.archive.rename(self.base / "offline-original-archive")
        from floati.archive_backup import export_epoch
        destination = self.base / "relocated-backup"
        try:
            status, _evidence, code = export_epoch(moved, self.receipt["epoch_id"], destination)
        except IntegrityFailure as refused:
            self.fail("backup must access only the selected root: " + refused.code)
        self.assertEqual((status, code), ("ok", 0))
        for relative, raw in self.expected.items():
            self.assertEqual((destination / relative).read_bytes(), raw)
        for invalid in (
                str(self.base / "other-fleet" / archive.name),
                str(self.root.path / "nested" / archive.name),
                str(self.root.path / ".." / self.root.path.name / archive.name),
                str(self.root.path / "archive-wrong-0000"),
                archive.name):
            receipt = dict(self.receipt, archive_path=invalid)
            for resolve, code in ((validate_epoch_receipt_archive, "epoch_receipt_archive_invalid"),
                                  (_owned_archive, "epoch_archive_retention_invalid")):
                with self.subTest(invalid=invalid, code=code):
                    with self.assertRaises(IntegrityFailure) as refused:
                        resolve(moved, receipt)
                    self.assertEqual(refused.exception.code, code)
        archive.unlink()
        archive.symlink_to(self.base / "offline-original-archive", target_is_directory=True)
        with self.assertRaises(IntegrityFailure) as refused:
            _owned_archive(moved, self.receipt)
        self.assertEqual(refused.exception.code, "epoch_archive_retention_invalid")

    def test_expiry_removes_payload_only_after_live_mail_remains_independent(self):
        rolled_at = datetime.fromisoformat(self.receipt["timestamp"].replace("Z", "+00:00"))
        future = rolled_at + timedelta(days=30, seconds=1)
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromisoformat(future.isoformat()).astimezone(tz)
        with mock.patch("floati.epoch_rotation.datetime", FutureClock):
            maintenance(self.root, actor="sender", trigger="send")
        self.assert_live_mail()
        self.assertFalse(self.archive.exists(),
            "native maintenance retained the default-expired epoch payload after 30 days")
        self.assertEqual(self.backup(self.base / "expired backup")[0], 32,
                         "removed epoch backup must return typed no_result")


if __name__ == "__main__":
    unittest.main()
