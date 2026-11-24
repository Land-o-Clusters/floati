"""Lineage readers must return the same archived history after compression."""
from __future__ import annotations

import tests  # Pin HOME/XDG before importing product code.

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from floati.bus_epoch import (
    _completed_roll_receipts, epoch_guard, roll_automatic_epoch,
    validate_epoch_receipt_archive,
)
from floati.cli import main
from floati.codex_wait import _WaiterInputsWatch
from floati.cursor_presentations import CursorPresentationLedger
from floati.epoch_carry import build_carry_plan
from floati.epoch_rotation import maintenance
from floati.events import EventLog
from floati.framing import decode_frames
from floati.message_consumption import MessageConsumption
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_execution import _history
from floati.seat_teardown_activity import _worker_pid_receipts
from floati.snapshot import _anchor_was_archived, _owned_epoch_archives
from floati.wake_daemon import WakeDaemon
from tests.temp_roots import REAL_TEMP_ROOT

REPO = Path(__file__).resolve().parents[1]


def _identity(rows):
    return tuple((row.get("kind"), row.get("id")) for row in rows)


class CompressedLineageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.messages = [self.send("one"), self.send("two")]
        self.roll()
        self.receipt = decode_frames((self.root.path / "events.jsonl").read_bytes())[0]
        self.archive = validate_epoch_receipt_archive(self.root, self.receipt)
        self.assertTrue(self.archive.is_dir())

    def send(self, key):
        return EventLog(self.root).send(
            "sender", "recipient", "floati", "a" * 40, "AGENTS.md",
            "compressed lineage " + key, idempotency_key=key,
        )

    def roll(self):
        def planner(root, old):
            carry = build_carry_plan(root, old)
            return carry.live_payloads, {
                "trigger": "send",
                "policy": {"max_bytes": 1, "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, _plane, raw in old if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None,
                "due_reasons": ["bytes"],
            }
        roll_automatic_epoch(self.root, actor="sender", planner=planner)

    def compress(self):
        epoch = decode_frames((self.root.path / "events.jsonl").read_bytes())[0]["epoch_id"]
        maintenance(self.root, actor="sender", trigger="send")
        self.assertTrue(self.archive.is_file())
        self.assertFalse(self.archive.is_dir())
        live = decode_frames((self.root.path / "events.jsonl").read_bytes())[0]
        self.assertEqual(epoch, live["epoch_id"])

    def assert_readers_after_compression(self, readers):
        before = {}
        for label, read, _ in readers:
            with self.subTest(reader=label, storage="directory"):
                before[label] = read()
        self.compress()
        for label, read, check in readers:
            with self.subTest(reader=label, storage="compressed"):
                after = read()
                if label in before:
                    self.assertEqual(before[label], after)
                if check is not None:
                    check(after)

    def test_snapshot_anchor_matches_the_directory_archive(self):
        data = (self.archive / "events.jsonl").read_bytes()
        anchor = {
            "path": "events.jsonl",
            "byte_offset": len(data),
            "record_ordinal": data.count(b"\n"),
            "prefix_sha256": hashlib.sha256(data).hexdigest(),
            "id_fingerprints": "",
        }

        def anchored():
            return _anchor_was_archived(anchor, _owned_epoch_archives(self.root))

        def sources():
            events, _ep, deliveries, _dp, acknowledgments, _ap = CursorPresentationLedger(
                self.root)._sources(self.archive, "recipient")
            return _identity(events), _identity(deliveries), _identity(acknowledgments)

        prefix = self.archive.relative_to(self.root.tenant_home)

        def frames():
            return _identity(MessageConsumption(self.root)._frames(prefix))

        def history():
            return _identity(_history(self.root))

        daemon = WakeDaemon.__new__(WakeDaemon)
        daemon.root = self.root

        def census():
            watch = _WaiterInputsWatch.__new__(_WaiterInputsWatch)
            watch.root = self.root
            watch.fixed = (self.root.tenant_home / "events.jsonl",)
            watch.workspace = self.base / "workspace"
            watch.stderr = io.StringIO()
            watch.census_signatures = {}
            watch.complete = False
            with epoch_guard(self.root):
                watch._refresh_inputs()
            owned = _owned_epoch_archives(self.root)
            return watch.complete, tuple(path in watch.paths for path in owned)

        self.assert_readers_after_compression((
            ("snapshot anchor", anchored, self.assertTrue),
            ("cursor presentation", sources, None),
            ("message consumption", frames, lambda rows: self.assertGreaterEqual(len(rows), 2)),
            ("seat execution history", history, None),
            ("wake predecessor", daemon._epoch_archive_token,
             lambda token: self.assertTrue(str(token).startswith("archive-"))),
            ("waiter census", census, lambda value: self.assertEqual((True, (True,)), value)),
        ))

        with self.subTest(archive_count=2):
            self.setUp()
            self.send("three")
            self.roll()
            self.assert_readers_after_compression((
                ("completed roll chain", self.completed_roll_chain, None),
                ("roll retry census", self.roll_retry_entries, None),
            ))

    def completed_roll_chain(self):
        receipts = list(_completed_roll_receipts(self.root))
        self.assertGreaterEqual(len(receipts), 2)
        return receipts

    def roll_retry_entries(self):
        from floati.archive_checkpoint import collect_roll_retry_sources

        with epoch_guard(self.root, exclusive=True):
            entries = collect_roll_retry_sources(self.root)["entries"]
        self.assertGreaterEqual(len(entries), 2)
        return entries

    def test_waiter_inbox_doctor_and_backup_succeed_over_the_compressed_epoch(self):
        self.compress()
        watch = _WaiterInputsWatch.__new__(_WaiterInputsWatch)
        watch.root = self.root
        watch.fixed = (self.root.tenant_home / "events.jsonl",)
        watch.workspace = self.base / "workspace"
        watch.stderr = io.StringIO()
        watch.census_signatures = {}
        watch.complete = False
        with epoch_guard(self.root):
            watch._refresh_inputs()
        self.assertTrue(watch.complete)
        self.assertIn(self.archive, watch.paths)

        code, output, diagnostic = self.invoke([
            "inbox", "--root", str(self.root.path), "--as", "recipient", "--peek",
        ])
        self.assertEqual(0, code, output + diagnostic)
        inbox = json.loads(output)
        self.assertEqual("ok", inbox["status"])
        self.assertEqual(
            [message["id"] for message in self.messages],
            [message["id"] for message in inbox["evidence"]["messages"]],
        )

        code, output, diagnostic = self.invoke([
            "doctor", "--root", str(self.root.path), "--source", str(REPO),
            "--no-sandbox", "--json",
        ])
        self.assertIn(code, (0, 35), (output + diagnostic)[-700:])
        doctor = json.loads(output)
        self.assertIn(doctor["status"], ("healthy", "degraded"))
        self.assertGreaterEqual(doctor["evidence"]["archive_retention"]["retained"], 1)

        destination = self.base / "compressed epoch backup"
        code, output, diagnostic = self.invoke([
            "backup", str(destination), "--root", str(self.root.path),
            "--epoch", self.receipt["epoch_id"],
        ])
        self.assertEqual(0, code, output + diagnostic)
        self.assertEqual("ok", json.loads(output)["status"])
        self.assertTrue((destination / "events.jsonl").is_file())

    def invoke(self, argv):
        output, diagnostic = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostic):
            try:
                code = main(argv)
            except SystemExit as failure:
                code = failure.code
        return code, output.getvalue(), diagnostic.getvalue()


class CompressedExecutionJoinTests(unittest.TestCase):
    def test_seat_teardown_pid_join_matches_the_directory_archive(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        root = FloatiRoot.open_direct_home(base / "fleet", create=True)
        workspace = base / "workspace"
        workspace.mkdir()
        registry = Registry(root)
        for node in ("builder", "architect"):
            registry.register(node, "Codex")
        from datetime import datetime as clock
        now = clock(2026, 10, 24, tzinfo=timezone.utc)
        from floati.planes import AuthorityGrantStore
        from floati.work import WorkLog
        from floati.ids import uuid7_hex
        from floati.workers import WorkerReceipts
        from floati.seat_execution import SeatExecutionController

        grant = AuthorityGrantStore(root).claim("work-claims", "builder", 60, 60, now)
        work = WorkLog(root)
        item = work.add(
            "implementation", "builder",
            [{"repo": "floati", "sha": "a" * 40, "doc": "docs/car.md"}],
            car_binding={"row": "BUS-ARCHIVE-RETENTION-1", "repository": "floati",
                         "branch": "row"},
            now=now,
        )
        work.claim(item["id"], "builder", "work-claims", grant["epoch"], now=now)
        controller = SeatExecutionController(root, recipient="architect")
        session = "worker-" + uuid7_hex()
        receipts = WorkerReceipts(root)
        receipts.append(session, item["id"], "builder", "codex", "claim", None, [], now=now)
        spawn = receipts.append(session, item["id"], "builder", "codex", "spawn", None, [], now=now)
        invocation = controller.bind_worker(spawn, workspace)
        detail = json.dumps({
            "adapter_pid": 4242,
            "alive_after_cleanup": [],
            "isolation_backend": None,
            "registered_process_groups": [4242],
        }, sort_keys=True)
        controller.callback(invocation, {
            "event": "ended", "code": "process_died", "detail": detail,
        })
        EventLog(root).present("architect")
        from floati.cursor import SparseCursor
        notice = next(row["id"] for row in EventLog(root).event_records()
                      if row["kind"] == "message_envelope")
        SparseCursor(root).ack("architect", [notice], acting_session_id="sess-1")

        def planner(current, old):
            carry = build_carry_plan(current, old)
            return carry.live_payloads, {
                "trigger": "send",
                "policy": {"max_bytes": 1, "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, _plane, raw in old if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None,
                "due_reasons": ["bytes"],
            }

        roll_automatic_epoch(root, actor="builder", planner=planner)
        receipt = decode_frames((root.path / "events.jsonl").read_bytes())[0]
        archive = validate_epoch_receipt_archive(root, receipt)
        self.assertTrue(archive.is_dir())
        before = _worker_pid_receipts(root, "builder")
        self.assertTrue(before)
        maintenance(root, actor="builder", trigger="send")
        self.assertTrue(archive.is_file())
        self.assertEqual(before, _worker_pid_receipts(root, "builder"))


if __name__ == "__main__":
    unittest.main()
