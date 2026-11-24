"""M2 archive-only Cursor presence; native oracles precede new-API RED.

Missing API failures cannot establish later interception/budget branches. Those
branches require the separately watched post-API run. Adversarial source-removal
roots use retained native epochs; no fixture issues an orphaned generation.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest import mock

from floati import bus_epoch, cursor
from floati import epoch_history, history_query_join, jsonl, snapshot
from floati import archive_survivor_sources as query_work
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS, EventLog
from floati.framing import decode_frames, encode_frame
from floati.records import WAKE_HOLD_KINDS
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.stop_hold import StopHoldLedger
from floati.wake_hold import WakeHoldController
from tests import test_epoch_carry as native_fixture


def changed_cursor_membership_code(*args, **kwargs):
    raise AssertionError("substituted cursor membership code executed")


class HistoryQueryCursorMembershipTests(unittest.TestCase):
    def setUp(self):
        self.native = native_fixture.EpochCarryTests()
        self.addCleanup(self.native.doCleanups)
        self.native.setUp()
        self.native.send("retained-history-original")
        bus_epoch.roll_bus_epoch(self.native.root, actor="sender", idempotency_key="retained-history-original-roll")
        self.fixture, self.root = self.native, self.native.root

    def api(self):
        result = getattr(epoch_history, "archived_item_membership", None)
        self.assertTrue(callable(result), "root-only archive envelope OR delivery membership is absent")
        return result

    def driver(self):
        result = getattr(history_query_join, "archived_item_membership", None)
        self.assertTrue(callable(result), "bounded captured archive membership driver is absent")
        return result

    def manual_roll(self, key, *, root=None):
        return bus_epoch.roll_bus_epoch(root or self.root, actor="sender",
            idempotency_key=key)["receipt"]

    def files(self, root=None):
        root = root or self.root
        return {path.relative_to(root.tenant_home).as_posix(): path.read_bytes()
                for path in root.tenant_home.rglob("*") if path.is_file()}

    def ack_files(self):
        return {name: raw for name, raw in self.files().items() if name.startswith("receipts/acks/")}

    def absent_id(self):
        foreign = FloatiRoot.open_direct_home(self.fixture.base / "foreign-cursor-membership", create=True)
        for node in ("sender", "seat-a"):
            Registry(foreign).register(node, "Codex")
        return EventLog(foreign).send("sender", "seat-a", "floati", "a" * 40,
            "docs/evidence/cursor-membership.md", "foreign native identity")["id"]

    def oracle(self, identity, node="seat-a", worker=None, *, root=None):
        """Independent strict legacy leaf, never the migrated private helper."""
        root = root or self.root
        relative = SparseCursor(root)._delivery_relative_path_for(node, worker_session_id=worker)
        with bus_epoch.epoch_guard(root):
            for archive in snapshot._owned_epoch_archives(root):
                base = archive.relative_to(root.tenant_home)
                events = jsonl.read_records_snapshot(root, base / "events.jsonl",
                    allowed_kinds=set(EVENT_KINDS))
                if any(row["kind"] == "message_envelope" and row["id"] == identity for row in events):
                    return True
                deliveries = jsonl.read_records_snapshot(root, base / relative,
                    allowed_kinds=set(WAKE_HOLD_KINDS))
                if any(identity in row["item_ids"] for row in deliveries):
                    return True
        return False

    def refusal(self, identity, code, *, node="seat-a", worker=None):
        before = self.ack_files()
        with self.assertRaises(ProtocolRefusal) as caught:
            SparseCursor(self.root).ack(node, [identity], acting_session_id="m2-acting",
                worker_session_id=worker)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(before, self.ack_files(), "refused membership wrote an ACK receipt")

    def remove_live_envelopes(self, identities):
        path = self.root.tenant_home / "events.jsonl"
        rows = decode_frames(path.read_bytes())
        self.assertTrue(set(identities).issubset({row["id"] for row in rows}))
        # Explicit adversarial SOURCE removal before a real roll; preserve its
        # existing first lineage anchor, all actual delivery/hold bytes and IDs.
        # Native readiness observations depend on their preceding envelope;
        # remove those dependent observations with the removed source only.
        path.write_bytes(b"".join(encode_frame(row) for row in rows
            if row["id"] not in identities and not (
                row["kind"] == "send_readiness_observation" and row["envelope_id"] in identities)))

    def future_frame(self):
        return {"schema_version": 99, "id": "m2-future-schema-kind",
            "tenant_id": self.root.tenant_id, "timestamp": "2026-10-20T20:00:00.000Z",
            "kind": "m2_future_schema_kind"}

    def plain_delivery(self):
        binding, consent, _classification, _prepared = self.fixture.presentation(flushed=True)
        ledger = StopHoldLedger(self.root)
        stamp = lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        start = ledger.start(binding, consent, "conversation-a", started_at=stamp(),
            deadline_seconds=consent["wait_deadline_seconds"])
        try:
            _messages, receipt = EventLog(self.root).present("seat-a")
            self.assertEqual("delivery_receipt", receipt["kind"],
                "plain delivery needs actual native presenter testimony")
            return receipt
        finally:
            ledger.close(start, timestamp=stamp())

    def test_archived_event_membership_and_current_ack_precedence(self):
        archived = self.fixture.send("m2-archived")
        self.manual_roll("m2-manual")
        unseen = self.absent_id()
        live = self.fixture.send("m2-live-no-delivery")
        self.assertTrue(self.oracle(archived["id"]))
        self.assertFalse(self.oracle(unseen))
        self.assertFalse(self.oracle(live["id"]))
        self.refusal(archived["id"], "ack_item_archived")
        self.refusal(unseen, "ack_item_unknown")
        self.refusal(live["id"], "ack_item_not_delivered")
        current = self.fixture.send("m2-current-delivered", node="seat-b")
        EventLog(self.root).present("seat-b")
        self.fixture.automatic_roll()
        self.assertTrue(self.oracle(current["id"], "seat-b"))
        self.assertTrue(self.oracle(live["id"]))
        self.assertIn(current["id"], [row["id"] for row in EventLog(self.root).records()])
        # Actual EPOCH+WAKE ACK path: archive presence cannot block current
        # delivery, while an undelivered LIVE copy still reaches archived.
        receipt = SparseCursor(self.root).ack("seat-b", [current["id"]], acting_session_id="m2-current")
        self.assertEqual([current["id"]], receipt["item_ids"])
        self.refusal(live["id"], "ack_item_archived")
        lookup = self.api()
        self.assertIs(True, lookup(self.root, live["id"], "seat-a"))
        self.assertIs(False, lookup(self.root, unseen, "seat-a"))
        original = SparseCursor._item_was_archived
        called = []
        def observed(instance, *args, **kwargs):
            called.append(True)
            return original(instance, *args, **kwargs)
        with mock.patch.object(SparseCursor, "_item_was_archived", observed):
            self.refusal(unseen, "ack_item_unknown")
        self.assertEqual([True], called)

    def test_delivery_only_membership_uses_exact_native_plane(self):
        ordinary = self.fixture.send("m2-plain-delivery")
        self.plain_delivery()
        worker = self.fixture.send("m2-unpresented", worker_session_id="m2-worker")
        _shown, unpresented = EventLog(self.root).present("seat-a", worker_session_id="m2-worker")
        self.assertEqual("delivered_unpresented", unpresented["kind"])
        held = self.fixture.send("m2-held", worker_session_id="m2-held-worker")
        held_result = WakeHoldController(self.root).evaluate("seat-a", worker_session_id="m2-held-worker",
            idempotency_key="m2-native-held")
        self.assertIn(held["id"], [row["id"] for row in held_result["fresh_messages"]])
        self.remove_live_envelopes({ordinary["id"], worker["id"], held["id"]})
        receipt = self.manual_roll("m2-source-removal-no-commit")
        context = epoch_history.open_history(self.root)
        self.assertIsNone(context.generation, "adversarial source removal is not an issuance fixture")
        selected = next(epoch for epoch in context.epochs if epoch.receipt["id"] == receipt["id"])
        cases = ((ordinary["id"], None, "delivery_receipt"),
                 (worker["id"], "m2-worker", "delivered_unpresented"),
                 (held["id"], "m2-held-worker", "wake_hold_receipt"))
        for identity, plane, kind in cases:
            source = context.source(selected.coordinate, "seat-a", plane)
            self.assertNotIn(identity, [row["id"] for row in source[0]])
            self.assertTrue(any(row["kind"] == kind and identity in row["item_ids"] for row in source[2]))
            self.assertTrue(self.oracle(identity, worker=plane))
            self.assertFalse(self.oracle(identity, "seat-b", plane))
            self.assertFalse(self.oracle(identity, worker="other-worker"))
            if plane is not None:
                self.assertFalse(self.oracle(identity))
        non_envelope = receipt["id"]
        self.assertFalse(self.oracle(non_envelope))
        lookup = self.api()
        for identity, plane, _kind in cases:
            self.assertIs(True, lookup(self.root, identity, "seat-a", plane))
            self.assertIs(False, lookup(self.root, identity, "seat-b", plane))
            self.assertIs(False, lookup(self.root, identity, "seat-a", "other-worker"))
            self.refusal(identity, "ack_item_archived", worker=plane)
        self.assertIs(False, lookup(self.root, non_envelope, "seat-a"))

    def test_admitted_legacy_lookup_preserves_visited_plane_order(self):
        event = self.fixture.send("m2-event-short-circuit")
        path = SparseCursor(self.root).delivery_path_for("seat-a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_frame(self.future_frame()))
        receipt = self.manual_roll("m2-invalid-unvisited-delivery")
        self.assertTrue(self.oracle(event["id"]))
        self.assertIsNone(epoch_history.open_history(self.root).generation)
        with self.assertRaises(IntegrityFailure):
            self.oracle("not-a-message-id")
        lookup = self.api()
        self.assertIs(True, lookup(self.root, event["id"], "seat-a"))
        with self.assertRaises(IntegrityFailure):
            lookup(self.root, "not-a-message-id", "seat-a")
        # A later no-commit delivery hit must precede older EVENT schema
        # validation, but admission still frames the full original lineage.
        events = self.root.tenant_home / "events.jsonl"
        events.write_bytes(events.read_bytes() + encode_frame(self.future_frame()))
        self.manual_roll("m2-invalid-older-event-schema")
        message = self.fixture.send("m2-newer-delivery-hit", worker_session_id="newer-worker")
        EventLog(self.root).present("seat-a", worker_session_id="newer-worker")
        self.remove_live_envelopes({message["id"]})
        self.manual_roll("m2-newer-delivery-no-commit")
        self.assertTrue(self.oracle(message["id"], worker="newer-worker"))
        self.assertIsNone(epoch_history.open_history(self.root).generation)
        self.assertIs(True, lookup(self.root, message["id"], "seat-a", "newer-worker"))
        # Changed owned facts are a whole-context failure before any hit;
        # preserve that stronger boundary rather than legacy partial success.
        archived = Path(receipt["archive_path"]) / "events.jsonl"
        original = archived.read_bytes()
        try:
            archived.write_bytes(original + b"{partial")
            with self.assertRaises(IntegrityFailure):
                epoch_history.open_history(self.root)
            with self.assertRaises(IntegrityFailure):
                lookup(self.root, message["id"], "seat-a", "newer-worker")
        finally:
            archived.write_bytes(original)

    def test_reached_refusal_leaf_requires_actual_committed_context(self):
        archived = self.fixture.send("m2-commit-archived")
        self.manual_roll("m2-commit-manual")
        live = self.fixture.send("m2-commit-current", node="seat-b")
        EventLog(self.root).present("seat-b")
        unseen = self.absent_id()
        self.assertTrue(self.oracle(archived["id"]))
        self.assertFalse(self.oracle(unseen))
        self.assertIsNone(epoch_history.open_history(self.root).generation)
        lookup = self.api()
        self.assertIs(True, lookup(self.root, archived["id"], "seat-a"))
        self.fixture.automatic_roll()
        self.assertIs(True, lookup(self.root, archived["id"], "seat-a"))
        undelivered = self.fixture.send("m2-commit-undelivered")
        worker_guard = self.fixture.send("m2-current-worker-guard", worker_session_id="guard-worker")
        retracted = self.fixture.send("m2-current-retraction-guard", worker_session_id="retraction-worker")
        EventLog(self.root).retract(retracted["id"], worker_session_id="retraction-worker",
            reason="sent_in_error", author="sender")
        targets = (self.root.tenant_home / "events.jsonl",
            snapshot._owned_epoch_archives(self.root)[0] / "events.jsonl")
        fallback = []
        def legacy(*args, **kwargs):
            fallback.append(True)
            raise AssertionError("malformed commitment fell back to Path membership")
        for target in targets:
            raw = target.read_bytes()
            try:
                target.write_bytes(raw + b"{partial")
                for identity in (archived["id"], unseen, undelivered["id"]):
                    with self.subTest(target=target.name, identity=identity):
                        before = self.ack_files()
                        with mock.patch.object(snapshot, "_owned_epoch_archives", legacy),\
                                self.assertRaises((IntegrityFailure, ProtocolRefusal)) as caught:
                            SparseCursor(self.root).ack("seat-a", [identity], acting_session_id="m2-invalid")
                        self.assertNotIn(caught.exception.code,
                            {"ack_item_archived", "ack_item_unknown", "ack_item_not_delivered"})
                        self.assertEqual(before, self.ack_files())
                def no_open(*args, **kwargs):
                    raise AssertionError("earlier current ACK branch opened archive history")
                with mock.patch.object(epoch_history, "open_history", no_open):
                    if target == self.root.tenant_home / "events.jsonl":
                        # Current-ID decisions still need an intact live ledger.
                        for identity, node, worker in ((live["id"], "seat-a", None),
                                (worker_guard["id"], "seat-a", None),
                                (retracted["id"], "seat-a", "retraction-worker"),
                                (live["id"], "seat-b", None)):
                            before = self.ack_files()
                            with self.subTest(current=identity, node=node),\
                                    self.assertRaises(IntegrityFailure) as caught:
                                SparseCursor(self.root).ack(node, [identity],
                                    acting_session_id="m2-current-with-bad-live",
                                    worker_session_id=worker)
                            self.assertEqual("incomplete_jsonl_line", caught.exception.code)
                            self.assertEqual(before, self.ack_files())
                    else:
                        self.refusal(live["id"], "ack_recipient_mismatch")
                        self.refusal(worker_guard["id"], "ack_session_mismatch")
                        self.refusal(retracted["id"], "ack_item_retracted", worker="retraction-worker")
                        result = SparseCursor(self.root).ack("seat-b", [live["id"]],
                            acting_session_id="m2-current-with-bad-history")
                        self.assertEqual([live["id"]], result["item_ids"])
            finally:
                target.write_bytes(raw)
        self.assertEqual([], fallback)
    def test_cursor_membership_rechecks_closed_selectors_readers_and_namespace(self):
        message = self.fixture.send("m2-reader-binding", worker_session_id="m2-reader-worker")
        EventLog(self.root).present("seat-a", worker_session_id="m2-reader-worker")
        self.manual_roll("m2-reader-archive")
        self.assertTrue(self.oracle(message["id"], worker="m2-reader-worker"))
        lookup, driver = self.api(), self.driver()
        self.assertIs(True, lookup(self.root, message["id"], "seat-a", "m2-reader-worker"))
        self.assertIs(True, lookup(self.root, message["id"].encode().decode(),
            "seat-a".encode().decode(), "m2-reader-worker".encode().decode()))
        callbacks = []
        class CallerString(str):
            def __eq__(self, other):
                callbacks.append("equal")
                raise AssertionError("caller equality ran")
            def __hash__(self):
                callbacks.append("hash")
                raise AssertionError("caller hash ran")
            def __str__(self):
                callbacks.append("string")
                raise AssertionError("caller conversion ran")
        for args in ((CallerString(message["id"]), "seat-a", None),
                     (message["id"], CallerString("seat-a"), None),
                     (message["id"], "seat-a", CallerString("m2-reader-worker"))):
            with self.subTest(selector=tuple(type(arg).__name__ for arg in args)),\
                    self.assertRaises(IntegrityFailure):
                lookup(self.root, *args)
        for root in (self.root.tenant_home, {}, object()):
            with self.assertRaises(IntegrityFailure):
                lookup(root, message["id"], "seat-a")
        self.assertEqual([], callbacks)
        opened = epoch_history.open_history
        aliases = ((history_query_join, "archived_item_membership"),
            (history_query_join, "_query_charge"), (cursor, "validate_identifier"),
            (history_query_join, "_membership_delivery_rows"),
            (history_query_join, "_membership_contains_item"),
            (SparseCursor, "_session_component"))
        for module, name in aliases:
            calls = []
            def substitute(*args, **kwargs):
                calls.append(True)
                raise AssertionError("post-admission reader replacement ran")
            with ExitStack() as changes:
                def admitted(root):
                    context = opened(root)
                    changes.enter_context(mock.patch.object(module, name, substitute))
                    return context
                with mock.patch.object(epoch_history, "open_history", admitted),\
                        self.subTest(alias=name), self.assertRaises(IntegrityFailure):
                    lookup(self.root, message["id"], "seat-a", "m2-reader-worker")
            self.assertEqual([], calls)
        code = driver.__code__
        try:
            def admitted(root):
                context = opened(root)
                driver.__code__ = changed_cursor_membership_code.__code__
                return context
            with mock.patch.object(epoch_history, "open_history", admitted), self.assertRaises(IntegrityFailure):
                lookup(self.root, message["id"], "seat-a", "m2-reader-worker")
        finally:
            driver.__code__ = code
        archive = snapshot._owned_epoch_archives(self.root)[0] / "events.jsonl"
        decode = jsonl._decode_path_records
        for target in (self.root.tenant_home / "events.jsonl", archive):
            armed, changed = [False], []
            def admitted(root):
                context = opened(root)
                armed[0] = True
                return context
            def observed(*args, **kwargs):
                result = decode(*args, **kwargs)
                if armed[0] and not changed:
                    target.write_bytes(target.read_bytes())  # same bytes, new native identity
                    changed.append(True)
                return result
            with mock.patch.object(epoch_history, "open_history", admitted),\
                    mock.patch.object(history_query_join, "_decode_path_records", observed),\
                    self.subTest(namespace=target.name), self.assertRaises(IntegrityFailure):
                lookup(self.root, message["id"], "seat-a", "m2-reader-worker")
            self.assertEqual([True], changed)

    def test_cursor_membership_charges_before_plane_name_and_item_traversal(self):
        message = self.fixture.send("m2-paid-delivery", worker_session_id="m2-budget-worker")
        EventLog(self.root).present("seat-a", worker_session_id="m2-budget-worker")
        self.manual_roll("m2-budget-archive")
        unseen = self.absent_id()
        self.assertTrue(self.oracle(message["id"], worker="m2-budget-worker"))
        self.assertFalse(self.oracle(unseen, worker="m2-budget-worker"))
        lookup = self.api()
        opened, decode = epoch_history.open_history, jsonl._decode_path_records
        names = getattr(history_query_join, "_membership_delivery_rows", None)
        items = getattr(history_query_join, "_membership_contains_item", None)
        self.assertTrue(callable(names) and callable(items), "charged native lookup primitives are absent")
        armed, entry, target = [False], [None], [None]
        observed, paid = [], {}
        def boundary(name):
            if armed[0]:
                observed.append(name)
                paid.setdefault(name, entry[0] - query_work._QUERY_ALLOWANCE.get().remaining)
        def admitted(root):
            context = opened(root)
            state = query_work._QUERY_ALLOWANCE.get()
            self.assertIsNotNone(state, "membership did not enclose actual admission")
            if target[0] is not None:
                self.assertGreaterEqual(state.remaining, target[0])
                state.charge(state.remaining - target[0])
            entry[0] = state.remaining
            armed[0] = True
            return context
        def decoded(path, *args, **kwargs):
            boundary("event_decode" if path.name == "events.jsonl" else "delivery_decode")
            return decode(path, *args, **kwargs)
        def named(*args, **kwargs):
            boundary("names")
            return names(*args, **kwargs)
        def scanned(*args, **kwargs):
            boundary("items")
            return items(*args, **kwargs)
        with mock.patch.object(epoch_history, "open_history", admitted),\
                mock.patch.object(history_query_join, "_decode_path_records", decoded),\
                mock.patch.object(history_query_join, "_membership_delivery_rows", named),\
                mock.patch.object(history_query_join, "_membership_contains_item", scanned):
            self.assertIs(False, lookup(self.root, unseen, "seat-a", "m2-budget-worker"))
            self.assertEqual({"event_decode", "names", "delivery_decode", "items"}, set(paid))
            boundaries = dict(paid)
            for name, cost in boundaries.items():
                self.assertGreater(cost, 0)
                for shortage in (1, 0):
                    armed[0] = False
                    observed.clear()
                    target[0] = cost - shortage
                    with self.subTest(boundary=name, shortage=shortage),\
                            self.assertRaises(ProtocolRefusal) as caught:
                        lookup(self.root, unseen, "seat-a", "m2-budget-worker")
                    self.assertEqual("epoch_history_query_unsupported", caught.exception.code)
                    self.assertEqual(shortage == 0, name in observed,
                        "reservation did not precede the actual decoder/search/item traversal")


if __name__ == "__main__":
    unittest.main()
