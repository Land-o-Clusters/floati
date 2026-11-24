"""Read surfaces preserve unknown provider state and never inspect history bytes."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

from floati.doctor import Doctor
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.projection import FleetProjection
from floati.records import THREAD_OBSERVATION_KINDS, validate_record
from floati.tui_render import HarborBoardModel, render_plain_dump
from tests.fixtures.adhoc_stage1 import AdhocFixture
from tests.test_thread_observations import thread_record_rows
from tests.schema_validation import validate_json_schema


class AdhocProjectionTests(AdhocFixture, unittest.TestCase):
    def joined(self):
        snapshot = FleetProjection(self.root).snapshot(now=self.now)
        self.assertIn("adhoc_threads", snapshot,
            "status must expose joined ad-hoc testimony independently of provider liveness")
        return snapshot, snapshot["adhoc_threads"]["profiles"]

    def test_doctor_and_board_share_joined_thread_unknown_state(self):
        profile = self.profile()
        before = self.tree_bytes()
        with mock.patch("floati.thread_source.CodexLocalThreadSource.read",
                        side_effect=AssertionError("read surfaces must never poll providers")),\
                mock.patch("floati.doctor.project_remote_control_worker_findings", return_value=[]):
            snapshot, rows = self.joined()
            self.assertEqual([profile["id"]], [row["profile_id"] for row in rows])
            row = rows[0]
            self.assertEqual(self.lease["id"], row["node_lease_id"])
            self.assertEqual({"value": "unknown", "evidence_class": "unknown"}, row["provider_status"])
            self.assertEqual("not_observed", row["observation_reason"])
            self.assertIsNone(row["provider_coordinate"])
            board = HarborBoardModel.from_projection(snapshot, [], {})
            self.assertEqual(snapshot["adhoc_threads"], board.to_snapshot()["adhoc_threads"])
            self.assertIn("UNKNOWN", render_plain_dump(board))
            artifact, _ = Doctor(self.repository, self.root.path, ref="HEAD",
                codex_hooks=self.base / "absent-hooks.json",
                codex_gateway_host=self.base / "absent-gateway").artifact()
        findings = [finding for finding in artifact["findings"] if finding["code"] == "adhoc_threads"]
        self.assertEqual(1, len(findings), artifact)
        self.assertEqual(snapshot["adhoc_threads"], json.loads(findings[0]["detail"]))
        self.assertEqual(before, self.tree_bytes())

    def test_provider_history_size_is_metadata_only_and_symlinks_stay_unknown(self):
        profile = self.profile()
        slot = self.root.path / profile["provider_state"]
        private = slot / "conversation.jsonl"
        canary = b"PRIVATE-HISTORY-MUST-NOT-BE-EXPORTED\n"
        private.write_bytes(canary)
        expected = sum(path.stat().st_size for path in slot.iterdir() if path.is_file())
        actual_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == private:
                raise AssertionError("history cannot be read")
            return actual_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", guarded_open):
            _, rows = self.joined()
        self.assertEqual({"value": expected, "evidence_class": "measured"}, rows[0]["provider_state_bytes"])
        self.assertNotIn(canary.decode().strip(), repr(rows))
        outside = self.base / "foreign-history"
        outside.write_bytes(b"foreign-history")
        (slot / "unsafe").symlink_to(outside)
        _, rows = self.joined()
        size = rows[0]["provider_state_bytes"]
        self.assertIsNone(size["value"])
        self.assertEqual("unknown", size["evidence_class"])
        self.assertEqual("provider_state_unsafe", size["reason"])
        self.assertEqual(b"foreign-history", outside.read_bytes())


class AdhocThreadRecordTests(unittest.TestCase):
    def test_historical_codex_thread_records_keep_exact_validation(self):
        original = thread_record_rows("fixture-tenant")[0]
        validate_record(original, "fixture-tenant", THREAD_OBSERVATION_KINDS, integrity=False)
        for provider, identifier in (("codex_local", "plain-provider-id"),
                                     ("claude_local", "session-id"),
                                     ("pi_local", "session-id")):
            changed = copy.deepcopy(original)
            changed.update(provider=provider, provider_thread_id=identifier)
            with self.subTest(provider=provider):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(changed, "fixture-tenant", THREAD_OBSERVATION_KINDS, integrity=False)
        for provider, identifier in (("claude_local", "b413a611-c66f-40b3-8de3-79ff8548c226"),
                                     ("pi_local", "fixture-session-id")):
            with self.subTest(versioned_provider=provider):
                versioned = copy.deepcopy(original)
                versioned.update(schema_version=2, provider=provider,
                    provider_thread_id=identifier, subject_kind="adhoc_dispatch",
                    adhoc_profile_id="adhoc-profile-" + uuid7_hex(),
                    adhoc_dispatch_id="adhoc-dispatch-" + uuid7_hex(),
                    native_receipt_id="adhoc-native-opened-" + uuid7_hex())
                try:
                    validate_record(versioned, "fixture-tenant",
                        THREAD_OBSERVATION_KINDS, integrity=False)
                    validate_json_schema(versioned,
                        Path("schemas/v2/thread-attachment-registered-record.schema.json"))
                except ProtocolRefusal as exc:
                    self.fail("authenticated version-two provider form rejected: " + exc.code)

    def test_versioned_thread_attachment_requires_actual_native_testimony(self):
        from floati.thread_observations import ThreadObserver
        from tests.test_native_thread_protocol import fresh_native_fixture
        with fresh_native_fixture(self) as case:
            observer = ThreadObserver(case.root)
            before = case.tree_bytes()
            with self.assertRaises(ProtocolRefusal) as caught:
                observer.register_adhoc(case.admitted["dispatch_id"], "node-a", now=case.now)
            self.assertEqual("native_session_unmaterialized", caught.exception.code)
            self.assertEqual(before, case.tree_bytes())
            ack = case.controller().accept(case.event(), now=case.now)
            attached = observer.register_adhoc(case.admitted["dispatch_id"], "node-a", now=case.now)
            self.assertEqual(2, attached["schema_version"])
            self.assertEqual(ack["opened_record_id"], attached["native_receipt_id"])
            self.assertEqual(case.profile_record["id"], attached["adhoc_profile_id"])
            self.assertEqual(case.coordinate["session_id"], attached["provider_thread_id"])
            self.assertEqual(attached, observer.register_adhoc(
                case.admitted["dispatch_id"], "node-a", now=case.now))
            with mock.patch.object(observer.source, "read",
                                   side_effect=AssertionError("ad-hoc source must not be polled")):
                with self.assertRaises(ProtocolRefusal) as caught:
                    observer.observe(attached["id"], now=case.now)
            self.assertEqual("native_status_unsupported", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
