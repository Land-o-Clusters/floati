from __future__ import annotations

from tests.test_cli import LAUNCHER

import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.errors import DurabilityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.records import validate_record
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema

try:
    from floati.ledger_repair import LedgerRepair
except ModuleNotFoundError:
    LedgerRepair = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class GovernedLedgerRepairTests(unittest.TestCase):
    """G4 quarantine repair must preserve evidence while replacing one frame."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "repair-tenant"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.ledger = self.root.resolve_relative("events.jsonl")
        self.good_first_id = "msg-018f7e9b3c117abc8def0123456789ab"
        self.good_first = encode_frame(self._record(self.good_first_id, "first"))
        self.bad_id = "registry-018f7e9b3c127abc8def0123456789ac"
        self.bad_frame = encode_frame({
            "schema_version": 0,
            "id": self.bad_id,
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-29T12:00:00.000Z",
            "kind": "registry_entry",
            "node_id": "bad",
            "state": "active",
        })
        self.good_last = encode_frame(self._record(
            "msg-018f7e9b3c137abc8def0123456789ad", "last"
        ))
        self.original = self.good_first + self.bad_frame + self.good_last
        self.retained = self.good_first + self.good_last
        self.ledger.write_bytes(self.original)
        self.lock = self.ledger.with_name(self.ledger.name + ".lock")
        self.lock.touch()

    def _record(self, record_id: str, note: str) -> dict[str, object]:
        return {
            "schema_version": 0,
            "id": record_id,
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-29T12:00:00.000Z",
            "kind": "message_envelope",
            "sender": public_ids.builder("source"),
            "recipient": public_ids.worker("recipient"),
            "repo": "floati",
            "sha": "a" * 40,
            "doc": f"docs/evidence/{note}.md",
            "note": note,
            "idempotency_key": f"message-{note}",
        }

    def _crlf_boundary_claim(self) -> tuple[dict[str, object], bytes]:
        """Build one valid claim at the canonical 65,536-byte logical limit."""
        artifacts = [
            {
                "path": f"evidence/{index:03d}-" + "x" * 900,
                "sha256": f"{index:064x}",
            }
            for index in range(65)
        ]
        artifacts[-1]["path"] = "evidence/064-" + "x" * 982
        claim: dict[str, object] = {
            "schema_version": 0,
            "id": "delivery-claim-018f7e9b3c187abc8def0123456789ac",
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-29T12:00:00.000Z",
            "kind": "delivery_claim",
            "sha": "a" * 40,
            "repo_path": "\x2fprivate/tmp/floati",
            "bank": "discover",
            "declared": {"ran": 1, "result": "OK"},
            "artifacts": artifacts,
            "note_ref": self.good_first_id,
            "deadline_seconds": 60,
        }
        lf_frame = encode_frame(claim)
        self.assertEqual(65_536, len(lf_frame))
        self.assertEqual(65_535, len(lf_frame.splitlines()[0]))
        crlf_frame = lf_frame[:-1] + b"\r\n"
        self.assertEqual(65_537, len(crlf_frame))
        return claim, crlf_frame

    def _snapshot(self) -> tuple[tuple[object, ...], ...]:
        """Capture every selected-tenant object, including repair side state."""
        entries: list[tuple[object, ...]] = []
        for path in sorted(self.root.tenant_home.rglob("*")):
            stat = path.lstat()
            relative = path.relative_to(self.root.tenant_home).as_posix()
            if path.is_symlink():
                payload = os.readlink(path)
                kind = "symlink"
            elif path.is_dir():
                payload = None
                kind = "directory"
            else:
                payload = path.read_bytes()
                kind = "file"
            entries.append((
                relative,
                kind,
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                payload,
            ))
        return tuple(entries)

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int, int, bytes]:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, path.read_bytes()

    def _repair(self):
        self.assertIsNotNone(
            LedgerRepair,
            "floati.ledger_repair.LedgerRepair must implement G4 governed quarantine repair",
        )
        return LedgerRepair(self.root)

    def test_quarantine_preserves_exact_frame_and_receipts_atomic_replace(self) -> None:
        """Catches G4 replacing bytes in place, losing the invalid frame, or misreporting its receipt."""
        before = self.ledger.stat()
        repair = self._repair()

        result = repair.quarantine("events.jsonl", self.bad_id, key="repair-1")

        quarantine = Path(result["quarantine_path"])
        after = self.ledger.stat()
        self.assertEqual(self.bad_frame, quarantine.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(quarantine.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(quarantine.parent.stat().st_mode))
        self.assertEqual(self.retained, self.ledger.read_bytes()[:len(self.retained)])
        self.assertNotEqual(
            (before.st_dev, before.st_ino),
            (after.st_dev, after.st_ino),
            "atomic repair must replace, rather than rewrite, the ledger inode",
        )
        self.assertEqual(
            ["tail_followers", "waiters", "monitors"],
            result["invalidated_followers"],
        )
        self.assertEqual("events.jsonl", result["ledger"])
        self.assertEqual(self.bad_id, result["record_id"])
        self.assertEqual("repair-1", result["idempotency_key"])
        self.assertEqual(hashlib.sha256(self.original).hexdigest(), result["original_digest"])
        self.assertEqual(hashlib.sha256(self.retained).hexdigest(), result["repaired_digest"])
        self.assertEqual(hashlib.sha256(self.bad_frame).hexdigest(), result["quarantine_digest"])
        self.assertTrue(quarantine.is_relative_to(self.root.tenant_home))
        self.assertEqual(
            {
                "before": {"device": before.st_dev, "inode": before.st_ino},
                "after": {"device": after.st_dev, "inode": after.st_ino},
                "changed": True,
            },
            result["replaced_inode"],
        )
        receipt = decode_frames(self.ledger.read_bytes())[-1]
        frames = decode_frames(self.ledger.read_bytes())
        self.assertEqual(decode_frames(self.retained), frames[:-1])
        self.assertEqual(3, len(frames))
        self.assertEqual(1, receipt["schema_version"])
        self.assertEqual("ledger_repair_receipt", receipt["kind"])
        self.assertRegex(
            receipt["id"],
            r"^ledger-repair-receipt-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$",
        )
        self.assertEqual(self.root.tenant_id, receipt["tenant_id"])
        self.assertRegex(
            receipt["timestamp"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$",
        )
        self.assertEqual(
            receipt,
            validate_record(
                receipt,
                self.root.tenant_id,
                frozenset({"ledger_repair_receipt"}),
                integrity=True,
            ),
        )
        validate_json_schema(
            receipt,
            Path("schemas/v1/ledger-repair-receipt.schema.json"),
        )
        self.assertEqual(result, receipt)

    def test_quarantine_refuses_nonselected_ledger_coordinates_before_mutation(self) -> None:
        """Catches G4 accepting an absolute, traversing, or alternate ledger coordinate."""
        before = self._snapshot()
        repair = self._repair()
        for ledger in (str(self.ledger), "../events.jsonl", "other.jsonl"):
            with self.subTest(ledger=ledger):
                with self.assertRaises(ProtocolRefusal):
                    repair.quarantine(ledger, self.bad_id, key="repair-1")
                self.assertEqual(before, self._snapshot())

    def test_quarantine_refuses_missing_or_duplicate_target_without_mutation(self) -> None:
        """Catches G4 removing no frame or an ambiguous set of frames for one record id."""
        missing = "registry-018f7e9b3c147abc8def0123456789ae"
        repair = self._repair()
        before_valid_target = self._snapshot()
        with self.assertRaises(ProtocolRefusal):
            repair.quarantine(
                "events.jsonl", self.good_first_id, key="repair-wrong-target"
            )
        self.assertEqual(before_valid_target, self._snapshot())

        before_missing = self._snapshot()
        with self.assertRaises(ProtocolRefusal):
            repair.quarantine("events.jsonl", missing, key="repair-missing")
        self.assertEqual(before_missing, self._snapshot())

        self.ledger.write_bytes(self.original + self.bad_frame)
        before_duplicate = self._snapshot()
        with self.assertRaises(ProtocolRefusal):
            repair.quarantine("events.jsonl", self.bad_id, key="repair-duplicate")
        self.assertEqual(before_duplicate, self._snapshot())

    def test_quarantine_refuses_when_a_retained_frame_exceeds_the_reader_limit(self) -> None:
        """Catches repair producing a ledger canonical readers reject by frame size."""
        before = self._snapshot()

        with mock.patch(
            "floati.jsonl.MAX_RECORD_BYTES", len(self.good_first) - 1
        ):
            with self.assertRaises(ProtocolRefusal):
                self._repair().quarantine(
                    "events.jsonl", self.bad_id, key="repair-oversized-retained"
                )

        self.assertEqual(before, self._snapshot())

    def test_canonically_valid_selected_crlf_boundary_frame_cannot_authorize_repair(self) -> None:
        """Catches physical CRLF width being mistaken for canonical record size."""
        claim, boundary_frame = self._crlf_boundary_claim()
        self.ledger.write_bytes(self.good_first + boundary_frame + self.good_last)
        events = EventLog(self.root, Registry(self.root))
        self.assertEqual(
            [self.good_first_id, claim["id"], "msg-018f7e9b3c137abc8def0123456789ad"],
            [record["id"] for record in events.event_records()],
        )
        before = self._snapshot()

        with self.assertRaises(ProtocolRefusal):
            self._repair().quarantine(
                "events.jsonl", str(claim["id"]), key="repair-valid-crlf-boundary"
            )

        self.assertEqual(before, self._snapshot())

    def test_canonically_valid_retained_crlf_boundary_frame_remains_repairable(self) -> None:
        """Catches repair refusing a final ledger canonical readers accept."""
        claim, boundary_frame = self._crlf_boundary_claim()
        retained = self.good_first + boundary_frame + self.good_last
        self.ledger.write_bytes(
            self.good_first + boundary_frame + self.bad_frame + self.good_last
        )

        receipt = self._repair().quarantine(
            "events.jsonl", self.bad_id, key="repair-retained-crlf-boundary"
        )

        self.assertEqual(retained, self.ledger.read_bytes()[:len(retained)])
        self.assertEqual(
            [
                self.good_first_id,
                claim["id"],
                "msg-018f7e9b3c137abc8def0123456789ad",
                receipt["id"],
            ],
            [
                record["id"]
                for record in EventLog(self.root, Registry(self.root)).event_records()
            ],
        )

    def test_quarantine_refuses_when_final_ledger_exceeds_the_record_limit(self) -> None:
        """Catches repair omitting its appended receipt from the canonical count bound."""
        before = self._snapshot()

        with mock.patch("floati.jsonl.MAX_LEDGER_RECORDS", 2):
            with self.assertRaises(ProtocolRefusal):
                self._repair().quarantine(
                    "events.jsonl", self.bad_id, key="repair-record-limit"
                )

        self.assertEqual(before, self._snapshot())

    def test_quarantine_waits_for_the_established_events_lock_before_replacing(self) -> None:
        """Catches G4 bypassing the real events.jsonl.lock during a quarantine replace."""
        repair = self._repair()
        before = self._snapshot()
        started = threading.Event()
        finished = threading.Event()
        completed: list[dict[str, object]] = []
        failures: list[BaseException] = []

        def quarantine_while_contended() -> None:
            started.set()
            try:
                completed.append(repair.quarantine("events.jsonl", self.bad_id, key="repair-lock-1"))
            except Exception as exc:  # Surface implementation failures in the test thread.
                failures.append(exc)
            finally:
                finished.set()

        with self.lock.open("a+b") as holder:
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
            worker = threading.Thread(target=quarantine_while_contended)
            worker.start()
            self.assertTrue(started.wait(1), "repair thread did not start")
            self.assertFalse(
                finished.wait(0.15),
                "repair must not finish while another holder owns events.jsonl.lock",
            )
            self.assertEqual(before, self._snapshot())
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

        worker.join(2)
        self.assertFalse(
            worker.is_alive(), "repair did not proceed after the lock was released"
        )
        if failures:
            raise failures[0]
        self.assertEqual(1, len(completed))
        self.assertEqual(self.bad_id, completed[0]["record_id"])

    def test_quarantine_collision_never_unlinks_preexisting_foreign_evidence(self) -> None:
        """Catches failed O_EXCL quarantine creation deleting a foreign path."""
        collision_id = "018f7e9b3c147abc8def0123456789ae"
        foreign = self.root.resolve_relative(
            Path("quarantine") / "ledger-repair"
            / f"ledger-repair-receipt-{collision_id}.jsonl"
        )
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"foreign-quarantine\n")
        before = self._file_identity(foreign)

        with mock.patch("floati.ledger_repair.uuid7_hex", return_value=collision_id):
            with self.assertRaises(DurabilityFailure):
                self._repair().quarantine(
                    "events.jsonl", self.bad_id, key="repair-quarantine-collision"
                )

        self.assertTrue(foreign.is_file())
        self.assertEqual(before, self._file_identity(foreign))
        self.assertEqual(self.original, self.ledger.read_bytes())

    def test_staging_collision_never_unlinks_preexisting_foreign_file(self) -> None:
        """Catches failed O_EXCL staging creation deleting a foreign path."""
        receipt_id = "018f7e9b3c147abc8def0123456789ae"
        collision_id = "018f7e9b3c157abc8def0123456789af"
        foreign = self.ledger.with_name(f".events.jsonl.repair-{collision_id}")
        foreign.write_bytes(b"foreign-staging\n")
        before = self._file_identity(foreign)

        with mock.patch(
            "floati.ledger_repair.uuid7_hex",
            side_effect=(receipt_id, collision_id),
        ):
            with self.assertRaises(DurabilityFailure):
                self._repair().quarantine(
                    "events.jsonl", self.bad_id, key="repair-staging-collision"
                )

        self.assertTrue(foreign.is_file())
        self.assertEqual(before, self._file_identity(foreign))
        self.assertEqual(self.original, self.ledger.read_bytes())
        quarantine = self.root.resolve_relative(
            Path("quarantine") / "ledger-repair"
            / f"ledger-repair-receipt-{receipt_id}.jsonl"
        )
        self.assertFalse(quarantine.exists())

    def test_quarantine_exact_retry_reuses_receipt_and_conflict_never_mutates(self) -> None:
        """Catches G4 duplicating a repair or accepting an idempotency key for another request."""
        repair = self._repair()
        first = repair.quarantine("events.jsonl", self.bad_id, key="repair-1")
        after_first = self._snapshot()
        quarantine = Path(first["quarantine_path"])
        quarantine_identity = self._file_identity(quarantine)

        self.assertEqual(first, repair.quarantine("events.jsonl", self.bad_id, key="repair-1"))
        self.assertEqual(after_first, self._snapshot())
        self.assertEqual(quarantine_identity, self._file_identity(quarantine))
        with self.assertRaises(ProtocolRefusal):
            repair.quarantine(
                "events.jsonl",
                self.good_first_id,
                key="repair-1",
        )
        self.assertEqual(after_first, self._snapshot())
        self.assertEqual(quarantine_identity, self._file_identity(quarantine))

    def test_cli_repair_quarantine_emits_one_compact_root_bound_receipt(self) -> None:
        """Catches the missing G4 public repair quarantine grammar or an unbound artifact."""
        result = subprocess.run(
            [
                str(LAUNCHER), "repair", "quarantine",
                "--root", str(self.home),
                "--ledger", "events.jsonl",
                "--record-id", self.bad_id,
                "--idempotency-key", "repair-cli-1",
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        artifact = json.loads(result.stdout)
        self.assertEqual(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            result.stdout,
        )
        self.assertEqual(0, artifact["artifact_version"])
        self.assertEqual("repair", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(str(self.home.resolve()), artifact["evidence"]["root"])
        self.assertEqual(self.root.tenant_id, artifact["evidence"]["tenant_id"])
        receipt = artifact["evidence"]["receipt"]
        self.assertEqual("ledger_repair_receipt", receipt["kind"])
        self.assertEqual(receipt, decode_frames(self.ledger.read_bytes())[-1])
        self.assertEqual(
            decode_frames(self.retained), decode_frames(self.ledger.read_bytes())[:-1]
        )
        quarantine = Path(receipt["quarantine_path"])
        self.assertTrue(quarantine.is_relative_to(self.root.tenant_home))
        self.assertEqual(self.bad_frame, quarantine.read_bytes())
        self.assertEqual(hashlib.sha256(self.bad_frame).hexdigest(), receipt["quarantine_digest"])

    def test_event_readers_send_and_retract_after_repair_receipt(self) -> None:
        """Catches strict event consumers treating inert repair testimony as a message."""
        self.ledger.write_bytes(b"")
        registry = Registry(self.root)
        for node in (public_ids.builder("source"), public_ids.worker("recipient")):
            registry.register(node, "worker")
        events = EventLog(self.root, registry)
        session = "worker-018f7e9b3c137abc8def0123456789ab"
        first = events.send(
            public_ids.builder("source"), public_ids.worker("recipient"), "floati", "a" * 40,
            "docs/evidence/first.md", "first",
            idempotency_key="first", worker_session_id=session,
        )
        second = events.send(
            public_ids.builder("source"), public_ids.worker("recipient"), "floati", "b" * 40,
            "docs/evidence/second.md", "second",
            idempotency_key="second", worker_session_id=session,
        )
        with self.ledger.open("ab") as handle:
            handle.write(self.bad_frame)

        receipt = self._repair().quarantine(
            "events.jsonl", self.bad_id, key="repair-reader-1"
        )
        third = events.send(
            public_ids.builder("source"), public_ids.worker("recipient"), "floati", "c" * 40,
            "docs/evidence/third.md", "third",
            idempotency_key="third", worker_session_id=session,
        )
        retraction = events.retract(
            second["id"], worker_session_id=session,
            reason="sent_in_error", author=public_ids.builder("source"),
        )

        self.assertEqual("ledger_repair_receipt", receipt["kind"])
        self.assertEqual("message_retracted", retraction["kind"])
        self.assertEqual(
            [first["id"], second["id"], third["id"]],
            [record["id"] for record in events.records()],
        )

    def test_send_after_repair_may_reuse_the_repairs_idempotency_key(self) -> None:
        """Catches repair testimony poisoning the message idempotency namespace."""
        shared_key = "shared-send-repair-key"
        receipt = self._repair().quarantine(
            "events.jsonl", self.bad_id, key=shared_key
        )
        registry = Registry(self.root)
        for node in (public_ids.builder("source"), public_ids.worker("recipient")):
            registry.register(node, "worker")
        events = EventLog(self.root, registry)

        message = events.send(
            public_ids.builder("source"), public_ids.worker("recipient"), "floati", "d" * 40,
            "docs/evidence/shared-after-repair.md", "shared after repair",
            idempotency_key=shared_key,
            worker_session_id="worker-018f7e9b3c167abc8def0123456789aa",
        )

        self.assertEqual("ledger_repair_receipt", receipt["kind"])
        self.assertEqual("message_envelope", message["kind"])
        self.assertEqual(shared_key, message["idempotency_key"])

    def test_repair_after_send_may_reuse_the_messages_idempotency_key(self) -> None:
        """Catches message testimony poisoning the repair idempotency namespace."""
        shared_key = "shared-repair-send-key"
        self.ledger.write_bytes(b"")
        registry = Registry(self.root)
        for node in (public_ids.builder("source"), public_ids.worker("recipient")):
            registry.register(node, "worker")
        events = EventLog(self.root, registry)
        message = events.send(
            public_ids.builder("source"), public_ids.worker("recipient"), "floati", "e" * 40,
            "docs/evidence/shared-before-repair.md", "shared before repair",
            idempotency_key=shared_key,
            worker_session_id="worker-018f7e9b3c177abc8def0123456789ab",
        )
        with self.ledger.open("ab") as handle:
            handle.write(self.bad_frame)

        receipt = self._repair().quarantine(
            "events.jsonl", self.bad_id, key=shared_key
        )

        self.assertEqual("message_envelope", message["kind"])
        self.assertEqual("ledger_repair_receipt", receipt["kind"])
        self.assertEqual(shared_key, receipt["idempotency_key"])


if __name__ == "__main__":
    unittest.main()
