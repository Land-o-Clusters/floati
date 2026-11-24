"""Selected ACK writes preserve the complete native delivery/receipt contract."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati import cursor_prefix, jsonl, send_prefix
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.records import WAKE_HOLD_KINDS
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_test_profile import stable_fixture_sources
from tests.bus_scale_prefix_fixture import shared_prefix_baseline


class CursorPrefixTests(StableSourceFixture, unittest.TestCase):
    @stable_fixture_sources()
    @shared_prefix_baseline()
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("sender", "recipient", "other"):
            Registry(self.root).register(node, "codex")
        self.log = EventLog(self.root)
        self.cursor = SparseCursor(self.root)
        self.messages = [self.send("message-" + str(number)) for number in range(3)]
        _shown, self.delivery = self.log.present("recipient")
        self.ack_path = self.cursor.path_for("recipient")
        self.delivery_path = self.cursor.delivery_path_for("recipient")

    def send(self, key, *, recipient="recipient", worker_session_id=None):
        return self.log.send("sender", recipient, "floati", "a" * 40, "note.md", key,
            idempotency_key=key, worker_session_id=worker_session_id)

    def ack(self, items=None, session="acting"):
        if items is None:
            items = [self.messages[0]["id"]]
        return self.cursor.ack("recipient", items, acting_session_id=session)

    def prefixes(self):
        _events, _retracted, proof = send_prefix.event_references(self.root, [])
        return (cursor_prefix.DeliveryPrefix(self.root,
                    self.cursor._delivery_relative_path_for("recipient"), "recipient", None, proof),
                cursor_prefix.AckPrefix(self.root, self.cursor._relative_path_for("recipient")))

    def forbid_history_read(self):
        native = jsonl._read_path_records
        selected = {self.root.resolve_relative("events.jsonl"), self.delivery_path, self.ack_path}
        def read(path, *args, **kwargs):
            if path in selected:
                self.fail("fresh ACK replayed its already certified authoritative history: " + path.name)
            return native(path, *args, **kwargs)
        return patch.object(jsonl, "_read_path_records", side_effect=read)

    def test_fresh_cursor_appends_and_retries_without_replaying_certified_histories(self):
        first = self.ack()
        for number in range(16):
            self.ack(session="historical-" + str(number))
        with self.forbid_history_read():
            self.ack([self.messages[1]["id"]], "new")
            self.assertEqual(first, self.ack())

    def test_native_send_present_ack_retains_certified_joins_across_appends(self):
        # Native presentation's selection projection is outside this ACK cure.
        # Observe its actual append boundary, then the actual ACK, so a real
        # send/present cannot secretly make either writer replay old histories.
        self.ack([row["id"] for row in self.messages])
        delivery, acknowledgment = self.prefixes()
        delivery.load()
        acknowledgment.load()
        delivery_generation = delivery.payload["generation"]
        ack_generation = acknowledgment.payload["generation"]
        event_generation = json.loads((self.root.tenant_home / ".send-prefix/checkpoint.json").read_bytes())["payload"]["generation"]
        native = cursor_prefix.transact_delivery
        def observed(*args, **kwargs):
            with self.forbid_history_read():
                return native(*args, **kwargs)
        for number in range(3):
            with self.forbid_history_read():
                sent = self.send("native-pipeline-" + str(number))
            with patch.object(cursor_prefix, "transact_delivery", side_effect=observed):
                shown, receipt = self.log.present("recipient")
            self.assertEqual([sent["id"]], [row["id"] for row in shown])
            self.assertEqual([sent["id"]], receipt["item_ids"])
            with self.forbid_history_read():
                self.ack([sent["id"]], "native-pipeline-" + str(number))
            delivery, acknowledgment = self.prefixes()
            delivery.load()
            acknowledgment.load()
            self.assertEqual(delivery_generation, delivery.payload["generation"])
            self.assertEqual(ack_generation, acknowledgment.payload["generation"])
            self.assertEqual(event_generation,
                json.loads((self.root.tenant_home / ".send-prefix/checkpoint.json").read_bytes())["payload"]["generation"])

    def test_identical_event_replacement_revokes_prior_delivery_generation(self):
        first = self.ack()
        delivery, _ack = self.prefixes()
        delivery.load()
        old_delivery = delivery.payload["generation"]
        old_events = delivery.binding["events"]["generation"]
        replacement = self.log.path.with_suffix(".replacement")
        replacement.write_bytes(self.log.path.read_bytes())
        replacement.replace(self.log.path)
        self.assertEqual(first, self.ack())
        delivery, _ack = self.prefixes()
        delivery.load()
        self.assertNotEqual(old_delivery, delivery.payload["generation"])
        self.assertNotEqual(old_events, delivery.binding["events"]["generation"])

    def test_native_retraction_cannot_reuse_cached_ack_admission(self):
        worker = "retraction-worker"
        message = self.send("retract-cached", worker_session_id=worker)
        self.log.present("recipient", worker_session_id=worker)
        self.cursor.ack("recipient", [message["id"]], acting_session_id="worker-acting", worker_session_id=worker)
        path = self.cursor.path_for("recipient", worker_session_id=worker)
        before = path.read_bytes()
        self.log.retract(message["id"], worker_session_id=worker, reason="sent_in_error", author="sender")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.cursor.ack("recipient", [message["id"]], acting_session_id="worker-acting", worker_session_id=worker)
        self.assertEqual("ack_item_retracted", caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_cache_absence_and_missing_proof_nodes_recover_without_duplicate_ack(self):
        first = self.ack()
        for fault in ("checkpoint", "nodes"):
            for prefix in self.prefixes():
                with self.subTest(fault=fault, cache=prefix.home.name):
                    if fault == "checkpoint":
                        (prefix.home / "checkpoint.json").unlink()
                    else:
                        for path in prefix.nodes.iterdir():
                            path.unlink()
                    before = self.ack_path.read_bytes()
                    self.assertEqual(first, self.ack())
                    self.assertEqual(before, self.ack_path.read_bytes())

    def test_unrelated_retry_deleted_during_publication_cannot_become_absent(self):
        first = self.ack()
        original = cursor_prefix.AckPrefix._advance
        interleaved = []
        def advance(prefix, batch, encoded):
            key = send_prefix._key("retry", cursor_prefix._retry_key("acting", first["item_ids"]))
            victim = prefix.nodes / prefix.filename(key)
            victim.unlink()
            interleaved.append(key)
            return original(prefix, batch, encoded)
        # The second authoritative ACK is already durable when cache
        # publication begins. Cache loss cannot authorize a third receipt for
        # the old key, even when the publisher subsequently writes new keys.
        with patch.object(cursor_prefix.AckPrefix, "_advance", new=advance):
            self.ack([self.messages[1]["id"]], "second")
        self.assertEqual(1, len(interleaved))
        before = self.ack_path.read_bytes()
        self.assertEqual(first, self.ack())
        self.assertEqual(before, self.ack_path.read_bytes())

    def test_exact_retry_preserves_order_session_and_last_matching_receipt(self):
        items = [row["id"] for row in self.messages[:2]]
        first = self.ack(items)
        ordered = self.ack(list(reversed(items)))
        other_session = self.ack(items, "other-acting")
        latest = dict(first, id="ack-" + uuid7_hex())
        jsonl.append_record(self.root, self.cursor._relative_path_for("recipient"), latest,
            allowed_kinds=cursor_prefix.ACK_KINDS)
        self.assertEqual(latest, self.ack(items))
        with self.forbid_history_read():
            self.assertEqual(latest, self.ack(items))
            self.assertEqual(ordered, self.ack(list(reversed(items))))
            self.assertEqual(other_session, self.ack(items, "other-acting"))

    def test_unrequested_historical_delivery_reference_is_still_validated(self):
        self.ack()
        foreign = self.send("foreign", recipient="other")
        bad = dict(self.delivery, id="delivery-" + uuid7_hex(), item_ids=[foreign["id"]])
        jsonl.append_record(self.root, self.cursor._delivery_relative_path_for("recipient"), bad,
            allowed_kinds=WAKE_HOLD_KINDS)
        before = self.ack_path.read_bytes()
        with self.assertRaises(IntegrityFailure) as caught:
            self.ack()
        self.assertEqual("delivery_evidence_invalid", caught.exception.code)
        self.assertEqual(before, self.ack_path.read_bytes())

    def test_unrequested_historical_wrong_worker_plane_refuses(self):
        self.ack()
        foreign = self.send("wrong-worker", worker_session_id="private-worker")
        bad = dict(self.delivery, id="delivery-" + uuid7_hex(), item_ids=[foreign["id"]])
        jsonl.append_record(self.root, self.cursor._delivery_relative_path_for("recipient"), bad,
            allowed_kinds=WAKE_HOLD_KINDS)
        with self.assertRaises(IntegrityFailure) as caught:
            self.ack()
        self.assertEqual("delivery_evidence_invalid", caught.exception.code)

    def test_cached_delivery_cannot_hide_truncated_event_history(self):
        self.ack()
        before = self.ack_path.read_bytes()
        self.log.path.write_bytes(b"")
        with self.assertRaises(IntegrityFailure) as caught:
            self.ack()
        self.assertEqual("delivery_evidence_invalid", caught.exception.code)
        self.assertEqual(before, self.ack_path.read_bytes())

    def test_malformed_prior_ack_refuses_before_new_receipt(self):
        self.ack()
        self.ack_path.write_bytes(self.ack_path.read_bytes()[:-1])
        before = self.ack_path.read_bytes()
        with self.assertRaises(IntegrityFailure) as native:
            jsonl.read_records_snapshot(self.root, self.cursor._relative_path_for("recipient"),
                allowed_kinds=cursor_prefix.ACK_KINDS)
        with self.assertRaises(IntegrityFailure) as indexed:
            self.ack([self.messages[1]["id"]])
        self.assertEqual(native.exception.code, indexed.exception.code)
        self.assertEqual(before, self.ack_path.read_bytes())

    def test_forged_checkpoint_cannot_hide_changed_delivery_history(self):
        self.ack()
        delivery, _ack = self.prefixes()
        self.delivery_path.write_bytes(self.delivery_path.read_bytes()[:-1])
        checkpoint = delivery.home / "checkpoint.json"
        value = json.loads(checkpoint.read_text())
        value["payload"]["identity"] = send_prefix._identity(self.delivery_path)
        value["payload"]["offset"] = self.delivery_path.stat().st_size
        checkpoint.write_text(json.dumps(value))
        before = self.ack_path.read_bytes()
        with self.assertRaises(IntegrityFailure):
            self.ack()
        self.assertEqual(before, self.ack_path.read_bytes())

    def test_warm_certifies_without_changing_any_authoritative_ledger(self):
        paths = (self.log.path, self.delivery_path)
        before = {path: path.read_bytes() for path in paths}
        proof = cursor_prefix.warm(self.root, "recipient")
        self.assertIsNotNone(proof)
        self.assertFalse(self.ack_path.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        with self.forbid_history_read():
            self.ack()

    def test_duplicate_ack_id_and_record_limit_remain_native_refusals(self):
        first = self.ack()
        before = self.ack_path.read_bytes()
        with patch("floati.cursor.uuid7_hex", return_value=first["id"].removeprefix("ack-")):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.ack([self.messages[1]["id"]])
        self.assertEqual("duplicate_record_id", caught.exception.code)
        self.assertEqual(before, self.ack_path.read_bytes())
        # The patched limit applies to every source ledger used by complete
        # certification, not only ACKs. Keep all sources valid at the boundary
        # and fill this ledger with genuine distinct-session native receipts.
        limit = len(self.log.event_records())
        for number in range(1, limit):
            self.ack(session="limit-history-" + str(number))
        self.assertEqual(limit, len(jsonl.read_records_snapshot(self.root,
            self.cursor._relative_path_for("recipient"), allowed_kinds=cursor_prefix.ACK_KINDS)))
        before = self.ack_path.read_bytes()
        candidate = copy.deepcopy(first)
        candidate["id"] = "ack-" + uuid7_hex()
        candidate["acting_session_id"] = "past-limit"
        with patch.object(jsonl, "MAX_LEDGER_RECORDS", limit):
            with self.assertRaises(ProtocolRefusal) as caught:
                cursor_prefix.transact_ack(self.root, self.cursor._relative_path_for("recipient"),
                    candidate["item_ids"], candidate["acting_session_id"], lambda _prior: (candidate, candidate))
        self.assertEqual("ledger_record_limit", caught.exception.code)
        self.assertEqual(before, self.ack_path.read_bytes())

    def test_selected_transaction_cannot_append_a_different_retry_or_plane(self):
        first = self.ack()
        before = self.ack_path.read_bytes()
        items = [self.messages[1]["id"]]
        for label in ("items", "session", "recipient", "worker-plane", "existing-retry"):
            with self.subTest(label=label):
                candidate = dict(first, id="ack-" + uuid7_hex(), item_ids=items,
                    acting_session_id="new-acting")
                coordinate = self.cursor._relative_path_for("recipient")
                selected_items, selected_session = items, "new-acting"
                worker = None
                if label == "items":
                    candidate["item_ids"] = [self.messages[2]["id"]]
                elif label == "session":
                    candidate["acting_session_id"] = "another-acting"
                elif label == "recipient":
                    candidate["recipient"] = "other"
                elif label == "worker-plane":
                    worker = "another-worker"
                else:
                    candidate["item_ids"] = first["item_ids"]
                    candidate["acting_session_id"] = first["acting_session_id"]
                    selected_items, selected_session = first["item_ids"], first["acting_session_id"]
                with self.assertRaises(ProtocolRefusal):
                    cursor_prefix.transact_ack(self.root, coordinate, selected_items, selected_session,
                        lambda _prior: (candidate, candidate), worker_session_id=worker)
                self.assertEqual(before, self.ack_path.read_bytes())

    def test_closed_delivery_append_preserves_count_and_rejects_other_contracts(self):
        self.ack()
        before = self.delivery_path.read_bytes()
        items = [self.messages[0]["id"]]
        for label in ("items", "recipient", "count", "held-kind"):
            with self.subTest(label=label):
                def decide(count):
                    candidate = dict(self.delivery, id="delivery-" + uuid7_hex(),
                        item_ids=items, presentation_count=count + 1)
                    if label == "items":
                        candidate["item_ids"] = [self.messages[1]["id"]]
                    elif label == "recipient":
                        candidate["recipient"] = "other"
                    elif label == "count":
                        candidate["presentation_count"] = count + 2
                    else:
                        candidate["kind"] = "wake_hold_receipt"
                    return candidate, candidate
                with self.assertRaises(ProtocolRefusal):
                    cursor_prefix.transact_delivery(self.root, "recipient", items, decide)
                self.assertEqual(before, self.delivery_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
