"""M3 archived-envelope batch DATA and only WakeAttempt's old-ID refusal.

Native prerequisites precede missing-API assertions. Their RED cannot establish
post-API interception or prepublication work; those need a watched later bank.
No evaluation/retry/seal migration, fake receipt, host worker or test hook.
"""
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_catalog, bus_epoch, epoch_history
from floati import history_query_join, jsonl, snapshot
from floati import archive_survivor_sources as query_work
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS, EventLog
from floati.framing import decode_frames, encode_frame
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import WakeAttemptLedger, WakeHoldController
from tests import test_epoch_carry as native_fixture
from tests import test_node_lease_protocol as lease_fixture


def changed_wake_membership_code(*args, **kwargs):
    raise AssertionError("substituted wake batch code executed")


class HistoryQueryWakeMembershipTests(unittest.TestCase):
    def setUp(self):
        self.native = self.new_native()
        self.root, self.fixture = self.native.root, self.native
        self.foreign = None

    def new_native(self):
        native = native_fixture.EpochCarryTests()
        self.addCleanup(native.doCleanups)
        native.setUp()
        native.send("retained-history-original")
        bus_epoch.roll_bus_epoch(native.root, actor="sender", idempotency_key="retained-history-original-roll")
        return native

    def api(self):
        method = getattr(epoch_history, "archived_envelope_membership", None)
        self.assertTrue(callable(method), "root-only archive-major batch envelope membership is absent")
        return method

    def driver(self):
        method = getattr(history_query_join, "archived_envelope_membership", None)
        self.assertTrue(callable(method), "guarded captured batch envelope driver is absent")
        return method


    def manual_roll(self, key, *, root=None):
        return bus_epoch.roll_bus_epoch(root or self.root, actor="sender",
            idempotency_key=key)["receipt"]

    def absent_id(self):
        if self.foreign is None:
            self.foreign = FloatiRoot.open_direct_home(self.fixture.base / "foreign-wake-membership", create=True)
            for node in ("sender", "seat-a"):
                Registry(self.foreign).register(node, "Codex")
        return EventLog(self.foreign).send("sender", "seat-a", "floati", "a" * 40,
            "docs/evidence/wake-membership.md", "foreign native message")["id"]

    def oracle(self, missing, *, root=None):
        """Exact native archive-major envelope-only reader, independent of M3."""
        root = root or self.root
        with bus_epoch.epoch_guard(root):
            for archive in snapshot._owned_epoch_archives(root):
                rows = jsonl.read_records_snapshot(root,
                    archive.relative_to(root.tenant_home) / "events.jsonl", allowed_kinds=set(EVENT_KINDS))
                if any(row["kind"] == "message_envelope" and row["id"] in missing for row in rows):
                    return True
        return False

    def files(self, root=None):
        root = root or self.root
        return {path.relative_to(root.tenant_home).as_posix(): path.read_bytes()
                for path in root.tenant_home.rglob("*") if path.is_file()}

    def attempt_files(self, root=None):
        return {name: raw for name, raw in self.files(root).items() if name.startswith("receipts/wakes/")}

    def attempts(self, root=None, node="seat-a"):
        return jsonl.read_records_snapshot(root or self.root,
            Path("receipts/wakes") / (node + ".jsonl"), allowed_kinds={"wake_attempt_receipt"})

    def record(self, ids, key, *, root=None, node="seat-a", worker=None,
               acting="m3-acting", outcome="refused", reason="wake_prompt_failed"):
        return WakeAttemptLedger(root or self.root).record(recipient=node,
            acting_session_id=acting, item_ids=ids, decision_receipt_id=None,
            message_worker_session_id=worker, idempotency_key=key,
            outcome=outcome, reason_code=reason)

    def refused(self, ids, key, code, *, durable=False, root=None, node="seat-a", **kwargs):
        before = self.attempt_files(root)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.record(ids, key, root=root, node=node, **kwargs)
        self.assertEqual(code, caught.exception.code)
        if durable:
            rows = [row for row in self.attempts(root, node) if row["idempotency_key"] == key]
            self.assertEqual(1, len(rows))
            self.assertEqual(("refused", code, ids),
                (rows[0]["outcome"], rows[0]["reason_code"], rows[0]["item_ids"]))
        else:
            self.assertEqual(before, self.attempt_files(root), "pretransaction refusal appended an attempt")

    def remove_live_envelopes(self, identities):
        path = self.root.tenant_home / "events.jsonl"
        rows = decode_frames(path.read_bytes())
        self.assertTrue(set(identities).issubset({row["id"] for row in rows}))
        path.write_bytes(b"".join(encode_frame(row) for row in rows
            if row["id"] not in identities and not (
                row["kind"] == "send_readiness_observation" and row["envelope_id"] in identities)))

    def captured(self, root=None):
        context = epoch_history.open_history(root or self.root)
        return context, epoch_history._CONTEXTS[context].native

    def test_batch_classification_preserves_archived_and_durable_unknown_distinction(self):
        archived = self.fixture.send("m3-archived")
        foreign_scope = self.fixture.send("m3-global-archived", node="seat-b", worker_session_id="foreign-worker")
        self.manual_roll("m3-classification")
        unknown = self.absent_id()
        current_wrong_owner = self.fixture.send("m3-current-wrong-owner", node="seat-b")
        selectors = ((frozenset({archived["id"]}), True),
            (frozenset({foreign_scope["id"]}), True), (frozenset({unknown}), False),
            (frozenset({unknown, foreign_scope["id"]}), True))
        for batch, expected in selectors:
            self.assertEqual(expected, self.oracle(batch))
        self.refused([archived["id"]], "m3-archived-only", "wake_envelope_archived")
        self.refused([unknown, foreign_scope["id"], current_wrong_owner["id"]],
            "m3-global-before-current-ownership", "wake_envelope_archived")
        self.refused([unknown], "m3-native-unknown", "wake_envelope_not_owned", durable=True)
        before = self.attempt_files()
        self.refused([unknown], "m3-native-unknown", "wake_envelope_not_owned", durable=True)
        self.assertEqual(before, self.attempt_files(), "refused retry must replay its native receipt")
        self.assertEqual(1, len(self.attempts()))
        lookup = self.api()
        for batch, expected in selectors:
            self.assertIs(expected, lookup(self.root, batch))

    def test_envelope_only_batch_and_admitted_archive_major_order(self):
        delivery_only = self.fixture.send("m3-delivery-only", worker_session_id="delivery-worker")
        _shown, delivery = EventLog(self.root).present("seat-a", worker_session_id="delivery-worker")
        self.assertIn(delivery_only["id"], delivery["item_ids"])
        self.remove_live_envelopes({delivery_only["id"]})
        receipt = self.manual_roll("m3-adversarial-source-removal")
        selector = frozenset({delivery_only["id"]})
        self.assertFalse(self.oracle(selector))
        context = epoch_history.open_history(self.root)
        self.assertIsNone(context.generation, "adversarial source removal is never an issuance fixture")
        selected = next(epoch for epoch in context.epochs if epoch.receipt["id"] == receipt["id"])
        source = context.source(selected.coordinate, "seat-a", "delivery-worker")
        self.assertNotIn(delivery_only["id"], [row["id"] for row in source[0]])
        self.assertTrue(any(delivery_only["id"] in row["item_ids"] for row in source[2]))
        self.assertIs(True, epoch_history.archived_item_membership(
            self.root, delivery_only["id"], "seat-a", "delivery-worker"))
        self.refused([delivery_only["id"]], "m3-delivery-is-not-envelope", "wake_envelope_not_owned",
            durable=True, worker="delivery-worker", acting="delivery-worker")
        # Separate no-commit root: never issue the source-removal history.
        ordered = self.new_native()
        root = ordered.root
        events = root.tenant_home / "events.jsonl"
        future = {"schema_version": 99, "id": "m3-future-schema-kind", "tenant_id": root.tenant_id,
            "timestamp": "2026-10-20T20:00:00.000Z", "kind": "m3_future_schema_kind"}
        events.write_bytes(events.read_bytes() + encode_frame(future))
        self.manual_roll("m3-older-schema-invalid", root=root)
        match = ordered.send("m3-newer-envelope")
        self.manual_roll("m3-newer-hit", root=root)
        # Make the no-hit native ID the first set iterator element so an
        # incorrect per-ID implementation reliably reaches the invalid older
        # plane instead of accidentally finding the hit first.
        batch = None
        for _ in range(256):
            unknown = self.absent_id()
            candidate = frozenset((unknown, match["id"]))
            if next(iter(candidate)) == unknown:
                batch = candidate
                break
        self.assertIsNotNone(batch, "native selector fixture could not put a no-hit ID first")
        self.assertTrue(self.oracle(batch, root=root))
        with self.assertRaises(IntegrityFailure):
            self.oracle(frozenset({unknown}), root=root)
        self.assertIsNone(epoch_history.open_history(root).generation)
        lookup = self.api()
        self.assertIs(False, lookup(self.root, selector))
        self.assertIs(True, lookup(root, batch))
        with self.assertRaises(IntegrityFailure):
            lookup(root, frozenset({unknown}))

    def test_only_missing_current_ids_enter_guarded_commit_admission(self):
        current = self.fixture.send("m3-carried-current")
        self.fixture.automatic_roll()
        unknown = self.absent_id()
        self.assertTrue(self.oracle(frozenset({current["id"]})))
        self.assertFalse(self.oracle(frozenset({unknown})))
        self.assertIn(current["id"], [row["id"] for row in EventLog(self.root).records()])
        self.refused([current["id"], unknown], "m3-native-only-missing", "wake_envelope_not_owned", durable=True)
        valid = self.new_native()
        root = valid.root
        archived = valid.send("m3-valid-issued-archive")
        self.manual_roll("m3-valid-issued-roll", root=root)
        live = valid.send("m3-valid-postcommit-current")
        valid.automatic_roll()
        self.assertTrue(self.oracle(frozenset({archived["id"]}), root=root))
        self.assertIsNone(epoch_history.open_history(root).generation)
        lookup = self.api()
        observed = []
        def actual(root_arg, ids):
            self.assertIs(type(ids), frozenset)
            observed.append(ids)
            return lookup(root_arg, ids)
        with mock.patch.object(epoch_history, "archived_envelope_membership", actual):
            self.refused([current["id"], unknown], "m3-observe-only-missing", "wake_envelope_not_owned", durable=True)
        self.assertEqual([frozenset({unknown})], observed)
        self.assertIs(True, lookup(root, frozenset({archived["id"]})))
        targets = (root.tenant_home / "events.jsonl",
            snapshot._owned_epoch_archives(root)[0] / "events.jsonl")
        fallback = []
        def legacy(*args, **kwargs):
            fallback.append(True)
            raise AssertionError("bad claimed history fell back to legacy archive traversal")
        for index, target in enumerate(targets):
            raw = target.read_bytes()
            try:
                target.write_bytes(raw + b"{partial")
                for identity in (archived["id"], unknown):
                    before = self.attempt_files(root)
                    with mock.patch.object(snapshot, "_owned_epoch_archives", legacy),\
                            self.assertRaises((IntegrityFailure, ProtocolRefusal)) as caught:
                        self.record([identity], "m3-corrupt-%d" % index, root=root)
                    self.assertNotIn(caught.exception.code, {"wake_envelope_archived", "wake_envelope_not_owned"})
                    self.assertEqual(before, self.attempt_files(root))
                def no_open(*args, **kwargs):
                    raise AssertionError("all-current explicit refusal opened history")
                with mock.patch.object(epoch_history, "open_history", no_open):
                    if target == root.tenant_home / "events.jsonl":
                        before = self.attempt_files(root)
                        with self.assertRaises(IntegrityFailure) as caught:
                            self.record([live["id"]], "m3-current-bad-live", root=root)
                        self.assertEqual("incomplete_jsonl_line", caught.exception.code)
                        self.assertEqual(before, self.attempt_files(root))
                    else:
                        self.refused([live["id"]], "m3-current-bypass-%d" % index,
                            "wake_prompt_failed", durable=True, root=root)
            finally:
                target.write_bytes(raw)
        self.assertEqual([], fallback)

    def test_retired_and_expired_active_node_boundaries_remain_native(self):
        leased = lease_fixture.NodeLeaseProtocolTests()
        self.addCleanup(leased.doCleanups)
        leased.setUp()
        message = leased.send("permanent", "temporary",
            now=lease_fixture.NOW + timedelta(seconds=1), key="m3-before-expiry")
        self.assertEqual("expired", leased.registry.node_lease_state("temporary",
            now=lease_fixture.NOW + timedelta(seconds=6))["state"])
        unknown = self.absent_id()
        self.refused([message["id"]], "m3-expired-current", "wake_prompt_failed",
            root=leased.root, node="temporary", durable=True)
        self.refused([unknown], "m3-expired-unknown", "wake_envelope_not_owned",
            root=leased.root, node="temporary", durable=True)
        self.assertFalse(self.oracle(frozenset({unknown}), root=leased.root))
        retracted = self.fixture.send("m3-current-retracted", worker_session_id="m3-retracted-worker")
        EventLog(self.root).retract(retracted["id"], worker_session_id="m3-retracted-worker",
            reason="sent_in_error", author="sender")
        self.refused([retracted["id"]], "m3-native-current-retraction", "wake_prompt_failed",
            durable=True, worker="m3-retracted-worker", acting="m3-retracted-worker")
        Registry(self.root).retire("seat-a")
        target = self.root.tenant_home / archive_catalog.LEDGER
        target.write_bytes(b"{partial")
        self.refused([unknown], "m3-retired-before-history", "unknown_node")
        lookup = self.api()
        self.assertIs(False, lookup(leased.root, frozenset({unknown})))
        self.refused([unknown], "m3-expired-unknown-postapi", "wake_envelope_not_owned",
            root=leased.root, node="temporary", durable=True)
        def no_open(*args, **kwargs):
            raise AssertionError("retired node reached history admission")
        with mock.patch.object(epoch_history, "open_history", no_open):
            self.refused([unknown], "m3-retired-postapi", "unknown_node")

    def test_closed_batch_selectors_and_actual_reader_namespace_bindings(self):
        message = self.fixture.send("m3-binding-archive")
        self.manual_roll("m3-bindings")
        batch = frozenset({message["id"]})
        self.assertTrue(self.oracle(batch))
        lookup, driver = self.api(), self.driver()
        self.assertIs(True, lookup(self.root, batch))
        armed, callbacks = [False], []
        class CallerString(str):
            def __hash__(self):
                if armed[0]:
                    callbacks.append("hash")
                    raise AssertionError("API hashed caller element")
                return str.__hash__(self)
            def __eq__(self, other):
                if armed[0]:
                    callbacks.append("equal")
                    raise AssertionError("API compared caller element")
                return str.__eq__(self, other)
            def __str__(self):
                if armed[0]:
                    callbacks.append("string")
                    raise AssertionError("API converted caller element")
                return str.__str__(self)
        class CallerSet(frozenset):
            def __len__(self):
                callbacks.append("container length")
                raise AssertionError("API inspected caller container")
            def __iter__(self):
                callbacks.append("container iteration")
                raise AssertionError("API traversed caller container")
        # Caller construction necessarily hashes. Arm/reset only afterward.
        malicious = (frozenset({CallerString(message["id"])}),
            frozenset({message["id"], CallerString("hostile-nonmatch")}))
        subclass = CallerSet(batch)
        callbacks.clear()
        armed[0] = True
        for selector in malicious + (subclass, set(batch), list(batch), {},
                frozenset(), frozenset({""}), frozenset({object()}),
                frozenset("direct-invalid-%d" % index for index in range(1001))):
            with self.subTest(selector=type(selector).__name__), self.assertRaises(IntegrityFailure):
                lookup(self.root, selector)
        self.assertEqual([], callbacks)
        for root in (self.root.tenant_home, {}, object()):
            with self.assertRaises(IntegrityFailure):
                lookup(root, batch)
        self.assertEqual(1000, history_query_join.MAX_BATCH_IDS)
        maximum = frozenset("direct-valid-%d" % index for index in range(1000))
        self.assertIs(False, lookup(self.root, maximum))
        self.refused(["outer-invalid-%d" % index for index in range(1001)],
            "m3-native-upper-bound", "item_ids_invalid")
        # These unused helpers are bound BEFORE real admission, not substituted
        # afterward. Envelope-only lookup must never invoke delivery semantics.
        unused = []
        def delivery(*args, **kwargs):
            unused.append(True)
            raise AssertionError("batch envelope lookup called delivery membership")
        with mock.patch.object(history_query_join, "_membership_delivery_rows", delivery),\
                mock.patch.object(history_query_join, "_membership_contains_item", delivery),\
                mock.patch.object(SparseCursor, "_delivery_relative_path_for", delivery):
            self.assertIs(True, lookup(self.root, batch))
        self.assertEqual([], unused)
        opened = epoch_history.open_history
        aliases = ((epoch_history, "archived_envelope_membership"),
            (history_query_join, "archived_envelope_membership"),
            (history_query_join, "_membership_batch_selectors"),
            (history_query_join, "_membership_plane"),
            (history_query_join, "_query_charge"), (history_query_join, "_domain_digest"),
            (history_query_join, "MAX_BATCH_IDS"))
        for module, name in aliases:
            calls = []
            def substitute(*args, **kwargs):
                calls.append(True)
                raise AssertionError("post-admission batch reader executed")
            replacement = 999 if name == "MAX_BATCH_IDS" else substitute
            with ExitStack() as changes:
                def admitted(root):
                    context = opened(root)
                    changes.enter_context(mock.patch.object(module, name, replacement))
                    return context
                with mock.patch.object(epoch_history, "open_history", admitted),\
                        self.subTest(alias=name), self.assertRaises(IntegrityFailure):
                    lookup(self.root, batch)
            self.assertEqual([], calls)
        code = driver.__code__
        try:
            def admitted(root):
                context = opened(root)
                driver.__code__ = changed_wake_membership_code.__code__
                return context
            with mock.patch.object(epoch_history, "open_history", admitted), self.assertRaises(IntegrityFailure):
                lookup(self.root, batch)
        finally:
            driver.__code__ = code
        archive = snapshot._owned_epoch_archives(self.root)[0] / "events.jsonl"
        decoder = jsonl._decode_path_records
        for target in (self.root.tenant_home / "events.jsonl", archive):
            ready, changed = [False], []
            before = self.attempt_files()
            def admitted(root):
                context = opened(root)
                ready[0] = True
                return context
            def decoded(*args, **kwargs):
                result = decoder(*args, **kwargs)
                if ready[0] and not changed:
                    target.write_bytes(target.read_bytes())
                    changed.append(True)
                return result
            with mock.patch.object(epoch_history, "open_history", admitted),\
                    mock.patch.object(history_query_join, "_decode_path_records", decoded),\
                    self.subTest(namespace=target.name), self.assertRaises(IntegrityFailure):
                self.record([message["id"]], "m3-namespace")
            self.assertEqual([True], changed)
            self.assertEqual(before, self.attempt_files())

    def test_batch_reservations_and_real_support_dominate_lookup(self):
        message = self.fixture.send("m3-repeat-carried")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="m3-native-held")
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        unknown = self.absent_id()
        self.assertTrue(self.oracle(frozenset({message["id"]})))
        self.assertFalse(self.oracle(frozenset({unknown})))
        bare = native_fixture.EpochCarryTests()
        self.addCleanup(bare.doCleanups)
        bare.setUp()
        (bare.root.tenant_home / "events.jsonl").write_bytes(b"")
        empty_context, empty_native = self.captured(bare.root)
        self.assertEqual((), empty_context.epochs)
        lookup, driver = self.api(), self.driver()
        absent = frozenset("direct-missing-%d" % index for index in range(1000))
        hit = frozenset(tuple("direct-missing-%d" % index for index in range(999)) + (message["id"],))
        context, native = self.captured()
        for root, epochs in ((bare.root, empty_native), (self.root, native)):
            archives = tuple(epoch for epoch in epochs if epoch.owner is not None)
            for selected in tuple((epoch,) for epoch in archives) + (epochs,):
                for selector in (frozenset({unknown}), frozenset({message["id"]}), absent, hit):
                    with query_work._query_work_scope(epoch_history.MAX_QUERY_WORK) as allowance:
                        start = allowance.remaining
                        result = driver(root, selected, selector, work=history_query_join._Work(0),
                            max_result_bytes=epoch_history.MAX_QUERY_RESULT_BYTES)
                        spent = start - allowance.remaining
                    self.assertIs(type(result), bool)
                    if root is bare.root and len(selector) == 1000:
                        self.assertEqual(1001, spent)
        self.assertIsNotNone(context)
        opened, decoder, digest = epoch_history.open_history, jsonl._decode_path_records, history_query_join._domain_digest
        selectors = getattr(history_query_join, "_membership_batch_selectors", None)
        self.assertTrue(callable(selectors), "closed batch selector validator is absent")
        armed, entry, target = [False], [None], [None]
        seen, paid = [], {}
        def boundary(name):
            if armed[0]:
                seen.append(name)
                paid.setdefault(name, entry[0] - query_work._QUERY_ALLOWANCE.get().remaining)
        def admitted(root):
            context = opened(root)
            state = query_work._QUERY_ALLOWANCE.get()
            self.assertIsNotNone(state)
            if target[0] is not None:
                self.assertGreaterEqual(state.remaining, target[0])
                state.charge(state.remaining - target[0])
            entry[0] = state.remaining
            armed[0] = True
            return context
        def validated(*args, **kwargs):
            boundary("selectors")
            return selectors(*args, **kwargs)
        def decoded(*args, **kwargs):
            boundary("decode")
            return decoder(*args, **kwargs)
        def measured(name, value, **kwargs):
            if name == "native_archived_membership_rows":
                boundary("rows")
            return digest(name, value, **kwargs)
        with mock.patch.object(epoch_history, "open_history", admitted),\
                mock.patch.object(history_query_join, "_membership_batch_selectors", validated),\
                mock.patch.object(history_query_join, "_decode_path_records", decoded),\
                mock.patch.object(history_query_join, "_domain_digest", measured):
            self.assertIs(False, lookup(self.root, absent))
            self.assertEqual({"selectors", "decode", "rows"}, set(paid))
            self.assertEqual(1000, paid["selectors"])
            for name, cost in dict(paid).items():
                for shortage in (1, 0):
                    armed[0] = False
                    seen.clear()
                    target[0] = cost - shortage
                    with self.subTest(boundary=name, shortage=shortage),\
                            self.assertRaises(ProtocolRefusal) as caught:
                        lookup(self.root, absent)
                    self.assertEqual("epoch_history_query_unsupported", caught.exception.code)
                    self.assertEqual(shortage == 0, name in seen)



if __name__ == "__main__":
    unittest.main()
