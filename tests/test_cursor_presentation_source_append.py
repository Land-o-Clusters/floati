"""Real native source appends retain full-read and replay refusal guarantees."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from floati.bus_epoch import epoch_guard
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.wake_hold import WakeHoldController, wake_coordination_guard
from tests import test_codex_wait as baseline


class CursorPresentationSourceAppendTests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.session = "source-append-session"
        self.f.arm_session(self.session)
        self.root, self.node = self.f.root, self.f.participant.binding.node_id
        self.plane = CursorPresentationLedger(self.root)
        self.events = self.root.tenant_home / "events.jsonl"

    def send(self, key):
        receipt = self.f.send(key)
        return receipt.get("message", receipt)

    def source(self, plane=None):
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            return (self.plane if plane is None else plane)._sources(self.root.tenant_home, self.node)

    @contextmanager
    def selected(self):
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            binding = self.f.participant.binding
            consent = CodexWaitConsentLedger(self.root).require_armed(binding)
            classification = CodexWaitSessionLedger(self.root)._participate_already_guarded(
                binding, consent, self.session, classified=True)
            yield self.plane.select(binding, consent, classification, harness="codex")

    def append(self, *rows):
        with self.events.open("ab") as output:
            for row in rows:
                output.write(encode_frame(row))

    def future(self, record_id=None):
        return {"schema_version": 2, "id": record_id or "future-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": "2026-10-11T12:00:00.000Z",
            "kind": "future_source_append_receipt", "payload": {"future": True}}

    def assert_replay_refusal(self, code, detail=None):
        failures = []
        for plane in (self.plane, CursorPresentationLedger(self.root)):
            with self.assertRaises((IntegrityFailure, ProtocolRefusal)) as caught:
                self.source(plane)
            failure = caught.exception
            self.assertEqual(code, failure.code)
            if detail is not None:
                self.assertIn(detail, failure.detail)
            failures.append((type(failure), failure.code, failure.detail, getattr(failure, "remedy", None)))
        self.assertEqual(failures[0], failures[1], "warm refusal must equal full native replay")

    def test_native_append_decodes_suffix_but_still_protects_and_reads_all_bytes(self):
        # Re-decoding the admitted prefix wastes the work this patch removes;
        # skipping the protected full read would miss middle rewrites.
        import floati.cursor_presentations as presentation
        real_decode = presentation._decode_path_records
        decoded, observed = [], []
        real_bytes = self.plane._bytes

        def inspect_decode_path_records(path, tenant, kinds, data, **kwargs):
            if path == self.events:
                decoded.append(bytes(data))
            return real_decode(path, tenant, kinds, data, **kwargs)

        def protected_bytes(relative, *args, **kwargs):
            data = real_bytes(relative, *args, **kwargs)
            if relative == Path("events.jsonl"):
                observed.append(data)
            return data

        with mock.patch.object(presentation, "_decode_path_records", inspect_decode_path_records),\
                mock.patch.object(self.plane, "_bytes", protected_bytes):
            first = self.send("source-native-first")
            initial_bytes = self.events.read_bytes()
            original = self.source()
            second = self.send("source-native-second")
            combined_bytes = self.events.read_bytes()
            combined = self.source()
        self.assertEqual([initial_bytes, combined_bytes[len(initial_bytes):]], decoded)
        self.assertEqual([initial_bytes, combined_bytes], observed)
        self.assertEqual([first["id"], second["id"]],
            [row["id"] for row in combined[0] if row["kind"] == "message_envelope"])
        self.assertEqual(original[0], combined[0][:len(original[0])])
        self.assertEqual(original[1], combined[1][:len(original[1])])
        digest = hashlib.sha256(WakeHoldController._EVENT_DOMAIN.encode("ascii") + b"\0")
        expected = [digest.hexdigest()]
        for frame in combined_bytes.splitlines(keepends=True):
            digest.update(frame)
            expected.append(digest.hexdigest())
        self.assertEqual(tuple(expected), combined[1])

    def test_middle_same_size_rewrite_plus_append_cannot_reuse_old_rows(self):
        self.send("source-middle-first")
        self.send("source-middle-second")
        before = self.events.read_bytes()
        self.source()
        self.send("source-middle-third")
        after = self.events.read_bytes()
        changed_prefix = before.replace(b"live ping", b"live pong", 1)
        self.assertEqual(len(before), len(changed_prefix))
        self.events.write_bytes(changed_prefix + after[len(before):])
        source = self.source()
        self.assertEqual("live pong", source[0][0]["note"])
        self.assertEqual(source, self.source(CursorPresentationLedger(self.root)))

    def test_changed_cut_does_not_extend_rows_mutated_through_returned_source(self):
        self.send("source-alias-first")
        exposed = self.source()
        exposed[0][0]["note"] = "caller mutation"
        self.assertEqual("caller mutation", self.source()[0][0]["note"],
            "unchanged exact-byte hits retain baseline alias behavior")
        exposed[0][1]["readiness"]["observed_at"] = "2000-01-01T00:00:00.000Z"
        self.send("source-alias-second")
        changed = self.source()
        self.assertEqual(self.source(CursorPresentationLedger(self.root)), changed)
        self.assertEqual("live ping", changed[0][0]["note"])
        # Rows returned by the append path must be detached as well.
        changed[0][0]["note"] = "second caller mutation"
        self.send("source-alias-third")
        self.assertEqual(self.source(CursorPresentationLedger(self.root)), self.source())

    def test_changed_delivery_cut_does_not_extend_exposed_nested_item_ids(self):
        first = self.send("source-delivery-alias-first")
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            EventLog(self.root)._write_delivery_receipt(self.node, [first])
        exposed = self.source()
        exposed[2][0]["item_ids"][0] = "msg-" + uuid7_hex()
        second = self.send("source-delivery-alias-second")
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            EventLog(self.root)._write_delivery_receipt(self.node, [second])
        changed = self.source()
        self.assertEqual(self.source(CursorPresentationLedger(self.root)), changed)
        self.assertEqual([first["id"]], changed[2][0]["item_ids"])

    def test_selection_source_alias_cannot_change_rows_after_native_append(self):
        first = self.send("source-selection-alias-first")
        with self.selected() as selection:
            selection["_source"][0][0]["note"] = "selection caller mutation"
        second = self.send("source-selection-alias-second")
        with self.selected() as selection:
            self.assertEqual([first["id"], second["id"]],
                [row["id"] for row in selection["fresh_messages"]])
            self.assertEqual(["live ping", "live ping"],
                [row["note"] for row in selection["fresh_messages"]])
            changed = selection["_source"]
        self.assertEqual(self.source(CursorPresentationLedger(self.root)), changed)

    def test_cross_prefix_duplicate_known_id_is_not_an_admissible_append(self):
        message = self.send("source-duplicate-known")
        self.source()
        self.append(message)
        self.assert_replay_refusal("duplicate_record_id", message["id"])

    def test_unknown_prefix_id_still_participates_in_cross_prefix_duplicates(self):
        self.send("source-duplicate-future-prefix")
        unknown = self.future()
        self.append(unknown)
        self.source()
        self.append(unknown)
        self.assert_replay_refusal("duplicate_record_id", unknown["id"])

    def test_unknown_suffix_id_cannot_duplicate_a_known_prefix_id(self):
        message = self.send("source-duplicate-future-suffix")
        self.source()
        self.append(self.future(message["id"]))
        self.assert_replay_refusal("duplicate_record_id", message["id"])

    def test_unknown_physical_frame_before_late_roll_preserves_absolute_position(self):
        self.append(self.future())
        self.source()
        self.append({"kind": "bus_epoch_roll_receipt"})
        self.assert_replay_refusal("bus_epoch_roll_receipt_position_invalid", "physical record one")

    def test_unknown_prefix_exact_hit_preserves_admission_but_append_replays_provenance(self):
        self.send("source-provenance")
        self.append(self.future())
        with mock.patch("floati.installed_reader.installed_reader_identity", return_value=None):
            admitted = self.source()
        identity = {"source_sha": "a" * 40, "source_checkout": "/fixture/source",
            "install_root": "/fixture/install"}
        with mock.patch("floati.installed_reader.installed_reader_identity", return_value=identity):
            self.assertEqual(admitted, self.source(), "unchanged byte hit preserves the existing admission")
            self.append(self.future())
            self.assert_replay_refusal("reader_older_than_ledger", "future_source_append_receipt")

    def test_unknown_suffix_preserves_malformed_provenance_refusal(self):
        self.send("source-malformed-provenance")
        self.source()
        self.append(self.future())
        failure = ProtocolRefusal("installed_reader_identity_invalid", "controlled malformed provenance",
            remedy="reinstall this reader from its governed source and retry")
        with mock.patch("floati.installed_reader.installed_reader_identity", side_effect=failure):
            self.assert_replay_refusal("installed_reader_identity_invalid", "controlled malformed provenance")

    def test_node_named_events_does_not_make_delivery_history_skip_unknown_kinds(self):
        self.send("source-events-node")
        Registry(self.root).register("events", "codex")
        path = self.root.tenant_home / "receipts/deliveries/events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_frame(self.future()))
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, "events"):
            with self.assertRaises(IntegrityFailure) as caught:
                self.plane._sources(self.root.tenant_home, "events")
        self.assertEqual("record_kind_invalid", caught.exception.code)

    def test_malformed_suffix_uses_full_replay_absolute_line_and_error_precedence(self):
        self.send("source-malformed-suffix")
        original = self.events.read_bytes()
        self.source()
        for suffix, code, detail in (
            (b"not-json\n", "malformed_json", "line 3"),
            (b"\n", "blank_jsonl_line", "line 3"),
            (b"{", "incomplete_jsonl_line", "incomplete final record"),
            (b'{"id":"x","id":"y"}\n', "duplicate_json_key", "line 3"),
            (b'{"oversized":"' + b"x" * 65536 + b'"}\n', "record_too_large", "line 3"),
            (encode_frame({"kind": "bus_epoch_roll_receipt"}),
                "bus_epoch_roll_receipt_position_invalid", "physical record one"),
        ):
            with self.subTest(code=code):
                self.events.write_bytes(original + suffix)
                self.assert_replay_refusal(code, detail)
        self.events.write_bytes(original)

    def test_noncanonical_suffix_does_not_enter_digest_history(self):
        message = self.send("source-noncanonical")
        self.source()
        message = dict(message, id="msg-" + uuid7_hex(), idempotency_key="noncanonical-tail")
        with self.events.open("ab") as output:
            output.write((json.dumps(message, sort_keys=True) + "\n").encode())
        self.assert_replay_refusal("presentation_history_unavailable", "native canonical framing")

    def test_combined_physical_count_is_not_replaced_by_suffix_count(self):
        message = self.send("source-combined-record-cap")
        observation = decode_frames(self.events.read_bytes())[1]
        with mock.patch("floati.jsonl.MAX_LEDGER_RECORDS", 3):
            self.source()
            tail = dict(message, id="msg-" + uuid7_hex(), idempotency_key="combined-cap-tail")
            self.append(tail, dict(observation, id="send-readiness-" + uuid7_hex(),
                envelope_id=tail["id"], idempotency_key=tail["idempotency_key"]))
            self.assert_replay_refusal("ledger_record_limit", "exceeds 3 records")

    def test_changed_contract_record_cap_revalidates_an_unchanged_cut(self):
        self.send("source-record-cap")
        self.source()
        with mock.patch("floati.jsonl.MAX_LEDGER_RECORDS", 1):
            self.assert_replay_refusal("ledger_record_limit", "exceeds 1 records")

    def test_changed_digest_domain_rebuilds_prefixes_for_unchanged_bytes(self):
        self.send("source-domain-change")
        old = self.source()
        with mock.patch.object(WakeHoldController, "_EVENT_DOMAIN", "source-append-new-domain"):
            changed = self.source()
        self.assertEqual(old[0], changed[0])
        self.assertNotEqual(old[1], changed[1])
        digest = hashlib.sha256(b"source-append-new-domain\0")
        self.assertEqual(digest.hexdigest(), changed[1][0])

    def test_changed_schema_validator_rechecks_an_unchanged_cut(self):
        import floati.jsonl as jsonl
        message = self.send("source-validator-change")
        self.source()
        original = jsonl.validate_record

        def changed_validator(record, *args, **kwargs):
            if record.get("id") == message["id"]:
                raise IntegrityFailure("record_fields_invalid", "controlled changed decoder contract")
            return original(record, *args, **kwargs)

        with mock.patch.object(jsonl, "validate_record", changed_validator):
            self.assert_replay_refusal("record_fields_invalid", "controlled changed decoder contract")

    def test_changed_bounded_framing_kernel_rechecks_an_unchanged_cut(self):
        import floati.jsonl as jsonl
        self.send("source-framing-kernel-change")
        self.source()
        original = jsonl._decode_bounded_frames

        def changed_kernel(path, data, **kwargs):
            if path == self.events:
                raise IntegrityFailure("record_too_large", "controlled changed framing kernel")
            return original(path, data, **kwargs)

        with mock.patch.object(jsonl, "_decode_bounded_frames", changed_kernel):
            self.assert_replay_refusal("record_too_large", "controlled changed framing kernel")

    def test_combined_byte_cap_is_not_replaced_by_suffix_size(self):
        message = self.send("source-byte-cap")
        prefix = self.events.read_bytes()
        # Prime under the same smaller contract that the combined append will
        # exceed; the suffix alone still fits that contract.
        with mock.patch("floati.jsonl.MAX_LEDGER_BYTES", len(prefix) + 1):
            self.source()
            self.append(dict(message, id="msg-" + uuid7_hex(), idempotency_key="byte-cap-tail"))
            self.assert_replay_refusal("ledger_too_large", "exceeds")

    def test_shrink_and_missing_source_discard_old_prefix_before_regrowth(self):
        self.send("source-shrink-one")
        first = self.events.read_bytes()
        self.send("source-shrink-two")
        self.source()
        self.events.write_bytes(first)
        self.assertEqual(2, len(self.source()[0]))
        self.events.unlink()
        self.assertEqual([], self.source()[0])
        self.events.write_bytes(first)
        self.assertEqual(2, len(self.source()[0]))

    def test_invalid_reply_dependency_is_validated_against_whole_merged_events(self):
        message = self.send("source-reply")
        self.source()
        self.append(dict(message, id="msg-" + uuid7_hex(), idempotency_key="reply-tail",
            reply_to=message["id"]))
        self.assert_replay_refusal("message_order_invalid", "reverse the original message parties")

    def test_valid_reply_and_readiness_can_reference_an_admitted_prefix(self):
        message = self.send("source-valid-cross-prefix")
        observation = decode_frames(self.events.read_bytes())[1]
        self.source()
        reply = dict(message, id="msg-" + uuid7_hex(), idempotency_key="valid-reply-tail",
            sender=message["recipient"], recipient=message["sender"], reply_to=message["id"])
        self.append(reply)
        self.assertEqual([message["id"], reply["id"]],
            [row["id"] for row in self.source()[0] if row["kind"] == "message_envelope"])
        readiness = dict(observation, id="send-readiness-" + uuid7_hex(),
            envelope_id=reply["id"], sender=reply["sender"], recipient=reply["recipient"],
            idempotency_key=reply["idempotency_key"])
        self.append(readiness)
        self.assertEqual(4, len(self.source()[0]))

    def test_invalid_readiness_dependency_is_not_validated_in_isolated_suffix(self):
        message = self.send("source-readiness")
        observation = decode_frames(self.events.read_bytes())[1]
        self.source()
        tail = dict(message, id="msg-" + uuid7_hex(), idempotency_key="readiness-tail")
        bad = dict(observation, id="send-readiness-" + uuid7_hex(), envelope_id=tail["id"],
            idempotency_key=tail["idempotency_key"], sender=self.node)
        self.append(tail, bad)
        self.assert_replay_refusal("send_readiness_binding_invalid", "parties and idempotency key")

    def test_retraction_dependency_crossing_cached_prefix_remains_global(self):
        worker_session = "source-retraction-worker"
        receipt = EventLog(self.root).send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/ping.md", "worker routed source", idempotency_key="source-retraction",
            worker_session_id=worker_session)
        message = receipt.get("message", receipt)
        self.source()
        retraction = EventLog(self.root).retract(message["id"], worker_session_id=worker_session,
            reason="sent_in_error", author="architect")
        self.source()
        self.append(dict(retraction, id="ret-" + uuid7_hex()))
        self.assert_replay_refusal("message_retraction_duplicate", "one append-only retraction")

    def test_other_seat_mail_preserves_selection_but_prepare_uses_latest_event_digest(self):
        first = self.send("source-other-seat-held-selection")
        with self.selected() as selection:
            previous_digest = selection["_source"][1][-1]
            self.assertEqual([first["id"]], [row["id"] for row in selection["fresh_messages"]])
        Registry(self.root).register("source-other-seat", "codex")
        EventLog(self.root).send("architect", "source-other-seat", "floati", "a" * 40,
            "docs/evidence/ping.md", "other seat append", idempotency_key="source-other-seat")
        with self.selected() as selection:
            self.assertEqual([first["id"]], [row["id"] for row in selection["fresh_messages"]])
            self.assertNotEqual(previous_digest, selection["_source"][1][-1])
            prepared = self.plane.prepare(selection, {"decision": "block", "reason": "native fixture"})
            self.assertNotEqual(previous_digest, prepared["event_prefix_digest"])
            self.assertEqual(selection["_source"][1][-1], prepared["event_prefix_digest"])

    def test_relevant_append_delivery_and_ack_change_selection_immediately(self):
        first = self.send("source-state-first")
        with self.selected() as selection:
            self.assertEqual([first["id"]], [row["id"] for row in selection["fresh_messages"]])
        second = self.send("source-state-second")
        with self.selected() as selection:
            self.assertEqual([first["id"], second["id"]],
                [row["id"] for row in selection["fresh_messages"]])
            prepared = self.plane.prepare(selection, {"decision": "block", "reason": "native fixture"})
            delivery = self.plane.deliver(prepared, selection)
            self.plane.flushed(prepared, delivery)
        with self.selected() as selection:
            self.assertEqual([], selection["fresh_messages"])
            self.assertEqual([first["id"], second["id"]],
                [row["item_id"] for row in selection["held_items"]])
        SparseCursor(self.root).ack(self.node, [first["id"], second["id"]], acting_session_id=self.session)
        with self.selected() as selection:
            self.assertEqual([], selection["fresh_messages"])
            self.assertEqual([], selection["held_items"])

    def test_worker_route_and_retraction_remain_excluded_from_ordinary_selection(self):
        ordinary = self.send("source-ordinary-alongside-worker")
        with self.selected() as selection:
            self.assertEqual([ordinary["id"]], [row["id"] for row in selection["fresh_messages"]])
        worker_session = "source-selection-worker"
        receipt = EventLog(self.root).send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/ping.md", "worker routed selection", idempotency_key="source-worker-selection",
            worker_session_id=worker_session)
        worker = receipt.get("message", receipt)
        with self.selected() as selection:
            self.assertEqual([ordinary["id"]], [row["id"] for row in selection["fresh_messages"]])
        EventLog(self.root).retract(worker["id"], worker_session_id=worker_session,
            reason="sent_in_error", author="architect")
        with self.selected() as selection:
            self.assertEqual([ordinary["id"]], [row["id"] for row in selection["fresh_messages"]])
