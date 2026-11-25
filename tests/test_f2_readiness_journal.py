"""F2-READINESS-1: send testimony survives retries and stays out of cut bundles.

These controls use native sends, leases, deliveries and acknowledgments. Faults
cut their scratch frames or interrupt existing formatting/encoding boundaries;
they never manufacture a readiness answer or a Stop-hold receipt. This module
imports only interfaces present at the RED base.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati import jsonl
from floati.confluence import ConfluenceGrantLedger, materialize_bundle
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import FrameError, decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.mail_health import RecipientReadiness
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# These controls exercise readiness replay within a fresh epoch. Keep their
# relative lease clocks while avoiding an unrelated seven-day maintenance cut.
NOW = datetime.now(timezone.utc).replace(microsecond=0)
SHA = "a" * 40
OBSERVATION_KIND = "send_readiness_observation"
NOTE = "journal control: authored notification stays only in its envelope"


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ReadinessJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        home = self.base / "home"
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        environment = mock.patch.dict(os.environ, {
            "HOME": str(home), "CODEX_HOME": str(codex_home),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.registry = Registry(self.root)
        self.registry.register("sender", "worker")
        self.registry.register("recipient", "worker")
        self.events = EventLog(self.root, self.registry)

    def send(self, key: str = "f2-journal", *, now: datetime = NOW,
             claim: object = None) -> dict:
        return self.events.send(
            "sender", "recipient", "floati", SHA,
            "docs/evidence/readiness-control.md", NOTE,
            idempotency_key=key, now=now, claim=claim,
        )

    def event_bytes(self) -> bytes:
        return self.events.path.read_bytes() if self.events.path.exists() else b""

    def frames(self) -> list[dict]:
        return decode_frames(self.event_bytes())

    def observation(self, message: dict) -> dict:
        matches = [row for row in self.frames()
                   if row.get("kind") == OBSERVATION_KIND
                   and row.get("envelope_id") == message["id"]]
        self.assertEqual(1, len(matches),
                         "a successful send must leave exactly one envelope-bound observation")
        return matches[0]

    def assert_observation(self, result: dict) -> dict:
        self.assertIn("message", result)
        self.assertIn("recipient_readiness", result)
        message = result["message"]
        readiness = result["recipient_readiness"]
        self.assertIsInstance(readiness, dict)
        self.assertTrue({"state", "reason", "observed_at", "checked_mechanisms",
                         "mechanism_evidence"} <= set(readiness))
        row = self.observation(message)
        for field in ("envelope_id", "sender", "recipient", "observed_at",
                      "readiness", "idempotency_key"):
            self.assertIn(field, row)
        self.assertEqual(message["id"], row["envelope_id"])
        self.assertEqual(message["sender"], row["sender"])
        self.assertEqual(message["recipient"], row["recipient"])
        self.assertIsInstance(row["idempotency_key"], str)
        self.assertTrue(row["idempotency_key"])
        self.assertEqual(message["idempotency_key"], row["idempotency_key"])
        self.assertEqual(message["timestamp"], row["timestamp"])
        self.assertEqual(message["timestamp"], row["observed_at"])
        self.assertEqual(readiness["observed_at"], row["observed_at"])
        self.assertEqual(readiness, row["readiness"])
        self.assertNotIn(NOTE, json.dumps(row))
        self.assertNotIn("body", row)
        return row

    def lease(self, *, start: datetime = NOW,
              expiry: datetime = NOW + timedelta(hours=1)) -> dict:
        # Same native registry primitive and closed lease shape as the existing
        # node-lease protocol controls; this is administrative lifetime evidence.
        row = {
            "schema_version": 0, "id": "lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": stamp(start),
            "kind": "node_lease", "node_id": "recipient",
            "workspace": str(self.root.path / "nodes" / "recipient"),
            "expires_at": stamp(expiry), "state": "active",
        }
        append_record(self.root, self.registry.relative_path, row,
                      allowed_kinds=REGISTRY_KINDS)
        return row

    def claim(self) -> dict:
        subject = self.base / "claim-subject"
        subject.mkdir()
        return {
            "kind": "delivery_claim", "schema_version": 0, "sha": SHA,
            "repo_path": str(subject), "bank": ["tests.test_sample"],
            "declared": {"ran": 1, "result": "OK"},
            "artifacts": [], "deadline_seconds": 10,
        }

    def test_send_journals_exact_readiness_beside_an_unchanged_envelope(self) -> None:
        result = self.send()
        observation = self.assert_observation(result)
        self.assertEqual(["message_envelope", OBSERVATION_KIND],
                         [row["kind"] for row in self.frames()])
        message = result["message"]
        self.assertEqual({"schema_version", "id", "tenant_id", "timestamp",
                          "kind", "sender", "recipient", "repo", "sha", "doc",
                          "note", "idempotency_key", "attempt_binding"}, set(message))
        self.assertEqual(NOTE, message["note"])
        self.assertEqual(message, self.frames()[0])
        self.assertEqual(observation, self.events.strict_event_records()[1])

    def test_optional_claim_and_observation_are_one_native_append_batch(self) -> None:
        calls = []
        original = jsonl._append_frame

        def observe_append(path, payload, **kwargs):
            if path == self.events.path:
                calls.append(decode_frames(payload))
            return original(path, payload, **kwargs)

        with mock.patch.object(jsonl, "_append_frame", side_effect=observe_append):
            result = self.send(claim=self.claim())
        self.assertEqual(1, len(calls), "send must not publish a separate readiness append")
        self.assertEqual(["message_envelope", "delivery_claim", OBSERVATION_KIND],
                         [row["kind"] for row in calls[0]])
        self.assert_observation(result)
        self.assertEqual(result["message"]["id"], result["claim"]["note_ref"])
        before = self.event_bytes()
        replay = self.send(claim={key: result["claim"][key] for key in self.claim_fields()})
        self.assertEqual(dict(result), dict(replay))
        self.assertEqual(before, self.event_bytes())

    @staticmethod
    def claim_fields() -> tuple[str, ...]:
        return ("kind", "schema_version", "sha", "repo_path", "bank", "declared",
                "artifacts", "deadline_seconds")

    def test_explicit_observation_clock_controls_both_existing_timestamps(self) -> None:
        # This is already correct on the RED base: do not label a different
        # utc_now as a reproduced defect when explicit now is authoritative.
        observed = NOW - timedelta(minutes=5)
        self.lease(start=observed - timedelta(seconds=1))
        with mock.patch("floati.events.utc_now", return_value=stamp(NOW + timedelta(hours=1))):
            result = self.send(now=observed)
        self.assertEqual(stamp(observed), result["message"]["timestamp"])
        self.assertEqual(stamp(observed), result["recipient_readiness"]["observed_at"])
        self.assertEqual("recipient_wake_arranged", result["recipient_readiness"]["state"])

    def test_default_clock_after_guard_wait_cannot_revive_expired_native_board(self) -> None:
        """Catches evaluating a real two-second board at the pre-lock clock."""
        from floati.wake_hold import wake_coordination_guard
        from tests.test_f2_readiness_mechanisms import RecipientHookMechanismTests, NOW as board_now

        fixture = RecipientHookMechanismTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.board(deadline=2)
        clock = [board_now + timedelta(seconds=1)]
        after_wait = board_now + timedelta(seconds=2)

        @contextmanager
        def delayed_guard(*args, **kwargs):
            # Keep the actual physical guard; only passage of time is injected.
            with wake_coordination_guard(*args, **kwargs):
                clock[0] = after_wait
                yield

        with mock.patch("floati.wake_hold.wake_coordination_guard", delayed_guard),\
             mock.patch("floati.events.utc_now", side_effect=lambda: stamp(clock[0])):
            sent = EventLog(fixture.root).send("sender", fixture.node, "floati", SHA,
                "docs/evidence/readiness-control.md", NOTE,
                idempotency_key="board-expired-during-guard-wait")
        self.assertEqual("unknown", sent["recipient_readiness"]["state"], sent)
        self.assertEqual(stamp(after_wait), sent["message"]["timestamp"])
        self.assertEqual(stamp(after_wait), sent["recipient_readiness"]["observed_at"])
        rows = EventLog(fixture.root).event_records()
        observation = next(row for row in rows if row.get("envelope_id") == sent["message"]["id"])
        self.assertEqual(stamp(after_wait), observation["timestamp"])

    def test_default_protocol_lease_fence_uses_same_post_guard_clock(self) -> None:
        """Catches lease admission at wall time while readiness uses an injected clock."""
        from floati.wake_hold import wake_coordination_guard

        start = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.lease(start=start, expiry=start + timedelta(seconds=2))
        clock = [start + timedelta(seconds=1)]

        @contextmanager
        def delayed_guard(*args, **kwargs):
            with wake_coordination_guard(*args, **kwargs):
                clock[0] = start + timedelta(seconds=2)
                yield

        with mock.patch("floati.wake_hold.wake_coordination_guard", delayed_guard),\
             mock.patch("floati.events.utc_now", side_effect=lambda: stamp(clock[0])):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.send(now=None)
        self.assertEqual("node_lease_expired", caught.exception.code)
        self.assertEqual(b"", self.event_bytes())

    def test_exact_default_clock_replay_does_not_read_a_new_clock(self) -> None:
        first = self.send()
        before = self.event_bytes()
        with mock.patch("floati.events.utc_now", side_effect=AssertionError("replay read current time")):
            replay = self.send(now=None)
        self.assertEqual(dict(first), dict(replay))
        self.assertEqual(before, self.event_bytes())

    def test_guard_discovered_default_clock_replay_does_not_read_a_new_clock(self) -> None:
        first = self.send()
        before = self.event_bytes()
        original = self.events.event_records
        reads = 0

        def initially_stale_read():
            nonlocal reads
            reads += 1
            return [] if reads == 1 else original()

        with mock.patch.object(self.events, "event_records", side_effect=initially_stale_read),\
             mock.patch("floati.events.utc_now", side_effect=AssertionError("guard replay read current time")):
            replay = self.send(now=None)
        self.assertEqual(dict(first), dict(replay))
        self.assertEqual(before, self.event_bytes())

    def test_pause_after_observation_preserves_history_but_changes_next_send(self) -> None:
        """Catches recomputing frozen testimony or promising readiness through append."""
        from floati.wake_control import WakeController
        from tests.test_f2_readiness_mechanisms import RecipientHookMechanismTests, NOW as board_now

        fixture = RecipientHookMechanismTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.board(deadline=2)
        events = EventLog(fixture.root)
        observed = board_now + timedelta(seconds=1)
        paused = board_now + timedelta(seconds=1, milliseconds=250)
        original = RecipientReadiness._observe_already_guarded

        def observe_then_pause(*args, **kwargs):
            readiness = original(*args, **kwargs)
            self.assertEqual("recipient_wake_arranged", readiness.state)
            with mock.patch("floati.wake_control.utc_now", return_value=stamp(paused)):
                pause = WakeController(fixture.root).pause(fixture.node, "board-session",
                    idempotency_key="pause-after-frozen-observation")
            self.assertEqual("paused", pause["state"])
            return readiness

        def send(key, when):
            return events.send("sender", fixture.node, "floati", SHA,
                "docs/evidence/readiness-control.md", NOTE,
                idempotency_key=key, now=when)

        with mock.patch.object(RecipientReadiness, "_observe_already_guarded", side_effect=observe_then_pause):
            first = send("observed-before-pause", observed)
        self.assertEqual("recipient_wake_arranged", first["recipient_readiness"]["state"])
        self.assertEqual(stamp(observed), first["recipient_readiness"]["observed_at"])
        next_send = send("observed-after-pause", board_now + timedelta(seconds=1, milliseconds=500))
        self.assertEqual("unknown", next_send["recipient_readiness"]["state"])
        self.assertEqual(dict(first), dict(send("observed-before-pause", board_now + timedelta(seconds=1, milliseconds=750))))

    def test_replay_freezes_observation_before_a_later_lease_is_added(self) -> None:
        first = self.send()
        self.assertNotEqual("recipient_wake_arranged", first["recipient_readiness"]["state"])
        self.lease(start=NOW + timedelta(seconds=1))
        current = self.registry.node_lease_state("recipient", now=NOW + timedelta(seconds=2))
        self.assertEqual("active", current["state"], "the mechanism-change control must be real")
        before = self.event_bytes()
        replay = self.send(now=NOW + timedelta(seconds=2))
        self.assertEqual(first["recipient_readiness"], replay["recipient_readiness"],
                         "replay must read saved observation, not latest registry at old time")
        self.assertEqual(dict(first), dict(replay))
        self.assertEqual(before, self.event_bytes())
        self.assert_observation(replay)

    def test_saved_active_lease_observation_survives_expiry_on_exact_retry(self) -> None:
        self.lease(expiry=NOW + timedelta(seconds=5))
        first = self.send(now=NOW + timedelta(seconds=1))
        self.assertEqual("recipient_wake_arranged", first["recipient_readiness"]["state"])
        self.assertEqual("expired", self.registry.node_lease_state(
            "recipient", now=NOW + timedelta(seconds=6))["state"])
        before = self.event_bytes()
        replay = self.send(now=NOW + timedelta(seconds=6))
        self.assertEqual(first["recipient_readiness"], replay["recipient_readiness"])
        self.assertEqual(before, self.event_bytes())
        self.assert_observation(replay)

    def test_replay_discovered_under_recipient_guard_does_not_reobserve(self) -> None:
        first = self.send()
        self.assert_observation(first)
        before = self.event_bytes()
        original = self.events.event_records
        reads = 0

        def initially_stale_read():
            # Model a concurrent send completing after the first read. The
            # stored envelope and companion remain authentic native output.
            nonlocal reads
            reads += 1
            return [] if reads == 1 else original()

        replay = None
        failure = None
        with mock.patch.object(self.events, "event_records", side_effect=initially_stale_read):
            with mock.patch.object(RecipientReadiness, "artifact", side_effect=OSError("later observer unavailable")):
                try:
                    replay = self.send(now=NOW + timedelta(seconds=1))
                except OSError as exc:
                    failure = exc
        self.assertIsNone(failure, "a completed concurrent send must replay without a fresh observation")
        self.assertEqual(dict(first), dict(replay))
        self.assertEqual(before, self.event_bytes())

    def test_complete_append_survives_failure_before_the_returned_receipt(self) -> None:
        # _send_receipt is today's actual post-append/pre-stdout boundary.
        # A failed presentation does not license a second observation on retry.
        with mock.patch.object(EventLog, "_send_receipt", side_effect=OSError("receipt output interrupted")):
            with self.assertRaises(OSError):
                self.send()
        rows = self.frames()
        self.assertEqual(["message_envelope", OBSERVATION_KIND], [row["kind"] for row in rows])
        saved = rows[1]
        self.lease(start=NOW + timedelta(seconds=1))
        before = self.event_bytes()
        replay = self.send(now=NOW + timedelta(seconds=2))
        self.assertEqual(saved["readiness"], replay["recipient_readiness"])
        self.assertEqual(before, self.event_bytes())
        self.assert_observation(replay)

    def test_surviving_envelope_without_its_observation_is_unknown_on_replay(self) -> None:
        result = self.send()
        # Photograph a real valid envelope, then model a torn batch at the
        # complete-frame boundary. No new observation is invented by the test.
        envelope_frame = encode_frame(result["message"])
        self.events.path.write_bytes(envelope_frame)
        self.lease(start=NOW + timedelta(seconds=1))
        replay = self.send(now=NOW + timedelta(seconds=2))
        self.assertEqual("unknown", replay["recipient_readiness"]["state"],
                         "a partial or pre-change send has no historical readiness testimony")
        self.assertEqual("historical_readiness_missing", replay["recipient_readiness"]["reason"])
        self.assertEqual(result["message"]["timestamp"], replay["recipient_readiness"]["observed_at"])
        self.assertEqual([], replay["recipient_readiness"]["checked_mechanisms"])
        self.assertEqual([], replay["recipient_readiness"]["mechanism_evidence"])
        self.assertEqual(result["message"], replay["message"])
        self.assertEqual(envelope_frame, self.event_bytes(),
                         "retry must not manufacture a historical observation")

    def test_observation_encoding_failure_appends_neither_envelope_nor_claim(self) -> None:
        original = jsonl.encode_frame
        reached = []

        def fail_observation(record):
            if record.get("kind") == OBSERVATION_KIND:
                reached.append(record["envelope_id"])
                raise FrameError("record_not_ijson", "scratch observation encoder failure")
            return original(record)

        before = self.event_bytes()
        failure = None
        with mock.patch.object(jsonl, "encode_frame", side_effect=fail_observation):
            try:
                self.send(claim=self.claim())
            except (ProtocolRefusal, FrameError) as exc:
                failure = exc
        self.assertEqual(1, len(reached), "the observation encoder must be reached before mutation")
        self.assertIsNotNone(failure, "failed observation encoding cannot report a successful send")
        self.assertEqual(before, self.event_bytes())

    def test_observation_formatting_failure_does_not_leave_a_successful_send(self) -> None:
        # Inject a failure, never an answer, through the existing formatter.
        before = self.event_bytes()
        failure = None
        with mock.patch.object(RecipientReadiness, "artifact", side_effect=OSError("scratch observation failure")) as formatter:
            try:
                self.send()
            except OSError as exc:
                failure = exc
        self.assertGreater(formatter.call_count, 0, "fault must reach the native observation formatter")
        self.assertIsNotNone(failure)
        self.assertEqual(before, self.event_bytes(),
                         "format the observation before publishing a success-shaped envelope")

    def test_journal_party_tampering_is_rejected_by_the_strict_event_reader(self) -> None:
        result = self.send()
        observation = self.observation(result["message"])
        rows = self.frames()
        damaged = copy.deepcopy(observation)
        damaged["recipient"] = "sender"
        self.events.path.write_bytes(b"".join(encode_frame(
            damaged if row.get("id") == observation["id"] else row) for row in rows))
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.events.strict_event_records()

    def test_duplicate_observation_for_one_envelope_is_refused(self) -> None:
        result = self.send()
        duplicate = copy.deepcopy(self.observation(result["message"]))
        duplicate["id"] = "send-readiness-" + uuid7_hex()
        with self.events.path.open("ab") as handle:
            handle.write(encode_frame(duplicate))
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.events.strict_event_records()

    def test_observation_timestamp_must_match_its_envelope(self) -> None:
        result = self.send()
        observation = self.observation(result["message"])
        damaged = copy.deepcopy(observation)
        changed = stamp(NOW + timedelta(seconds=1))
        # Keep the observation internally consistent. Only its cross-record
        # relationship to the real envelope is false.
        damaged["timestamp"] = changed
        damaged["observed_at"] = changed
        damaged["readiness"]["observed_at"] = changed
        self.events.path.write_bytes(b"".join(encode_frame(
            damaged if row.get("id") == observation["id"] else row)
            for row in self.frames()))
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.events.strict_event_records()

    def test_observation_id_cannot_be_used_as_a_mail_reply_target(self) -> None:
        result = self.send()
        observation = self.observation(result["message"])
        before = self.event_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.send("recipient", "sender", "floati", SHA,
                "docs/evidence/reply.md", "reply", reply_to=observation["id"],
                idempotency_key="not-a-message", now=NOW)
        self.assertEqual("reply_to_unknown", caught.exception.code)
        self.assertEqual(before, self.event_bytes())

    def test_confluence_v2_excludes_a_native_observation_and_keeps_its_cut_shape(self) -> None:
        from floati.quiet_window import LEDGER_RELATIVE

        result = self.send()
        self.assert_observation(result)
        grant = ConfluenceGrantLedger(self.root).grant("consumer", "f2-readiness-grant")
        output = self.base / "bundle-v2.json"
        quiet_history = self.root.resolve_relative(LEDGER_RELATIVE)
        quiet_history.parent.mkdir(parents=True, exist_ok=True)
        quiet_history.write_bytes(b"partial frame")
        before = self.event_bytes()
        materialize_bundle(self.root, consumer="consumer", out=output, version=2)
        document = json.loads(output.read_bytes())
        # Ratified optional addition under the 2026-09-01 loosening precedent.
        self.assertEqual({"schema_version", "kind", "tenant_id", "snapshot_at",
                          "grant_id", "entries", "excluded_wake_hold_receipt_count",
                          "roster", "fleet_state"}, set(document))
        self.assertEqual(2, document["schema_version"])
        self.assertEqual(0, document["excluded_wake_hold_receipt_count"])
        self.assertEqual(["message_envelope"],
                         [entry["record"]["kind"] for entry in document["entries"]])
        self.assertEqual(result["message"], document["entries"][0]["record"])
        self.assertNotIn("load_hold", document["fleet_state"])
        validate_json_schema(document, REPOSITORY_ROOT / "schemas/v0/receipts-read-bundle-v2.schema.json")
        # A legacy reader keeps the original field order and nonempty entry
        # bytes, including the receipt-derived snapshot rather than roster age.
        legacy = json.loads((REPOSITORY_ROOT / "tests/fixtures/confluence/roster/legacy-v2.json")
                            .read_text(encoding="utf-8"))
        persisted_message = next(row for row in self.frames()
                                 if row["kind"] == "message_envelope")
        legacy.update(tenant_id=self.root.tenant_id, grant_id=grant["id"],
                      snapshot_at=result["message"]["timestamp"], entries=[{
                          "sequence": 1, "source": "events.jsonl", "source_ordinal": 1,
                          "record": persisted_message,
                      }])
        validate_json_schema(legacy, REPOSITORY_ROOT / "schemas/v0/receipts-read-bundle-v2.schema.json")
        legacy_view = {key: value for key, value in document.items()
                       if key not in {"roster", "fleet_state"}}
        self.assertEqual((json.dumps(legacy, indent=2) + "\n").encode(),
                         (json.dumps(legacy_view, indent=2) + "\n").encode())
        self.assertEqual(before, self.event_bytes())

    def test_confluence_v2_still_refuses_an_ack_in_the_event_plane(self) -> None:
        result = self.send()
        messages, delivery = self.events.present("recipient", now=NOW)
        self.assertEqual([result["message"]["id"]], [message["id"] for message in messages])
        self.assertIsNotNone(delivery)
        acknowledgment = SparseCursor(self.root).ack(
            "recipient", [result["message"]["id"]],
            acting_session_id="f2-readiness-reader", now=NOW,
        )
        ConfluenceGrantLedger(self.root).grant("consumer", "f2-illegal-plane-grant")
        # The acknowledgment is native and schema-valid, but illegal here.
        with self.events.path.open("ab") as handle:
            handle.write(encode_frame(acknowledgment))
        output = self.base / "refused-bundle.json"
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            materialize_bundle(self.root, consumer="consumer", out=output, version=2)
        self.assertFalse(output.exists())


class NativeHandoffCaptureTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_native_handoff_capture_drains_its_frozen_observation(self):
        fixture = ReadinessJournalTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        script = REPOSITORY_ROOT / "scripts/capture-mk-d1.py"
        spec = importlib.util.spec_from_file_location("f2_native_handoff_capture", script)
        capture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(capture)
        root = FloatiRoot.open_direct_home(fixture.base / "capture-fleet", create=True)
        send_text, inbox_text = capture._seed_handoff(root, NOW)
        sent = json.loads(send_text)["evidence"]
        inbox = json.loads(inbox_text)["evidence"]
        self.assertEqual("unknown", sent["recipient_readiness"]["state"])
        self.assertEqual([sent["message"]["id"]], inbox["acknowledgment"]["item_ids"])
        observations = [row for row in EventLog(root).event_records()
                        if row["kind"] == OBSERVATION_KIND]
        self.assertEqual(1, len(observations))
        self.assertEqual(sent["recipient_readiness"], observations[0]["readiness"])


if __name__ == "__main__":
    unittest.main()
