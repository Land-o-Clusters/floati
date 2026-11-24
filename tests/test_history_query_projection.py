"""Native context projection DATA, compared with the original native resolver.

Every fixture/oracle is established before requiring the new accessor. The
initial missing-accessor RED therefore proves only absent API support; the
substitution and allocation controls must be observed again after it exists.
"""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import stat
import unittest
from unittest import mock

from floati import archive_projection_replay
from floati import archive_survivor_sources, bus_epoch, epoch_carry, epoch_history
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS
from floati.message_consumption import MessageConsumption
from floati.wake_hold import WakeHoldController, project_wake_items
from tests import test_epoch_carry as native_fixture


def changed_project_native(*args, **kwargs):
    # No closure: swapping a genuine module function's code must test the
    # reader contract rather than fail Python's closure compatibility check.
    raise AssertionError("changed projection code ran")


def cut(source):
    return {"counts": tuple(len(source[index]) for index in (0, 2, 4)),
            "digests": tuple(source[index][-1] for index in (1, 3, 5))}


def states_data(states):
    return [{"message": deepcopy(dict(item.message)), "state": item.state,
             "presentation_count": item.presentation_count} for item in states]


def wire_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8"))


class HistoryQueryProjectionTests(unittest.TestCase):
    def setUp(self):
        self.native = native_fixture.EpochCarryTests()
        self.native.setUp()
        self.native.send("retained-history-original")
        bus_epoch.roll_bus_epoch(self.native.root, actor="sender", idempotency_key="retained-history-original-roll")
        self.addCleanup(self.native.doCleanups)
        self.fixture = self.native
        self.root = self.native.root


    def accessor(self, context):
        method = getattr(context, "projection_data", None)
        self.assertTrue(callable(method),
            "root-only context has no guarded native cut/origin/projection DATA accessor")
        return method

    def native_oracle(self, coordinate=("live",), node="seat-a", worker=None):
        """Use only the old native Path resolver/projector on the expected side.

        Native receipts identify homes; no source-pool content key or new
        adapter contributes to this expected result. Imported origins also
        reproduce the old resolver's exact public original testimony.
        """
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            resolver = epoch_carry.ArchiveProofResolver(self.root)
            homes = (self.root.tenant_home, *resolver.archives())
            archives = epoch_history.read_history(self.root,
                max_records=MAX_LEDGER_RECORDS, max_bytes=MAX_LEDGER_BYTES)
            self.assertEqual(list(homes[1:]), [row.legacy_path for row in archives])
            sources = [resolver.source(home, node, worker) for home in homes]
            coordinates = [("live",)]
            for archive, source in zip(archives, sources[1:]):
                anchor = source[0][0] if source[0] else None
                anchor_id = (anchor["id"] if anchor is not None
                             and anchor["kind"] == "bus_epoch_roll_receipt" else None)
                coordinates.append(("archive", archive.receipt["id"], anchor_id))
            selected = coordinates.index(coordinate)

            def project(source, position):
                proofs = epoch_carry.verified_hold_proofs(self.root, source, node, worker,
                    home=homes[position], resolver=resolver)
                return project_wake_items(events=source[0], event_prefix_digests=source[1],
                    deliveries=source[2], delivery_prefix_digests=source[3],
                    acknowledgments=source[4], acknowledgment_prefix_digests=source[5],
                    recipient=node, worker_session_id=worker,
                    tenant_id=self.root.tenant_id, _hold_proofs=proofs)

            origins = {}

            def visit(source, position):
                for ordinal, receipt in enumerate(source[2]):
                    if receipt["kind"] != "wake_hold_receipt":
                        continue
                    bound = epoch_carry._original_hold_cuts(source, receipt, ordinal)
                    origin_position = position
                    if bound is None:
                        testimony = resolver.hold_origin(receipt, node, worker, homes[position])
                        self.assertEqual(encode_frame(receipt), testimony[0])
                        for old_position in range(position + 1, len(sources)):
                            for old_ordinal, old in enumerate(sources[old_position][2]):
                                if old["id"] != receipt["id"]:
                                    continue
                                self.assertEqual(encode_frame(receipt), encode_frame(old))
                                candidate = epoch_carry._original_hold_cuts(
                                    sources[old_position], old, old_ordinal)
                                if candidate is not None:
                                    bound, origin_position = candidate, old_position
                                    break
                            if bound is not None:
                                break
                        self.assertIsNotNone(bound, "native resolver origin has no physical cut")
                        self.assertEqual(testimony[1], bound[0])
                    before, after = bound
                    project(after, origin_position)
                    original_states = project(before, origin_position)
                    if origin_position != position:
                        self.assertEqual(states_data(testimony[2]), states_data(original_states))
                    if receipt['schema_version'] == 3:
                        selected = set(receipt['item_ids'])
                        fresh = [item for item in original_states if item.message['id'] in selected]
                        held = [item for item in original_states
                                if item.state == 'held' and item.message['id'] not in selected]
                    else:
                        fresh = [item for item in original_states if item.state == 'fresh']
                        held = [item for item in original_states if item.state == 'held']
                    artifact = WakeHoldController._artifact(tenant_id=self.root.tenant_id,
                        recipient=node, worker_session_id=worker, limit=receipt["limit"],
                        fresh=fresh, held=held,
                        receipt=receipt, event_digest=receipt["event_prefix_digest"],
                        delivery_digest=receipt["delivery_prefix_digest"],
                        acknowledgment_digest=receipt["acknowledgment_prefix_digest"])
                    row = {"receipt": deepcopy(receipt),
                           "origin_coordinate": coordinates[origin_position],
                           "before_cut": cut(before), "after_cut": cut(after),
                           "original_states": states_data(original_states),
                           "original_artifact": artifact}
                    if receipt["id"] in origins:
                        self.assertEqual(encode_frame(origins[receipt["id"]]["receipt"]),
                                         encode_frame(receipt))
                        self.assertEqual(origins[receipt["id"]], row)
                        continue
                    origins[receipt["id"]] = row
                    visit(before, origin_position)

            selected_source = sources[selected]
            expected_states = states_data(project(selected_source, selected))
            visit(selected_source, selected)
            return {"coordinate": coordinate, "recipient": node,
                    "worker_session_id": worker, "source_cut": cut(selected_source),
                    "states": expected_states,
                    "origins": [origins[key] for key in sorted(origins)]}

    def two_holds(self):
        self.fixture.send("projection-held")
        first = WakeHoldController(self.root).evaluate("seat-a",
            idempotency_key="projection-first", limit=1)
        self.fixture.send("projection-fresh-one")
        self.fixture.send("projection-fresh-two")
        second = WakeHoldController(self.root).evaluate("seat-a",
            idempotency_key="projection-second", limit=1)
        self.assertEqual((2, 1, True),
            (second["fresh_total"], second["held_total"], second["fresh_truncated"]))
        return {row["receipt"]["id"]: row for row in (first, second)}

    def test_complete_original_artifacts_after_native_commit_and_two_rolls(self):
        originals = self.two_holds()
        # This accessor control compares byte-exact imported v1/v2 decisions.
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        expected = self.native_oracle()
        self.assertEqual(originals, {row["receipt"]["id"]: row["original_artifact"]
                                     for row in expected["origins"]})
        context = epoch_history.open_history(self.root)
        self.assertEqual(expected, self.accessor(context)(context.live_coordinate, "seat-a"))

    def test_roll_anchored_holds_have_native_projection_origins(self):
        originals = self.two_holds()
        by_key = {row['receipt']['idempotency_key']: row for row in originals.values()}
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        expected = self.native_oracle()
        self.assertTrue(expected['origins'])
        for origin in expected['origins']:
            receipt = origin['receipt']
            self.assertEqual(3, receipt['schema_version'])
            self.assertEqual(('live',), origin['origin_coordinate'])
            original = by_key[receipt['idempotency_key']]
            self.assertEqual(original['receipt']['decision_digest'], receipt['decision_digest'])
            self.assertEqual(original['receipt']['item_ids'],
                             [row['id'] for row in origin['original_artifact']['fresh_messages']])
        context = epoch_history.open_history(self.root)
        method = self.accessor(context)
        self.assertEqual(expected, method(('live',), 'seat-a'))
        for epoch in context.epochs:
            self.assertEqual(self.native_oracle(epoch.coordinate), method(epoch.coordinate, 'seat-a'))

    def test_original_and_carried_cuts_match_native_origin_testimony(self):
        originals = self.two_holds()
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        expected = self.native_oracle()
        for origin in expected["origins"]:
            before, after = origin["before_cut"], origin["after_cut"]
            self.assertEqual(before["counts"][0::2], after["counts"][0::2])
            self.assertEqual(before["counts"][1] + 1, after["counts"][1])
            self.assertEqual(tuple(originals[origin["receipt"]["id"]][name] for name in
                ("event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest")),
                before["digests"])
            self.assertNotEqual(("live",), origin["origin_coordinate"])
        context = epoch_history.open_history(self.root)
        method = self.accessor(context)
        self.assertEqual(expected, method(("live",), "seat-a"))
        for epoch in context.epochs:
            self.assertEqual(self.native_oracle(epoch.coordinate), method(epoch.coordinate, "seat-a"))

    def test_ack_before_first_act_has_exact_context_projection_without_consumed_history(self):
        message = self.fixture.send("projection-future-act")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="projection-seen")
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        expected = self.native_oracle()
        # Fully ACKed unrelated history need not survive LIVE. The historical
        # original source remains queryable without inventing a prior ACT.
        archives = epoch_history.read_history(self.root,
            max_records=MAX_LEDGER_RECORDS, max_bytes=MAX_LEDGER_BYTES)
        owner = next(row for row in archives if any(frame["id"] == message["id"]
            for frame in epoch_carry.ArchiveProofResolver(self.root).source(row.legacy_path, "seat-a")[0]))
        source = epoch_carry.ArchiveProofResolver(self.root).source(owner.legacy_path, "seat-a")
        anchor = source[0][0]
        coordinate = ("archive", owner.receipt["id"],
            anchor["id"] if anchor["kind"] == "bus_epoch_roll_receipt" else None)
        historical = self.native_oracle(coordinate)
        state = next(row for row in historical["states"] if row["message"]["id"] == message["id"])
        self.assertEqual(("acknowledged", 1), (state["state"], state["presentation_count"]))
        self.assertFalse((self.root.tenant_home / "receipts/consumed").exists())
        context = epoch_history.open_history(self.root)
        method = self.accessor(context)
        self.assertEqual(expected, method(("live",), "seat-a"))
        self.assertEqual(historical, method(coordinate, "seat-a"))

    def test_manual_boundary_keeps_old_act_source_without_new_act_permission(self):
        message = self.fixture.send("projection-old-act")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="projection-seen")
        reader = MessageConsumption(self.root)
        old_receipt = reader.consume("seat-a", message["id"], "projection-actor")
        another = self.fixture.send("projection-no-new-act")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [another["id"]], acting_session_id="projection-seen")
        manual = bus_epoch.roll_bus_epoch(self.root, actor="sender",
            idempotency_key="projection-manual")["receipt"]
        self.fixture.automatic_roll()
        resolver = epoch_carry.ArchiveProofResolver(self.root)
        home = Path(manual["archive_path"])
        source = resolver.source(home, "seat-a")
        coordinate = ("archive", manual["id"], source[0][0]["id"])
        expected = self.native_oracle(coordinate)
        self.assertIn(message["id"], [row["message"]["id"] for row in expected["states"]])
        from floati.archive_consumption_replay import replay_history
        sources = tuple(resolver.source(path, "seat-a") for path in
                        (self.root.tenant_home, *resolver.archives()))
        history = replay_history(self.root.tenant_id, "seat-a", None, sources, [old_receipt], [])
        self.assertEqual([], history["current_receipts"])
        self.assertEqual(dict(message["message"]), history["joins"][0]["envelope"])
        with self.assertRaises(ProtocolRefusal) as refused:
            reader.consume("seat-a", another["id"], "projection-actor")
        self.assertEqual("consume_item_archived", refused.exception.code)
        context = epoch_history.open_history(self.root)
        result = self.accessor(context)(coordinate, "seat-a")
        self.assertEqual(expected, result)
        self.assertFalse({"eligibility", "proof", "can_act"} & result.keys())

    def test_completed_groups_and_worker_planes_keep_original_denominators(self):
        self.fixture.send("projection-completed-group")
        self.fixture.presentation(flushed=True)
        originals = {}
        for worker in ("projection-worker-a", "projection-worker-b"):
            self.fixture.send("projection-mail-" + worker, worker_session_id=worker)
            artifact = WakeHoldController(self.root).evaluate("seat-a",
                idempotency_key="projection-hold-" + worker, worker_session_id=worker)
            originals[worker] = artifact
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        expected = {worker: self.native_oracle(worker=worker) for worker in (None, *originals)}
        self.assertTrue(expected[None]["source_cut"]["counts"][1])
        for worker, artifact in originals.items():
            self.assertEqual([artifact], [row["original_artifact"] for row in expected[worker]["origins"]])
            self.assertEqual({worker}, {row["message"]["worker_session_id"]
                                        for row in expected[worker]["states"]})
        context = epoch_history.open_history(self.root)
        method = self.accessor(context)
        for worker, oracle in expected.items():
            with self.subTest(worker=worker):
                self.assertEqual(oracle, method(("live",), "seat-a", worker))

    def test_projection_is_detached_data_and_selector_is_not_authority(self):
        self.two_holds()
        expected = self.native_oracle()
        self.assertTrue(expected["origins"])
        context = epoch_history.open_history(self.root)
        method = self.accessor(context)
        result = method(tuple(["live"]), "seat-a")
        self.assertEqual(expected, result)

        def closed(value):
            self.assertIn(type(value), (dict, list, tuple, str, int, bool, type(None)))
            if type(value) is dict:
                for key, member in value.items():
                    self.assertIs(str, type(key))
                    closed(member)
            elif type(value) in (list, tuple):
                for member in value:
                    closed(member)
        closed(result)
        result["states"][0]["message"]["note"] = "detached caller edit"
        result["origins"][0]["original_artifact"]["fresh_total"] = 999
        self.assertEqual(expected, method(("live",), "seat-a"))
        other = native_fixture.EpochCarryTests()
        other.setUp()
        other.send("projection-foreign-original")
        bus_epoch.roll_bus_epoch(other.root, actor="sender", idempotency_key="projection-foreign-roll")
        self.addCleanup(other.doCleanups)
        foreign = epoch_history.open_history(other.root).epochs[0].coordinate
        equality_calls = []
        class BorrowedString(str):
            def __eq__(self, other):
                equality_calls.append(True)
                return True
        for value in (self.root.tenant_home, "0" * 64, {"coordinate": ("live",)},
                      ["live"], foreign, (BorrowedString("live"),),
                      ("archive", 1, None), ("archive", "owner", BorrowedString("anchor"))):
            with self.subTest(value=repr(value)), self.assertRaises(IntegrityFailure):
                method(value, "seat-a")
        self.assertEqual([], equality_calls, "coordinate types were compared before exact validation")
        # Equal local copies select DATA; they do not acquire root authority.
        local = context.epochs[0].coordinate
        self.assertEqual(self.native_oracle(local), method(tuple(list(local)), "seat-a"))
        forged = object.__new__(type(context))
        with self.assertRaises(IntegrityFailure):
            type(context).projection_data(forged, ("live",), "seat-a")

    def test_projection_rechecks_sources_and_actual_reader_semantics_before_dispatch(self):
        self.two_holds()
        expected = self.native_oracle()
        context = epoch_history.open_history(self.root)
        method = self.accessor(context)
        self.assertEqual(expected, method(("live",), "seat-a"))
        adapter = importlib.import_module("floati.history_query_projection")
        calls = []
        def substituted(*args, **kwargs):
            calls.append(True)
            raise AssertionError("changed projection semantics ran")
        for owner, name in ((adapter, "project_native"),
                            (adapter, "_projection_replayer"),
                            (archive_projection_replay, "_original_hold_cuts")):
            with self.subTest(reader=name), mock.patch.object(owner, name, substituted):
                with self.assertRaises(IntegrityFailure):
                    method(("live",), "seat-a")
            self.assertEqual([], calls)
        original = adapter.project_native
        original_code = original.__code__
        try:
            original.__code__ = changed_project_native.__code__
            with self.assertRaises(IntegrityFailure):
                method(("live",), "seat-a")
        finally:
            original.__code__ = original_code
        reject_dispatch = [False]
        def observed(*args, **kwargs):
            if reject_dispatch[0]:
                calls.append(True)
                raise AssertionError("changed native source reached projection dispatch")
            return original(*args, **kwargs)
        # Install the observer before admission so this subcase isolates a
        # source change, rather than being masked by helper substitution.
        with mock.patch.object(adapter, "project_native", observed):
            watched = epoch_history.open_history(self.root)
            watched_method = self.accessor(watched)
            self.assertEqual(expected, watched_method(("live",), "seat-a"))
            self.fixture.send("projection-live-after-admission")
            reject_dispatch[0] = True
            with self.assertRaises(IntegrityFailure):
                watched_method(("live",), "seat-a")
        self.assertEqual([], calls)
        # A fresh context uses the appended source instead of a cached result.
        fresh = epoch_history.open_history(self.root)
        self.assertEqual(self.native_oracle(), self.accessor(fresh)(("live",), "seat-a"))

    def test_projection_work_and_result_exhaustion_precede_replay_or_copy(self):
        originals = self.two_holds()
        expected = self.native_oracle()
        context = epoch_history.open_history(self.root)
        method = self.accessor(context)
        self.assertEqual(expected, method(("live",), "seat-a"))
        adapter = importlib.import_module("floati.history_query_projection")
        calls = []
        def premature(*args, **kwargs):
            calls.append(True)
            raise AssertionError("allocation/replay ran after support was exhausted")
        with mock.patch.object(archive_projection_replay, "_project_origin_data", premature):
            bounded = epoch_history.open_history(self.root)
            with self.assertRaises(ProtocolRefusal):
                with archive_survivor_sources._query_work_scope(1):
                    self.accessor(bounded)(("live",), "seat-a")
        self.assertEqual([], calls)
        source = epoch_carry.ArchiveProofResolver(self.root).source(self.root.tenant_home, "seat-a")
        receipt = originals[next(iter(originals))]["receipt"]
        ordinal = next(index for index, row in enumerate(source[2]) if row["id"] == receipt["id"])
        self.assertIsNotNone(epoch_carry._original_hold_cuts(source, receipt, ordinal))
        slices = []
        class WatchedSequence(list):
            def __getitem__(self, key):
                if isinstance(key, slice):
                    slices.append(True)
                return super().__getitem__(key)
        watched = tuple(WatchedSequence(axis) for axis in source)
        with self.assertRaises(archive_survivor_sources._QueryWorkExhausted):
            with archive_survivor_sources._query_work_scope(1):
                epoch_carry._original_hold_cuts(watched, receipt, ordinal)
        self.assertEqual([], slices, "exhausted original-cut scan reached a source slice")
        # This legacy-only handle can fit metadata/source DATA while the
        # expanded original-state/artifact result cannot fit the same ceiling.
        cap = wire_size(source) + 1
        self.assertLess(cap, wire_size(expected), "fixture does not distinguish source and result budgets")
        with mock.patch.object(epoch_history, "MAX_QUERY_RESULT_BYTES", cap),\
                mock.patch.object(adapter, "deepcopy", premature):
            bounded = epoch_history.open_history(self.root)
            with self.assertRaises(ProtocolRefusal):
                self.accessor(bounded)(("live",), "seat-a")
        self.assertEqual([], calls)


    def test_source_rejects_custom_coordinate_equality_before_comparison(self):
        context = epoch_history.open_history(self.root)
        self.assertTrue(context.source(("live",), "seat-a")[0])
        local = context.epochs[0].coordinate
        self.assertEqual(context.source(local, "seat-a"),
                         context.source(tuple(list(local)), "seat-a"))
        calls = []
        class BorrowedString(str):
            def __eq__(self, other):
                calls.append(True)
                raise AssertionError("source dispatched caller coordinate equality")
        for coordinate in ((BorrowedString("live"),),
                           ("archive", BorrowedString(local[1]), local[2])):
            with self.subTest(coordinate=repr(coordinate)):
                with self.assertRaises(IntegrityFailure):
                    context.source(coordinate, "seat-a")
        self.assertEqual([], calls)

    def test_projection_class_alias_substitution_refuses_before_class_member_lookup(self):
        from floati import wake_hold
        self.two_holds()
        expected = self.native_oracle()
        context = epoch_history.open_history(self.root)
        method = self.accessor(context)
        self.assertEqual(expected, method(("live",), "seat-a"))
        adapter = importlib.import_module("floati.history_query_projection")
        calls = []
        def replacement(*args, **kwargs):
            calls.append(True)
            raise AssertionError("substituted class alias body executed")
        for owner, name in ((adapter, "_NativeView"),
                            (epoch_carry, "_VerifiedHoldProofs"),
                            (wake_hold, "WakeItemState"),
                            (wake_hold, "WakeHoldController")):
            with self.subTest(alias=name), mock.patch.object(owner, name, replacement):
                with self.assertRaises(IntegrityFailure):
                    method(("live",), "seat-a")
            self.assertEqual([], calls)
        descriptor_calls = []
        class ChangedDescriptor:
            def __get__(self, instance, owner):
                descriptor_calls.append(True)
                raise AssertionError("contract dispatched a substituted method descriptor")
        with self.subTest(alias="_NativeView._source descriptor"),\
                mock.patch.object(adapter._NativeView, "_source", ChangedDescriptor()):
            with self.assertRaises(IntegrityFailure):
                method(("live",), "seat-a")
        self.assertEqual([], descriptor_calls)

    def test_existing_finite_class_aliases_refuse_before_member_lookup(self):
        from floati import cursor, history_query_join
        context = epoch_history.open_history(self.root)
        method = context.source
        expected = method(("live",), "seat-a")
        self.assertTrue(expected[0])
        calls = []
        def replacement(*args, **kwargs):
            calls.append(True)
            raise AssertionError("substituted finite reader class executed")
        for owner, name in ((archive_survivor_sources, "_QueryAllowance"),
                            (epoch_history, "EpochArchive"),
                            (history_query_join, "_Work"),
                            (cursor, "SparseCursor")):
            with self.subTest(alias=name), mock.patch.object(owner, name, replacement):
                with self.assertRaises(IntegrityFailure):
                    method(("live",), "seat-a")
        self.assertEqual([], calls)

    def test_existing_finite_member_descriptors_refuse_without_execution(self):
        from floati import cursor, history_query_join
        context = epoch_history.open_history(self.root)
        method = context.source
        self.assertTrue(method(("live",), "seat-a")[0])
        calls = []
        class ChangedDescriptor:
            def __get__(self, instance, owner):
                calls.append(True)
                raise AssertionError("finite reader contract dispatched a substituted descriptor")
        for owner, name in ((archive_survivor_sources._QueryAllowance, "require"),
                            (epoch_history.EpochArchive, "_assert_bound"),
                            (epoch_history.EpochArchive, "receipt"),
                            (epoch_history._HistoryContext, "epochs"),
                            (epoch_history._HistoryEpoch, "boundary"),
                            (history_query_join._Work, "charge"),
                            (history_query_join._Work, "remaining"),
                            (cursor.SparseCursor, "_session_component")):
            with self.subTest(owner=owner.__name__, member=name),\
                    mock.patch.object(owner, name, ChangedDescriptor()):
                with self.assertRaises(IntegrityFailure):
                    method(("live",), "seat-a")
        self.assertEqual([], calls)

    def test_native_view_binding_kind_change_refuses_before_saved_projection_dispatch(self):
        self.two_holds()
        expected = self.native_oracle()
        adapter = importlib.import_module("floati.history_query_projection")
        driver = adapter.project_native
        armed, calls = [False], []
        def observed(*args, **kwargs):
            if armed[0]:
                calls.append(True)
            return driver(*args, **kwargs)
        with mock.patch.object(adapter, "project_native", observed):
            context = epoch_history.open_history(self.root)
            method = self.accessor(context)
            self.assertEqual(expected, method(("live",), "seat-a"))
            original = vars(adapter._NativeView)["_source"]
            armed[0] = True
            for wrapper in (staticmethod, classmethod):
                calls.clear()
                with self.subTest(binding=wrapper.__name__),\
                        mock.patch.object(adapter._NativeView, "_source", wrapper(original)):
                    with self.assertRaises(IntegrityFailure):
                        method(("live",), "seat-a")
                with self.subTest(binding=wrapper.__name__, boundary="before driver"):
                    self.assertEqual([], calls)

    def test_allowance_binding_kind_change_refuses_before_saved_source_scope(self):
        scope = archive_survivor_sources._query_work_scope
        armed, calls = [False], []
        def observed(*args, **kwargs):
            if armed[0]:
                calls.append(True)
            return scope(*args, **kwargs)
        with mock.patch.object(archive_survivor_sources, "_query_work_scope", observed):
            context = epoch_history.open_history(self.root)
            method = context.source
            self.assertTrue(method(("live",), "seat-a")[0])
            original = vars(archive_survivor_sources._QueryAllowance)["require"]
            armed[0] = True
            for wrapper in (staticmethod, classmethod):
                calls.clear()
                with self.subTest(binding=wrapper.__name__), mock.patch.object(
                        archive_survivor_sources._QueryAllowance, "require", wrapper(original)):
                    with self.assertRaises(IntegrityFailure):
                        method(("live",), "seat-a")
                with self.subTest(binding=wrapper.__name__, boundary="before scope"):
                    self.assertEqual([], calls)

    def test_original_cut_reservation_pays_scans_before_all_twelve_slices(self):
        originals = self.two_holds()
        source = epoch_carry.ArchiveProofResolver(self.root).source(self.root.tenant_home, "seat-a")
        receipt = originals[next(iter(originals))]["receipt"]
        ordinal = next(index for index, row in enumerate(source[2]) if row["id"] == receipt["id"])
        before, after = epoch_carry._original_hold_cuts(source, receipt, ordinal)
        scan_work = 2 * len(source[1]) + 2 * len(source[5]) + 1
        slice_work = sum(len(axis) for axis in before) + sum(len(axis) for axis in after)
        self.assertEqual(4 * (len(before[0]) + ordinal + len(before[4])) + 8, slice_work)
        slices = []
        class WatchedSequence(list):
            def __getitem__(self, key):
                if isinstance(key, slice):
                    slices.append(True)
                return super().__getitem__(key)
        watched = tuple(WatchedSequence(axis) for axis in source)
        with self.assertRaises(archive_survivor_sources._QueryWorkExhausted):
            with archive_survivor_sources._query_work_scope(scan_work + slice_work - 1) as failed:
                epoch_carry._original_hold_cuts(watched, receipt, ordinal)
        self.assertEqual(slice_work - 1, failed.remaining,
            "the scan must finish before the insufficient exact slice reservation")
        self.assertEqual([], slices)
        with archive_survivor_sources._query_work_scope(scan_work + slice_work) as supported:
            reproduced = epoch_carry._original_hold_cuts(watched, receipt, ordinal)
            self.assertIsNotNone(reproduced)
            self.assertEqual(0, supported.remaining)
        self.assertEqual(12, len(slices))







if __name__ == "__main__":
    unittest.main()
