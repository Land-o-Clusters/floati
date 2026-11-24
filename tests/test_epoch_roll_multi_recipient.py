"""Catch the second recipient's wake-lock misclassification during epoch roll."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati.bus_epoch import epoch_guard, reconcile_epoch_roll, roll_bus_epoch
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import encode_frame
from floati.jsonl import _locked_path
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import wake_coordination_guard
from tests.temp_roots import REAL_TEMP_ROOT


class MultiRecipientEpochRollTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet-home", create=True)
        for node in ("architect", "seat-a", "seat-b"):
            Registry(self.root).register(node, "Codex")
        for node in ("seat-a", "seat-b"):
            message = EventLog(self.root).send(
                "architect", node, "floati", "a" * 40, "docs/evidence/ping.md",
                "historical message", idempotency_key="seed-" + node,
            )
            EventLog(self.root).present(node)
            SparseCursor(self.root).ack(node, [message["id"]], acting_session_id="history")
        AuthorityGrantStore(self.root).grant_exact(
            "bus-epoch-roll", "architect", 1, datetime.now(timezone.utc),
        )
        self.selected = {
            str(path.relative_to(self.root.path)): path.read_bytes()
            for path in self.root.path.rglob("*.jsonl")
            if path.name == "events.jsonl"
            or path.parts[-2] in {"deliveries", "acks"}
        }

    def assert_completed(self, receipt):
        self.assertEqual(encode_frame(receipt), (self.root.path / "events.jsonl").read_bytes())
        archive = Path(receipt["archive_path"])
        if not archive.is_absolute():
            archive = self.root.path / archive
        self.assertEqual(
            set(self.selected),
            {path.relative_to(archive).as_posix() for path in archive.rglob("*") if path.is_file()},
        )
        for relative, payload in self.selected.items():
            self.assertEqual(payload, (archive / relative).read_bytes(), relative)
            if relative != "events.jsonl":
                self.assertFalse((self.root.path / relative).exists(), relative)
        self.assertEqual([], list(self.root.path.glob(".floati-epoch-*")))
        for node in ("seat-a", "seat-b"):
            message = EventLog(self.root).send(
                "architect", node, "floati", "b" * 40, "docs/evidence/ping.md",
                "post-roll message", idempotency_key="post-" + node,
            )
            rows, _receipt = EventLog(self.root).present(node)
            self.assertEqual([message["id"]], [row["id"] for row in rows])
            SparseCursor(self.root).ack(node, [message["id"]], acting_session_id="post-roll")
            self.assertEqual([], EventLog(self.root).unacked_ids(node))

    def test_roll_with_two_registered_recipients_archives_and_keeps_messaging(self):
        try:
            result = roll_bus_epoch(self.root, actor="architect", idempotency_key="multi-roll")
        except ProtocolRefusal as exc:
            self.fail("multi-recipient roll refused: " + exc.code)
        self.assert_completed(result["receipt"])

    def test_committed_interruption_recovers_two_recipients_without_staging_debris(self):
        def interrupt(boundary):
            if boundary == "marker_committed_durable":
                raise RuntimeError("test-owned committed interruption")

        with self.assertRaisesRegex(RuntimeError, "test-owned committed interruption"):
            roll_bus_epoch(self.root, actor="architect", idempotency_key="interrupted", fault=interrupt)
        self.assertEqual(1, len(list(self.root.path.glob(".floati-epoch-roll-*"))))
        self.assertEqual(self.selected["events.jsonl"], (self.root.path / "events.jsonl").read_bytes())
        try:
            recovered = reconcile_epoch_roll(self.root)
        except ProtocolRefusal as exc:
            self.fail("multi-recipient committed recovery refused: " + exc.code)
        self.assertEqual("roll_forward", recovered["direction"])
        self.assert_completed(recovered["receipt"])

    def test_real_ledger_lock_still_refuses_later_wake_lock(self):
        with epoch_guard(self.root):
            with _locked_path(self.root.path / "events.jsonl.lock", exclusive=True,
                              relative="events.jsonl.lock"):
                with self.assertRaises(ProtocolRefusal) as caught:
                    with wake_coordination_guard(self.root, "seat-a"):
                        self.fail("reverse lock order was accepted")
        self.assertEqual("lock_order_invalid", caught.exception.code)

    def test_prepared_validation_failure_removes_only_new_roll_state(self):
        failure = IntegrityFailure("epoch_archive_invalid", "test-owned validation failure")
        with mock.patch("floati.bus_epoch.verify_epoch_archive", side_effect=failure):
            with self.assertRaises(IntegrityFailure) as caught:
                roll_bus_epoch(self.root, actor="architect", idempotency_key="prepared-failure")
        self.assertIs(failure, caught.exception)
        for relative, payload in self.selected.items():
            self.assertEqual(payload, (self.root.path / relative).read_bytes(), relative)
        self.assertEqual([], list(self.root.path.glob("archive-*")))
        self.assertEqual([], list(self.root.path.glob(".floati-epoch-*")))
        self.assertIsNone(reconcile_epoch_roll(self.root))
        result = roll_bus_epoch(self.root, actor="architect", idempotency_key="after-failure")
        self.assert_completed(result["receipt"])


if __name__ == "__main__":
    unittest.main()
