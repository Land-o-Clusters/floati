"""F2: recent session evidence must retain its source and coverage limits."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids
from floati.errors import IntegrityFailure
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_health import WakeHealthProjection
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
SCHEMA = Path("schemas/v1/wake-health-fact.schema.json")
BEFORE = Path("tests/fixtures/f2-wake-health-v1-recent-before.schema.json")


class RecentSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node = public_ids.builder("recent")
        Registry(self.root).register(self.node, "Cursor")
        self.native_path = Path("receipts/wakes") / (self.node + ".jsonl")
        self.journal_path = Path("state/cursor-wait") / self.node / "journal.jsonl"

    def native(self, session: str, stamp: str, *, outcome: str = "woke", node=None):
        row = {
            "schema_version": 1, "id": "wake-attempt-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": stamp,
            "kind": "wake_attempt_receipt", "node_id": node or self.node,
            "acting_session_id": session, "message_worker_session_id": None,
            "idempotency_key": uuid7_hex(), "item_ids": ["msg-" + uuid7_hex()],
            "decision_receipt_id": "wake-hold-" + uuid7_hex(),
            "outcome": outcome,
            "reason_code": "wake_prompt_failed" if outcome == "refused" else None,
        }
        append_record(self.root, self.native_path, row, allowed_kinds={"wake_attempt_receipt"})
        return row

    def cursor(self, session="conversation-a", stamp="2026-10-02T11:30:00Z", **extra):
        row = {
            "ts": "2026-10-02T10:59:00Z", "event": "followup_mail",
            "node": self.node, "status": "completed", "loop_count": 1,
            "conversation_id": session, "last_seen_root": "/fixture/workspace",
            "publication": "flushed", "published_at": stamp, **extra,
        }
        path = self.root.tenant_home / self.journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(row, sort_keys=True).encode("utf-8")
        with path.open("ab") as stream:
            stream.write(encoded + b"\n")
        return row, hashlib.sha256(encoded).hexdigest()

    def recent(self, now=NOW):
        fact = WakeHealthProjection(self.root).fact(self.node, now)
        self.assertIn("recent_sessions", fact, "doctor has no bounded session evidence")
        return fact["recent_sessions"]

    def test_only_positive_native_receipts_enter_deduplicated_hour(self):
        """Catches queued/refused receipts masquerading as successful wakes."""
        late = self.native("session-b", "2026-10-02T11:50:00Z")
        self.native("session-queued", "2026-10-02T11:51:00Z", outcome="queued")
        self.native("session-refused", "2026-10-02T11:52:00Z", outcome="refused")
        early = self.native("session-b", "2026-10-02T11:10:00Z")
        a = self.native("session-a", "2026-10-02T11:40:00Z")
        native = self.recent()["native_woke"]
        self.assertEqual("recorded_receipts_only", native["coverage"])
        self.assertEqual(self.native_path.as_posix(), native["source_path"])
        self.assertEqual(["session-a", "session-b"], [row["session_id"] for row in native["sessions"]])
        self.assertEqual(1, native["sessions"][0]["evidence_count"])
        self.assertEqual(a["id"], native["sessions"][0]["last_record"]["receipt_id"])
        self.assertEqual({
            "session_id": "session-b", "evidence_count": 2,
            "first_at": "2026-10-02T11:10:00Z", "last_at": "2026-10-02T11:50:00Z",
            "first_record": {"line": 4, "receipt_id": early["id"]},
            "last_record": {"line": 1, "receipt_id": late["id"]},
        }, native["sessions"][1])

    def test_closed_hour_excludes_past_and_future_without_rounding(self):
        """Catches either boundary being exclusive or fractional time being truncated."""
        for session, stamp in (
            ("lower", "2026-10-02T11:00:00.001Z"),
            ("upper", "2026-10-02T12:00:00.001Z"),
            ("past", "2026-10-02T11:00:00Z"),
            ("future", "2026-10-02T12:00:00.002Z"),
        ):
            self.native(session, stamp)
            self.cursor(session, stamp)
        recent = self.recent(NOW + timedelta(milliseconds=1))
        for plane in ("native_woke", "cursor_local_publication"):
            self.assertEqual(["lower", "upper"], [row["session_id"] for row in recent[plane]["sessions"]])
        self.assertEqual(NOW + timedelta(milliseconds=1), datetime.fromisoformat(recent["window_end"].replace("Z", "+00:00")))
        self.assertEqual(3600, recent["window_seconds"])
        offset = timezone(timedelta(hours=-4))
        self.assertEqual(recent, self.recent((NOW + timedelta(milliseconds=1)).astimezone(offset)))

    def test_equal_time_uses_physical_line_and_preserves_opaque_native_id(self):
        """Catches ID rewriting and nondeterministic timestamp ties."""
        session = "opaque session/" + "x" * 300
        first = self.native(session, "2026-10-02T11:30:00Z")
        last = self.native(session, "2026-10-02T11:30:00.000Z")
        row = self.recent()["native_woke"]["sessions"][0]
        self.assertEqual(session, row["session_id"])
        self.assertEqual({"line": 1, "receipt_id": first["id"]}, row["first_record"])
        self.assertEqual({"line": 2, "receipt_id": last["id"]}, row["last_record"])

    def test_refused_attempt_does_not_replace_last_positive_session(self):
        """Catches last_seen_session taking the latest refused session."""
        self.native("positive-session", "2026-10-02T09:00:00Z")
        self.native("refused-session", "2026-10-02T11:50:00Z", outcome="refused")
        fact = WakeHealthProjection(self.root).fact(self.node, NOW)
        self.assertEqual("positive-session", fact["last_seen_session"])
        self.assertEqual([], self.recent()["native_woke"]["sessions"])

    def test_native_foreign_node_is_malformed_not_positive_evidence(self):
        """Catches a valid same-tenant receipt being trusted under another node's path."""
        self.native("foreign-session", "2026-10-02T11:30:00Z", node=public_ids.builder("foreign"))
        with self.assertRaises(IntegrityFailure):
            WakeHealthProjection(self.root).fact(self.node, NOW)

    def test_native_framing_and_duplicate_refusals_are_not_swallowed(self):
        """Catches malformed native evidence being downgraded to empty coverage."""
        self.native("session-a", "2026-10-02T11:30:00Z")
        path = self.root.tenant_home / self.native_path
        original = path.read_bytes()
        for broken in (original + original, original[:-1], b"{}\n"):
            with self.subTest(broken=broken[:12]):
                path.write_bytes(broken)
                with self.assertRaises(IntegrityFailure):
                    WakeHealthProjection(self.root).fact(self.node, NOW)
        path.write_bytes(original)

    def test_cursor_publications_remain_separate_and_retain_diagnostic_reason(self):
        """Catches Cursor continuation publication being counted as native woke."""
        self.native("conversation-a", "2026-10-02T11:20:00Z")
        self.cursor("conversation-a", "2026-10-02T11:25:00Z", event="followup_rearm")
        _row, digest = self.cursor("conversation-a", "2026-10-02T11:35:00Z",
            event="followup_participation_refused", reason_code="cursor_wait_consent_absent")
        recent = self.recent()
        self.assertEqual(1, recent["native_woke"]["sessions"][0]["evidence_count"])
        cursor = recent["cursor_local_publication"]
        self.assertEqual("complete_read", cursor["coverage"])
        self.assertEqual(0, cursor["legacy_row_count"])
        self.assertEqual(2, cursor["sessions"][0]["evidence_count"])
        self.assertEqual({"line": 2, "sha256": digest, "event": "followup_participation_refused",
            "reason_code": "cursor_wait_consent_absent"}, cursor["sessions"][0]["last_record"])

    def test_cursor_legacy_and_invalid_rows_cannot_claim_publication(self):
        """Catches preflush rows, fallback identities and malformed markers becoming proof."""
        self.cursor("legacy", publication=None, published_at=None)
        path = self.root.tenant_home / self.journal_path
        legacy = json.loads(path.read_text())
        legacy.pop("publication")
        legacy.pop("published_at")
        path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        for extra in (
            {"conversation_id": None}, {"conversation_id": "bad id"},
            {"event": "exit_empty_aborted"}, {"publication": "queued"},
            {"node": "foreign"}, {"published_at": "not-a-time"},
            {"published_at": "2026-10-02T11:30:00"},
        ):
            self.cursor(**extra)
        with path.open("ab") as stream:
            stream.write(b"not json\n\xff\n")
        _row, digest = self.cursor("valid")
        with path.open("ab") as stream:
            stream.write(b'{"unfinished":')
        cursor = self.recent()["cursor_local_publication"]
        self.assertEqual("incomplete", cursor["coverage"])
        self.assertEqual(1, cursor["legacy_row_count"])
        self.assertEqual(10, cursor["invalid_row_count"])
        self.assertEqual(["valid"], [row["session_id"] for row in cursor["sessions"]])
        self.assertEqual(11, cursor["sessions"][0]["last_record"]["line"])
        self.assertEqual(digest, cursor["sessions"][0]["last_record"]["sha256"])

    def test_cursor_empty_events_are_nonpositive_without_legacy_uncertainty(self):
        """Catches explicit empty responses being described as unproven publications."""
        path = self.root.tenant_home / self.journal_path
        rows = []
        for event in ("exit_empty_participation", "exit_empty_aborted", "exit_empty_aborted_twice"):
            row, _digest = self.cursor(event=event)
            row.pop("publication")
            row.pop("published_at")
            rows.append(row)
            with self.subTest(event=event):
                path.write_text(json.dumps(row) + "\n", encoding="utf-8")
                cursor = self.recent()["cursor_local_publication"]
                self.assertEqual("complete_read", cursor["coverage"])
                self.assertEqual(0, cursor["legacy_row_count"])
                self.assertEqual(0, cursor["invalid_row_count"])
                self.assertEqual([], cursor["sessions"])
        legacy, _digest = self.cursor(event="followup_mail")
        legacy.pop("publication")
        legacy.pop("published_at")
        path.write_text("".join(json.dumps(row) + "\n" for row in [*rows, legacy]), encoding="utf-8")
        cursor = self.recent()["cursor_local_publication"]
        self.assertEqual("complete_read", cursor["coverage"])
        self.assertEqual(1, cursor["legacy_row_count"])
        self.assertEqual(0, cursor["invalid_row_count"])
        self.assertEqual([], cursor["sessions"])

    def test_cursor_marker_bearing_empty_events_remain_invalid(self):
        """Catches an empty event carrying contradictory publication testimony."""
        path = self.root.tenant_home / self.journal_path
        for event in ("exit_empty_participation", "exit_empty_aborted", "exit_empty_aborted_twice"):
            original, _digest = self.cursor(event=event)
            for marker in ("both", "publication", "published_at"):
                row = dict(original)
                if marker != "both":
                    row.pop("published_at" if marker == "publication" else "publication")
                with self.subTest(event=event, marker=marker):
                    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
                    cursor = self.recent()["cursor_local_publication"]
                    self.assertEqual("incomplete", cursor["coverage"])
                    self.assertEqual(0, cursor["legacy_row_count"])
                    self.assertEqual(1, cursor["invalid_row_count"])
                    self.assertEqual([], cursor["sessions"])

    def test_cursor_absence_empty_and_nonregular_sources_have_typed_coverage(self):
        """Catches absent or unreadable testimony becoming an unexplained empty list."""
        self.assertEqual("absent", self.recent()["cursor_local_publication"]["coverage"])
        path = self.root.tenant_home / self.journal_path
        path.parent.mkdir(parents=True)
        path.touch()
        self.assertEqual("complete_read", self.recent()["cursor_local_publication"]["coverage"])
        path.unlink()
        path.mkdir()
        self.assertEqual("unavailable", self.recent()["cursor_local_publication"]["coverage"])

    def test_cursor_hostile_json_is_incomplete_without_hiding_valid_rows(self):
        """Catches malformed field types or ambiguous JSON crashing/creating testimony."""
        valid, _digest = self.cursor("valid")
        encoded = json.dumps(valid).encode()
        malformed = [
            json.dumps({**valid, "event": []}).encode(),
            json.dumps({**valid, "event": {}}).encode(),
            b'{"publication":"not-flushed",' + encoded[1:],
            b'{"nested":' + b'[' * 1200 + b'0' + b']' * 1200 + b',' + encoded[1:],
            b'{"nonfinite":NaN,' + encoded[1:],
        ]
        path = self.root.tenant_home / self.journal_path
        for raw in malformed:
            with self.subTest(raw=raw[:50]):
                path.write_bytes(raw + b"\n" + encoded + b"\n")
                cursor = self.recent()["cursor_local_publication"]
                self.assertEqual("incomplete", cursor["coverage"])
                self.assertEqual(1, cursor["invalid_row_count"])
                self.assertEqual(1, cursor["sessions"][0]["evidence_count"])
                self.assertEqual(2, cursor["sessions"][0]["last_record"]["line"])

    def test_cursor_fifo_is_opened_nonblocking_and_refused_before_read(self):
        """Catches a diagnostic reader hanging on an attacker-controlled FIFO."""
        path = self.root.tenant_home / self.journal_path
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        original_open = os.open

        def guarded_open(name, flags, *args, **kwargs):
            if name == "journal.jsonl":
                self.assertTrue(flags & os.O_NONBLOCK, "FIFO leaf open would block")
            return original_open(name, flags, *args, **kwargs)

        with mock.patch("floati.wake_health.os.open", side_effect=guarded_open):
            self.assertEqual("unavailable", self.recent()["cursor_local_publication"]["coverage"])

    def test_cursor_open_refusal_and_leaf_swap_remain_unavailable(self):
        """Catches access failures escaping and a precheck/open symlink race."""
        self.cursor()
        path = self.root.tenant_home / self.journal_path
        outside = self.base / "outside.jsonl"
        outside.write_bytes(path.read_bytes())
        original_open = os.open

        def unavailable_open(name, flags, *args, **kwargs):
            if name == "journal.jsonl":
                raise PermissionError("fixture permission refusal")
            return original_open(name, flags, *args, **kwargs)

        with mock.patch("floati.wake_health.os.open", side_effect=unavailable_open):
            self.assertEqual("unavailable", self.recent()["cursor_local_publication"]["coverage"])

        def swapped_open(name, flags, *args, **kwargs):
            if name == "journal.jsonl":
                path.unlink()
                path.symlink_to(outside)
            return original_open(name, flags, *args, **kwargs)

        with mock.patch("floati.wake_health.os.open", side_effect=swapped_open):
            self.assertEqual("unavailable", self.recent()["cursor_local_publication"]["coverage"])

    def test_cursor_symlinked_leaf_and_parent_are_unavailable(self):
        """Catches following a journal link outside the exact node source."""
        outside = self.base / "outside.jsonl"
        outside.write_text("{}\n")
        path = self.root.tenant_home / self.journal_path
        path.parent.mkdir(parents=True)
        path.symlink_to(outside)
        self.assertEqual("unavailable", self.recent()["cursor_local_publication"]["coverage"])
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(self.base)
        self.assertEqual("unavailable", self.recent()["cursor_local_publication"]["coverage"])

    def test_cursor_read_limits_do_not_return_a_silent_prefix(self):
        """Catches an oversized source being truncated into apparent complete evidence."""
        self.cursor()
        for bound in ("MAX_LEDGER_BYTES", "MAX_RECORD_BYTES", "MAX_LEDGER_RECORDS"):
            with self.subTest(bound=bound), mock.patch("floati.wake_health." + bound, 1, create=True):
                if bound == "MAX_LEDGER_RECORDS":
                    self.cursor("conversation-b")
                cursor = self.recent()["cursor_local_publication"]
                self.assertEqual("over_limit", cursor["coverage"])
                self.assertEqual([], cursor["sessions"])

    def test_recent_projection_reads_no_legacy_scratch_and_writes_nothing(self):
        """Catches broad discovery or a reader creating testimony/cache/lock files."""
        scratch = self.root.tenant_home / "scratch/cursor-hook/journal.jsonl"
        scratch.parent.mkdir(parents=True)
        scratch.write_text('{"conversation_id":"legacy-scratch"}\n')
        self.native("native", "2026-10-02T11:30:00Z")
        self.cursor()
        before = {p.relative_to(self.root.path).as_posix(): p.read_bytes()
            for p in self.root.path.rglob("*") if p.is_file()}
        recent = self.recent()
        after = {p.relative_to(self.root.path).as_posix(): p.read_bytes()
            for p in self.root.path.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertNotIn("legacy-scratch", json.dumps(recent))

    def test_optional_schema_keeps_old_facts_and_rejects_new_unsafe_fields(self):
        """Catches accidental required fields or unclosed evidence objects."""
        self.native("native", "2026-10-02T11:30:00Z")
        self.cursor()
        fact = WakeHealthProjection(self.root).fact(self.node, NOW)
        self.assertIn("recent_sessions", fact)
        validate_json_schema(fact, SCHEMA)
        old = copy.deepcopy(fact)
        old.pop("recent_sessions")
        validate_json_schema(old, BEFORE)
        validate_json_schema(old, SCHEMA)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(fact, BEFORE)
        for mutation in ("extra", "control", "bidi", "absolute-path"):
            changed = copy.deepcopy(fact)
            plane = changed["recent_sessions"]["native_woke"]
            if mutation == "extra":
                plane["sessions"][0]["unexpected"] = True
            elif mutation == "absolute-path":
                plane["source_path"] = "/private/native.jsonl"
            else:
                plane["sessions"][0]["session_id"] = "unsafe" + ("\x1b" if mutation == "control" else "\u202e")
            with self.subTest(mutation=mutation), self.assertRaises(SchemaValidationError):
                validate_json_schema(changed, SCHEMA)

    def test_schema_extension_leaves_preexisting_contract_unchanged(self):
        """Catches weakening a preexisting field while adding optional history."""
        old = json.loads(BEFORE.read_text())
        self.assertEqual("c5f98ae395c4f6d5d0f4339ea31408197d0a75761d1f98168f9741d8d4f1a1e1",
            hashlib.sha256(BEFORE.read_bytes()).hexdigest())
        # This row's extension is frozen before the later unavailable-history
        # alternatives; the legacy-read row separately tests that closed delta.
        new = json.loads(Path("tests/fixtures/wake-receipt-legacy-read-before.schema.json").read_text())
        self.assertIn("recent_sessions", new["properties"])
        self.assertNotIn("recent_sessions", new["required"])
        new["properties"].pop("recent_sessions")
        for key in list(new["$defs"]):
            if key not in old["$defs"]:
                new["$defs"].pop(key)
        self.assertEqual(old, new)

    def test_status_cache_refreshes_native_wakes_and_cursor_publications(self):
        """Catches same-clock receipt/journal appends hidden by a cached status."""
        projection = FleetProjection(self.root)
        first = projection.status_artifact(NOW)
        self.assertIn("recent_sessions", first["nodes"][0]["wake_health"])
        self.native("native-a", "2026-10-02T11:30:00Z")
        native = projection.status_artifact(NOW)["nodes"][0]["wake_health"]
        self.assertEqual("native-a", native["last_seen_session"])
        self.native("native-b", "2026-10-02T11:40:00Z")
        appended = projection.status_artifact(NOW)["nodes"][0]["wake_health"]
        self.assertEqual("native-b", appended["last_seen_session"])
        self.assertEqual(["native-a", "native-b"],
            [row["session_id"] for row in appended["recent_sessions"]["native_woke"]["sessions"]])
        self.cursor("cursor-a")
        with mock.patch.object(FleetProjection, "_snapshot", side_effect=AssertionError("journal-only cache hit performed a full projection")):
            cached = projection.status_artifact(NOW)["nodes"][0]["wake_health"]
        self.assertEqual("cursor-a", cached["recent_sessions"]["cursor_local_publication"]["sessions"][0]["session_id"])

    def test_old_cached_health_is_reprojected_and_removed_sources_disappear(self):
        """Catches pre-extension caches and deleted sources retaining stale testimony."""
        self.native("native", "2026-10-02T11:30:00Z")
        self.cursor()
        projection = FleetProjection(self.root)
        projection.status_artifact(NOW)
        store = projection._status_snapshot_store()
        payload = store.load().payload
        payload["artifact"]["nodes"][0]["wake_health"].pop("recent_sessions")
        store.refresh(payload)
        with mock.patch.object(FleetProjection, "_snapshot", wraps=projection._snapshot) as full:
            refreshed = projection.status_artifact(NOW)
        self.assertEqual(1, full.call_count)
        self.assertIn("recent_sessions", refreshed["nodes"][0]["wake_health"])
        (self.root.tenant_home / self.journal_path).unlink()
        with mock.patch.object(FleetProjection, "_snapshot", side_effect=AssertionError("journal deletion forced full projection")):
            removed = projection.status_artifact(NOW)["nodes"][0]["wake_health"]
        self.assertEqual("absent", removed["recent_sessions"]["cursor_local_publication"]["coverage"])
        (self.root.tenant_home / self.native_path).unlink()
        removed = projection.status_artifact(NOW)["nodes"][0]["wake_health"]
        self.assertIsNone(removed["last_seen_session"])
        self.assertEqual([], removed["recent_sessions"]["native_woke"]["sessions"])

    def test_cursor_only_cached_window_expires_without_full_projection(self):
        """Catches cached journal evidence outliving its hour when no ledger changes."""
        self.cursor()
        projection = FleetProjection(self.root)
        first = projection.status_artifact(NOW)
        self.assertIn("recent_sessions", first["nodes"][0]["wake_health"])
        with mock.patch.object(FleetProjection, "_snapshot", side_effect=AssertionError("empty-ledger clock forced a full projection")):
            later = projection.status_artifact(NOW + timedelta(hours=1))
        self.assertEqual([], later["nodes"][0]["wake_health"]["recent_sessions"]["cursor_local_publication"]["sessions"])


if __name__ == "__main__":
    unittest.main()
