"""The optional v2 roster preserves evidence ages and the legacy contract."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati.confluence import ConfluenceGrantLedger, materialize_bundle
from floati.presence import PresenceService
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.errors import FloatiError
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/v0/receipts-read-bundle-v2.schema.json"
FIXTURES = ROOT / "tests/fixtures/confluence/roster"
CELLS = {"owner", "harness", "repository_branch", "activity", "current_action", "liveness"}


class ConfluenceRosterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register("builder-a", "Codex")
        Registry(self.root).register("builder-b", "Claude")
        Registry(self.root).register("retired-builder", "Codex")
        Registry(self.root).retire("retired-builder")
        ConfluenceGrantLedger(self.root).grant("puddle", "roster-grant")

    def bundle(self, version=2):
        output = self.base / "bundle.json"
        materialize_bundle(self.root, consumer="puddle", out=output, version=version)
        return json.loads(output.read_text(encoding="utf-8"))

    def test_bundle_exports_active_roster_cells_with_stamps_and_ages(self):
        document = self.bundle()
        rows = document["roster"]
        self.assertEqual(["builder-a", "builder-b"], [row["node_id"] for row in rows])
        for row in rows:
            self.assertEqual(CELLS, set(row) - {"node_id"})
            for name in CELLS:
                cell = row[name]
                self.assertIn(cell["stamp"], {"MEASURED", "DERIVED", "ESTIMATE", "UNKNOWN"})
                self.assertIn("observed_at", cell)
                self.assertIn("age_seconds", cell)
            self.assertEqual("MEASURED", row["harness"]["stamp"])
            self.assertGreaterEqual(row["harness"]["age_seconds"], 0)
            self.assertEqual("UNKNOWN", row["owner"]["stamp"])
            self.assertIsNone(row["owner"]["age_seconds"])
        validate_json_schema(document, SCHEMA)

    def test_expired_presence_keeps_named_time_and_never_infers_idle(self):
        reported = datetime.now(timezone.utc) - timedelta(seconds=300)
        PresenceService(self.root).report_self("builder-a", ttl_seconds=60, now=reported)
        document = self.bundle()
        row = document["roster"][0]
        self.assertEqual("no_report_since", row["liveness"]["value"])
        self.assertEqual("MEASURED", row["liveness"]["stamp"])
        self.assertGreaterEqual(row["liveness"]["age_seconds"], 300)
        expires = datetime.fromisoformat(row["liveness"]["named_time"].replace("Z", "+00:00"))
        self.assertLess(abs((expires - reported).total_seconds() - 60), 0.01)
        self.assertEqual("UNKNOWN", row["activity"]["stamp"])
        self.assertIsNone(row["activity"]["value"])
        self.assertEqual("UNKNOWN", document["roster"][1]["liveness"]["stamp"])
        validate_json_schema(document, SCHEMA)

    def test_legacy_fixture_still_validates_and_old_fields_are_byte_identical(self):
        legacy = json.loads((FIXTURES / "legacy-v2.json").read_text(encoding="utf-8"))
        self.assertNotIn("roster", legacy)
        validate_json_schema(legacy, SCHEMA)
        # Export exactly this fixed durable input through the current producer.
        self.root = FloatiRoot.open_direct_home(self.base / "legacy-fleet", create=True)
        grant = ConfluenceGrantLedger(self.root).grant("puddle", "legacy-grant")
        legacy.update(tenant_id=self.root.tenant_id, grant_id=grant["id"], snapshot_at=grant["timestamp"])
        expected_bytes = (json.dumps(legacy, indent=2) + "\n").encode()
        current = self.bundle()
        self.assertEqual([], current.pop("roster"))
        current.pop("fleet_state")
        self.assertEqual(expected_bytes, (json.dumps(current, indent=2) + "\n").encode())

    def test_schema_change_only_adds_an_optional_roster(self):
        previous = json.loads((FIXTURES / "schema-before.json").read_text(encoding="utf-8"))
        current = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertNotIn("roster", current["required"])
        current["properties"].pop("roster")
        current["properties"].pop("fleet_state")
        current.pop("$defs")
        self.assertEqual(previous, current)

    def test_schema_accepts_all_stamps_and_rejects_missing_age(self):
        document = self.bundle()
        for stamp in ("MEASURED", "DERIVED", "ESTIMATE", "UNKNOWN"):
            with self.subTest(stamp=stamp):
                document["roster"][0]["harness"]["stamp"] = stamp
                validate_json_schema(document, SCHEMA)
        for field in ("stamp", "observed_at", "age_seconds"):
            with self.subTest(missing=field):
                invalid = copy.deepcopy(document)
                del invalid["roster"][0]["harness"][field]
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(invalid, SCHEMA)

    def test_fleet_state_names_absent_waiter_evidence_without_inventing_cpu(self):
        document = self.bundle()
        self.assertIn("fleet_state", document)
        state = document["fleet_state"]
        self.assertEqual([], state["archive_retention"]["epochs"])
        self.assertFalse(state["archive_retention"]["keep_forever"])
        self.assertIsNone(state["archive_retention"]["last_removal_receipt"])
        self.assertEqual(["builder-a", "builder-b"],
                         [row["node_id"] for row in state["waiters"]])
        for row in state["waiters"]:
            for field, reason in (("mode", "waiter_observation_absent"),
                                  ("last_wake", "wake_receipt_absent"),
                                  ("idle_cpu", "idle_cpu_not_measured")):
                self.assertEqual("UNKNOWN", row[field]["stamp"])
                self.assertIsNone(row[field]["value"])
                self.assertEqual(reason, row[field]["reason"])
        validate_json_schema(document, SCHEMA)
        self.assertNotIn("fleet_state", self.bundle(version=1))

    def test_optional_fleet_state_preserves_the_entire_previous_v2_schema(self):
        previous = json.loads((FIXTURES / "schema-before-fleet-state.json").read_text())
        current = json.loads(SCHEMA.read_text())
        self.assertNotIn("fleet_state", current["required"])
        current["properties"].pop("fleet_state")
        current["$defs"].pop("fleet_cell")
        self.assertEqual(previous, current)
        examples = json.loads((FIXTURES / "fleet-state-compatibility.json").read_text())
        for document in examples["schemas/v0/receipts-read-bundle-v2.schema.json"]:
            validate_json_schema(document, FIXTURES / "schema-before-fleet-state.json")
            validate_json_schema(document, SCHEMA)

    def test_last_wake_ignores_queued_and_refused_attempts(self):
        from floati.jsonl import append_record
        for ordinal, (outcome, stamp) in enumerate((
                ("woke", "2026-10-01T12:00:00Z"),
                ("queued", "2026-10-02T12:00:00Z"),
                ("refused", "2026-10-03T12:00:00Z"))):
            row = {"schema_version": 1, "kind": "wake_attempt_receipt",
                   "id": "wake-attempt-018f7e9b3c117abc8def0123456789a" + str(ordinal),
                   "tenant_id": self.root.tenant_id, "timestamp": stamp,
                   "node_id": "builder-a", "acting_session_id": "observed-session",
                   "message_worker_session_id": None, "idempotency_key": "attempt-" + str(ordinal),
                   "item_ids": ["msg-018f7e9b3c117abc8def0123456789ab"],
                   "decision_receipt_id": "wake-hold-018f7e9b3c117abc8def0123456789ab",
                   "outcome": outcome, "reason_code": "wake_decision_missing" if outcome == "refused" else None}
            append_record(self.root, "receipts/wakes/builder-a.jsonl", row,
                          allowed_kinds={"wake_attempt_receipt"})
        document = self.bundle()
        cell = document["fleet_state"]["waiters"][0]["last_wake"]
        self.assertEqual("MEASURED", cell["stamp"])
        self.assertEqual("2026-10-01T12:00:00Z", cell["value"])
        self.assertEqual("wake-attempt-018f7e9b3c117abc8def0123456789a0", cell["receipt_id"])
        validate_json_schema(document, SCHEMA)

    def test_bundle_preserves_the_observed_waiter_backend_stamp_and_expiry(self):
        from floati.waiter_state import write_waiter_observation

        now = datetime.now(timezone.utc)
        self.assertTrue(write_waiter_observation(self.root, "builder-a", "blocking",
                        now + timedelta(seconds=120), observed_at=now))
        document = self.bundle()
        cell = document["fleet_state"]["waiters"][0]["mode"]
        self.assertEqual("MEASURED", cell["stamp"])
        self.assertEqual("blocking", cell["value"])
        self.assertEqual(now.isoformat(), cell["observed_at"])
        self.assertEqual((now + timedelta(seconds=120)).isoformat(), cell["valid_until"])
        validate_json_schema(document, SCHEMA)

    def test_archive_member_alias_is_rejected_before_external_bytes_are_read(self):
        from tests.test_archive_retention_doctor import ArchiveRetentionDoctorTests

        fixture = ArchiveRetentionDoctorTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.root = fixture.root
        ConfluenceGrantLedger(self.root).grant("puddle", "archive-alias-control")
        external = self.base / "external-events.jsonl"
        external.write_bytes(b"private external data\n")
        member = fixture.archive / "events.jsonl"
        member.unlink()
        member.symlink_to(external)
        native = Path.read_bytes

        def reject_external(path):
            self.assertNotEqual(external, path.resolve(), "Confluence read an external archive member")
            return native(path)

        with mock.patch.object(Path, "read_bytes", reject_external):
            with self.assertRaises(FloatiError):
                self.bundle()

    def test_archive_projection_holds_the_shared_epoch_barrier(self):
        from floati import confluence_fleet_state as subject
        from floati.bus_epoch import _require_owned_epoch_guard

        original = subject.read_archive_policy

        def read_policy(root):
            _require_owned_epoch_guard(root)
            return original(root)

        with mock.patch.object(subject, "read_archive_policy", read_policy):
            self.bundle()

    def test_deeply_nested_waiter_observation_is_named_unknown(self):
        path = self.root.path / "state/waiter-observations/builder-a.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[" * 1000 + "0" + "]" * 1000)
        cell = self.bundle()["fleet_state"]["waiters"][0]["mode"]
        self.assertEqual("UNKNOWN", cell["stamp"])
        self.assertEqual("waiter_observation_invalid", cell["reason"])

    def test_bundle_ignores_unrelated_archive_names(self):
        baseline = self.bundle()["fleet_state"]["archive_retention"]
        notes = self.root.path / "archive-notes.txt"
        notes.write_text("operator notes")
        state = self.bundle()["fleet_state"]["archive_retention"]
        self.assertEqual(baseline, state)
        self.assertEqual("operator notes", notes.read_text())

    def test_bundle_observes_one_epoch_while_roll_competes_with_roster(self):
        import fcntl
        import os

        from floati.bus_epoch import roll_bus_epoch
        from floati.events import EventLog
        from floati.planes import AuthorityGrantStore
        from floati.roster import project_roster

        EventLog(self.root).send("builder-a", "builder-b", "floati", "a" * 40,
                                "note.md", "before roll", idempotency_key="before-roll")
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "builder-a", 1, datetime.now(timezone.utc))
        roll_args = {"actor": "builder-a", "idempotency_key": "during-export"}
        def project_with_competing_roll(root, observed_at):
            descriptor = os.open(root.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return project_roster(root, observed_at)
            finally:
                os.close(descriptor)
            next_stamp = (observed_at + timedelta(seconds=1)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            with mock.patch("floati.registry.utc_now", return_value=next_stamp):
                roll_bus_epoch(root, **roll_args)
            return project_roster(root, observed_at)

        with mock.patch("floati.roster.project_roster", side_effect=project_with_competing_roll):
            observed = self.bundle()
        self.assertEqual([], observed["fleet_state"]["archive_retention"]["epochs"])
        roll_bus_epoch(self.root, **roll_args)
        self.assertEqual(1, len(self.bundle()["fleet_state"]["archive_retention"]["epochs"]))

    def test_fleet_state_exports_archive_size_expiry_policy_and_removal_receipt(self):
        from floati.epoch_rotation import maintenance
        from floati.jsonl import read_records_snapshot
        from floati.archive_retention import REMOVAL_LEDGER, REMOVAL_KIND
        from tests.test_archive_retention_doctor import ArchiveRetentionDoctorTests

        fixture = ArchiveRetentionDoctorTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.root = fixture.root
        ConfluenceGrantLedger(self.root).grant("puddle", "archive-bundle")
        stamp = datetime.fromisoformat(fixture.receipt["timestamp"].replace("Z", "+00:00"))
        expiry = (stamp + timedelta(days=30)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for forever in (False, True):
            with self.subTest(keep_forever=forever):
                policy = self.root.path / "state/archive-policy.json"
                policy.write_text(json.dumps({"schema_version": 1, "keep_forever": forever}))
                maintenance(self.root, actor="sender", trigger="send")
                before = {p.relative_to(self.root.path): p.read_bytes()
                          for p in self.root.path.rglob("*") if p.is_file()}
                document = self.bundle()
                state = document["fleet_state"]["archive_retention"]
                self.assertEqual(forever, state["keep_forever"])
                self.assertEqual(1, len(state["epochs"]))
                epoch = state["epochs"][0]
                self.assertEqual(fixture.receipt["epoch_id"], epoch["epoch_id"])
                self.assertEqual(fixture.archive.stat().st_size, epoch["size_bytes"])
                self.assertEqual(None if forever else expiry, epoch["expires_at"])
                self.assertFalse(epoch["expired"])
                self.assertFalse(epoch["removed"])
                self.assertEqual(before, {p.relative_to(self.root.path): p.read_bytes()
                                         for p in self.root.path.rglob("*") if p.is_file()})
                validate_json_schema(document, SCHEMA)

        policy.write_text(json.dumps({"schema_version": 1, "keep_forever": False}))

        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return stamp + timedelta(days=30, seconds=1)

        with mock.patch("floati.epoch_rotation.datetime", FutureClock):
            maintenance(self.root, actor="sender", trigger="send")
        removed = read_records_snapshot(self.root, REMOVAL_LEDGER, allowed_kinds={REMOVAL_KIND})[-1]
        state = self.bundle()["fleet_state"]["archive_retention"]
        self.assertEqual(removed, state["last_removal_receipt"])
        self.assertEqual(0, state["epochs"][0]["size_bytes"])
        self.assertTrue(state["epochs"][0]["removed"])
