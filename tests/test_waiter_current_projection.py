"""Native projection reuse keeps cutoff refusals and current selection testimony."""
from __future__ import annotations

import hashlib
import copy
import json
import math
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati import cursor_presentations
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure
from floati.events import EventLog
from floati.framing import encode_frame
from floati.records import validate_record
from floati.root import FloatiRoot
from floati.wake_hold import project_wake_items
from tests import test_cursor_presentation_history as history
from tests import test_f2_cursor_claimant as claimant
from tests.test_waiter_historical_cuts import (
    A, B, FOREIGN_ITEM, MISSING_ITEM, NODE, TENANT, WORKER, WORKER_ITEM,
    NativeSource, native_eligible, record_id,
)
from tests.test_wake_hold import NOW


def current(plane, source, node, anchor):
    # The unoptimized implementation still runs a valid behavioral oracle;
    # operation-count REDs are assertions, rather than absent-seam errors.
    helper = getattr(plane, "_current_states", None)
    return (plane._states(source, node, plane.root.tenant_id) if helper is None
            else helper(source, node, anchor))


class CurrentProjectionCloneTests(unittest.TestCase):
    def clone(self, value):
        # Keep the pre-candidate work-count control behavioral, not an absent
        # helper AttributeError. Parent owns execution against both revisions.
        return getattr(cursor_presentations, "_clone_native", copy.deepcopy)(value)

    def test_native_record_graph_detaches_nested_receipts_without_generic_deepcopy(self):
        fixture = NativeSource().envelope(A).delivery(A).hold(A)
        graph = {"events": fixture.events, "deliveries": fixture.deliveries,
                 "scalars": [None, True, False, 0, 1, -0.0, 1.25, "native value"]}
        expected = copy.deepcopy(graph)
        with mock.patch.object(cursor_presentations.copy, "deepcopy", wraps=copy.deepcopy) as generic:
            detached = self.clone(graph)
        self.assertEqual(expected, detached)
        detached["events"][0]["note"] = "changed clone"
        detached["deliveries"][0]["item_ids"].append(B)
        detached["deliveries"][1]["item_ids"].clear()
        detached["scalars"].append("new scalar")
        self.assertEqual(expected, graph, "nested clone mutations reached admitted input")
        graph["events"][0]["id"] = B
        self.assertEqual(A, detached["events"][0]["id"], "later input mutation reached its clone")
        self.assertEqual(0, generic.call_count, "native JSON containers used generic graph copying")

    def test_exact_immutable_values_retain_type_and_identity(self):
        values = [None, False, True, 0, 1, -12345678901234567890, -0.0, 1.25, "native scalar"]
        detached = self.clone(values)
        self.assertIsNot(values, detached)
        for before, after in zip(values, detached):
            with self.subTest(value=before):
                self.assertIs(before, after)
                self.assertIs(type(before), type(after))
        self.assertEqual(-1.0, math.copysign(1.0, detached[6]))

    def test_native_projection_row_snapshot_avoids_generic_copy_and_nested_aliases(self):
        fixture = NativeSource().envelope(A).delivery(A).hold(A)
        source = fixture.source()
        expected = copy.deepcopy(tuple(tuple(source[axis]) for axis in (0, 2, 4)))
        with mock.patch.object(cursor_presentations.copy, "deepcopy", wraps=copy.deepcopy) as generic:
            detached = cursor_presentations._projection_rows(source)
        self.assertEqual(expected, detached)
        detached[1][0]["item_ids"].append(B)
        detached[1][1]["item_ids"].clear()
        self.assertEqual(expected[1], tuple(fixture.deliveries))
        self.assertEqual(0, generic.call_count, "ordinary row snapshots still used generic graph copying")

    def test_container_subclasses_and_unknown_children_keep_deepcopy_semantics(self):
        class TaggedList(list):
            pass

        class TaggedDict(dict):
            pass

        class TaggedInt(int):
            pass

        class Payload:
            def __init__(self):
                self.rows = [["private"]]
                self.copied = False

            def __deepcopy__(self, memo):
                result = type(self)()
                memo[id(self)] = result
                result.rows = copy.deepcopy(self.rows, memo)
                result.copied = True
                return result

        items = TaggedList([["list child"]])
        items.annotation = {"list metadata": []}
        mapping = TaggedDict(child=["dict child"])
        mapping.annotation = {"dict metadata": []}
        number = TaggedInt(7)
        number.annotation = {"integer metadata": []}
        payload = Payload()
        graph = {"list": items, "dict": mapping, "integer": number, "unknown": payload}
        detached = self.clone(graph)
        self.assertIs(type(detached["list"]), TaggedList)
        self.assertIs(type(detached["dict"]), TaggedDict)
        self.assertIs(type(detached["integer"]), TaggedInt)
        self.assertIs(type(detached["unknown"]), Payload)
        self.assertTrue(detached["unknown"].copied)
        detached["list"][0].append("clone only")
        detached["list"].annotation["list metadata"].append("clone only")
        detached["dict"]["child"].append("clone only")
        detached["dict"].annotation["dict metadata"].append("clone only")
        detached["integer"].annotation["integer metadata"].append("clone only")
        detached["unknown"].rows[0].append("clone only")
        self.assertEqual([["list child"]], items)
        self.assertEqual({"list metadata": []}, items.annotation)
        self.assertEqual({"child": ["dict child"]}, mapping)
        self.assertEqual({"dict metadata": []}, mapping.annotation)
        self.assertEqual({"integer metadata": []}, number.annotation)
        self.assertEqual([["private"]], payload.rows)

    def test_identity_shortcut_retains_type_sensitive_recursive_comparison(self):
        shared = "shared immutable value"
        self.assertTrue(cursor_presentations._same_value(shared, shared))
        original = {"version": 0, "nested": [False, {"count": 1}]}
        detached = self.clone(original)
        self.assertTrue(cursor_presentations._same_value(original, detached))
        detached["version"] = False
        self.assertFalse(cursor_presentations._same_value(original, detached))
        detached["version"] = 0
        detached["nested"][1]["count"] = True
        self.assertFalse(cursor_presentations._same_value(original, detached))

    def test_native_row_comparison_does_not_reenter_helper_for_identical_children(self):
        fixture = NativeSource().envelope(A).delivery(A).hold(A)
        source = fixture.source()
        admitted = cursor_presentations._projection_rows(source)
        with mock.patch.object(cursor_presentations, "_same_value",
                               wraps=cursor_presentations._same_value) as compared:
            self.assertTrue(cursor_presentations._same_projection_rows(source, admitted))
        self.assertGreater(compared.call_count, 0, "native rows were not compared")
        self.assertFalse(any(call.args[0] is call.args[1] for call in compared.call_args_list),
                         "comparison reentered the helper for identical child objects")
        admitted[1][0]["item_ids"].append(B)
        self.assertFalse(cursor_presentations._same_projection_rows(source, admitted))
        original = ({"schema_version": 0, "item_ids": [A]},)
        for changed in (({"schema_version": False, "item_ids": [A]},),
                        ({"schema_version": 0, "item_ids": [B]},),
                        ({"schema_version": 0, "item_ids": []},),
                        ({"schema_version": 0},),
                        [{"schema_version": 0, "item_ids": [A]}]):
            with self.subTest(changed=changed):
                self.assertFalse(cursor_presentations._same_value(original, changed))


class CurrentProjectionCutTests(unittest.TestCase):
    def test_full_valid_source_does_not_admit_an_inconsistent_joint_maximum(self):
        event = NativeSource().envelope(A).envelope(B).delivery(B)
        hold_event = NativeSource().envelope(A).envelope(B).hold(A, event_count=2)
        hold_ack = NativeSource().envelope(A).envelope(B).delivery(A).ack(A)
        hold_ack.hold(B, ack_count=1)
        ack_delivery = NativeSource().envelope(A).envelope(B).delivery(A).delivery(B).ack(B)
        for fixture, cut in ((event, (1, 1, 0)), (hold_event, (1, 1, 0)),
                             (hold_ack, (2, 2, 0)), (ack_delivery, (2, 1, 1))):
            with self.subTest(cut=cut, deliveries=fixture.deliveries):
                source = fixture.source()
                native_eligible(source, fixture.limits(), (A, B))
                with self.assertRaises(IntegrityFailure) as expected:
                    native_eligible(source, cut, (A, B))
                with self.assertRaises(IntegrityFailure) as actual:
                    cursor_presentations._HistoricalCuts(source, NODE, TENANT, cut)
                self.assertEqual((expected.exception.code, expected.exception.detail),
                                 (actual.exception.code, actual.exception.detail))

    def test_full_invalid_tail_does_not_replace_a_valid_historical_projection(self):
        fixtures = (NativeSource().envelope(A).delivery(A).ack(B),
                    NativeSource().envelope(A).delivery(A).delivery(A, recipient="charlie"))
        for fixture in fixtures:
            with self.subTest(tail=fixture.limits()):
                source = fixture.source()
                with self.assertRaises(IntegrityFailure):
                    native_eligible(source, fixture.limits(), (A,))
                index = cursor_presentations._HistoricalCuts(source, NODE, TENANT, (1, 1, 0))
                self.assertEqual([A], index.eligible((1, 1, 0), (A,)))

    def test_repeated_hold_digest_must_cover_named_events_and_empty_delivery_needs_none(self):
        fixture = NativeSource().envelope(A).envelope(B).hold(A, event_count=2)
        source = list(fixture.source())
        # Native projector semantics permit a matching occurrence only when
        # that occurrence covers the named message, even if an earlier one exists.
        source[1] = (source[1][2], source[1][1], source[1][2])
        source = tuple(source)
        native_eligible(source, fixture.limits(), (A, B))
        with self.assertRaises(IntegrityFailure) as expected:
            native_eligible(source, (1, 1, 0), (A,))
        with self.assertRaises(IntegrityFailure) as actual:
            cursor_presentations._HistoricalCuts(source, NODE, TENANT, (1, 1, 0))
        self.assertEqual((expected.exception.code, expected.exception.detail),
                         (actual.exception.code, actual.exception.detail))
        empty = NativeSource().envelope(A).delivery()
        index = cursor_presentations._HistoricalCuts(empty.source(), NODE, TENANT, (0, 1, 0))
        self.assertEqual([], index.eligible((0, 1, 0), (A,)))

    def test_exact_source_reuse_equals_native_states_and_presentation_counts(self):
        fixture = NativeSource().envelope(A).envelope(B).envelope(MISSING_ITEM)
        fixture.envelope(WORKER_ITEM, session=WORKER).envelope(FOREIGN_ITEM, recipient="charlie")
        fixture.delivery(A).delivery(A).delivery(B).ack(B).retract_worker()
        source = fixture.source()
        expected = cursor_presentations.CursorPresentationLedger._states(source, NODE, TENANT)
        self.assertEqual([(A, "held", 2), (B, "acknowledged", 1), (MISSING_ITEM, "fresh", 0)],
                         [(item.message["id"], item.state, item.presentation_count) for item in expected])
        with tempfile.TemporaryDirectory() as directory:
            plane = cursor_presentations.CursorPresentationLedger(FloatiRoot.open(Path(directory), TENANT))
            with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
                self.assertEqual(expected, current(plane, source, NODE, "scratch-genesis"))
                self.assertEqual(expected, current(plane, source, NODE, "scratch-genesis"))
            self.assertEqual(1, native.call_count, "the exact admitted source was projected twice")

    def test_current_tuple_is_bound_to_epoch_and_recipient(self):
        fixture = NativeSource().envelope(A).delivery(A)
        source = fixture.source()
        with tempfile.TemporaryDirectory() as directory:
            plane = cursor_presentations.CursorPresentationLedger(FloatiRoot.open(Path(directory), TENANT))
            with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
                current(plane, source, NODE, "epoch-one")
                current(plane, source, NODE, "epoch-two")
            self.assertEqual(2, native.call_count, "a tuple crossed epoch identity")
            with self.assertRaises(IntegrityFailure) as expected:
                plane._states(source, "charlie", TENANT)
            with self.assertRaises(IntegrityFailure) as actual:
                current(plane, source, "charlie", "epoch-two")
            self.assertEqual((expected.exception.code, expected.exception.detail),
                             (actual.exception.code, actual.exception.detail))


class CurrentProjectionAppendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plane = cursor_presentations.CursorPresentationLedger(
            FloatiRoot.open(Path(directory.name), TENANT))

    def test_foreign_and_worker_append_preserves_all_native_states_without_reprojection(self):
        fixture = NativeSource().envelope(A).envelope(B).delivery(A).delivery(A)
        with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
            before = current(self.plane, fixture.source(), NODE, "epoch-one")
            fixture.envelope(FOREIGN_ITEM, recipient="charlie")
            after_foreign = current(self.plane, fixture.source(), NODE, "epoch-one")
            fixture.envelope(WORKER_ITEM, session=WORKER)
            after_worker = current(self.plane, fixture.source(), NODE, "epoch-one")
        expected = self.plane._states(fixture.source(), NODE, TENANT)
        self.assertEqual(expected, before)
        self.assertEqual(expected, after_foreign)
        self.assertEqual(expected, after_worker)
        self.assertEqual([(A, "held", 2), (B, "fresh", 0)],
                         [(item.message["id"], item.state, item.presentation_count) for item in after_worker])
        self.assertEqual(1, native.call_count, "irrelevant event tails replayed the admitted ordinary projection")

    def test_mutated_returned_message_cannot_change_exact_or_append_reuse(self):
        fixture = NativeSource().envelope(A).delivery(A)
        source = fixture.source()
        expected = copy.deepcopy(self.plane._states(source, NODE, TENANT))
        exposed = current(self.plane, source, NODE, "epoch-one")
        exposed[0].message["note"] = "caller mutation"
        exposed[0].message["id"] = B
        self.assertEqual(expected, current(self.plane, source, NODE, "epoch-one"))
        fixture.envelope(FOREIGN_ITEM, recipient="charlie")
        self.assertEqual(expected, current(self.plane, fixture.source(), NODE, "epoch-one"))

    def test_historical_full_tuple_does_not_alias_exposed_source_rows(self):
        fixture = NativeSource().envelope(A).delivery(A)
        source = fixture.source()
        expected = copy.deepcopy(self.plane._states(source, NODE, TENANT))
        index = cursor_presentations._HistoricalCuts(source, NODE, TENANT, fixture.limits())
        # Mutate only the exposed row after admission, not the byte testimony.
        # The retained native result must remain the original admitted value.
        source[0][0]["note"] = "caller mutation after native admission"
        self.assertEqual(expected, index.full_states)

    def test_shrink_and_same_size_rewrite_fall_back_to_original_projection(self):
        fixture = NativeSource().envelope(A).envelope(FOREIGN_ITEM, recipient="charlie")
        with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
            current(self.plane, fixture.source(), NODE, "epoch-one")
            fixture.events.pop()
            shrunk = current(self.plane, fixture.source(), NODE, "epoch-one")
            old_note = fixture.events[0]["note"]
            fixture.events[0]["note"] = ("x" if old_note[0] != "x" else "y") + old_note[1:]
            rewritten = current(self.plane, fixture.source(), NODE, "epoch-one")
        self.assertEqual(3, native.call_count, "shrink or prefix rewrite reused old testimony")
        self.assertEqual(self.plane._states(fixture.source(), NODE, TENANT), rewritten)
        self.assertNotEqual(shrunk[0].message["note"], rewritten[0].message["note"])

    def test_mutated_source_rows_keep_original_native_validation_and_result(self):
        fixture = NativeSource().envelope(A).envelope(FOREIGN_ITEM, recipient="charlie")
        source = fixture.source()
        current(self.plane, source, NODE, "epoch-one")
        source[0][0]["note"] = "exposed source mutation"
        self.assertEqual(self.plane._states(source, NODE, TENANT),
                         current(self.plane, source, NODE, "epoch-one"))
        source[0][1]["schema_version"] = True
        with self.assertRaises(IntegrityFailure) as expected:
            self.plane._states(source, NODE, TENANT)
        with self.assertRaises(IntegrityFailure) as actual:
            current(self.plane, source, NODE, "epoch-one")
        self.assertEqual((expected.exception.code, expected.exception.detail),
                         (actual.exception.code, actual.exception.detail))

    def test_exposed_old_prefix_mutation_prevents_irrelevant_append_reuse(self):
        fixture = NativeSource().envelope(A)
        current(self.plane, fixture.source(), NODE, "epoch-one")
        fixture.envelope(FOREIGN_ITEM, recipient="charlie")
        source = fixture.source()
        source[0][0]["note"] = "mutated admitted row with unchanged prefix digest"
        with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
            actual = current(self.plane, source, NODE, "epoch-one")
        self.assertEqual(1, native.call_count, "an exposed old-prefix mutation reused stale ordinary states")
        self.assertEqual(self.plane._states(source, NODE, TENANT), actual)

    def test_invalid_or_duplicate_exposed_foreign_suffix_keeps_native_refusal(self):
        for mutation in ("schema", "duplicate"):
            with self.subTest(mutation=mutation):
                fixture = NativeSource().envelope(A)
                current(self.plane, fixture.source(), NODE, "epoch-one")
                fixture.envelope(FOREIGN_ITEM, recipient="charlie")
                source = fixture.source()
                if mutation == "schema":
                    source[0][-1]["schema_version"] = False
                else:
                    source[0][-1]["id"] = A
                with self.assertRaises(IntegrityFailure) as expected:
                    self.plane._states(source, NODE, TENANT)
                with self.assertRaises(IntegrityFailure) as actual:
                    current(self.plane, source, NODE, "epoch-one")
                self.assertEqual((expected.exception.code, expected.exception.detail),
                                 (actual.exception.code, actual.exception.detail))

    def test_delivery_ack_and_same_count_delivery_rewrite_force_native_refusal_or_result(self):
        fixture = NativeSource().envelope(A)
        with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
            current(self.plane, fixture.source(), NODE, "epoch-one")
            fixture.delivery(A)
            held = current(self.plane, fixture.source(), NODE, "epoch-one")
            fixture.ack(A)
            acknowledged = current(self.plane, fixture.source(), NODE, "epoch-one")
            fixture.deliveries[0]["item_ids"] = [B]
            with self.assertRaises(IntegrityFailure) as actual:
                current(self.plane, fixture.source(), NODE, "epoch-one")
        self.assertEqual(4, native.call_count)
        self.assertEqual(("held", 1), (held[0].state, held[0].presentation_count))
        self.assertEqual("acknowledged", acknowledged[0].state)
        with self.assertRaises(IntegrityFailure) as expected:
            self.plane._states(fixture.source(), NODE, TENANT)
        self.assertEqual((expected.exception.code, expected.exception.detail),
                         (actual.exception.code, actual.exception.detail))


class CurrentProjectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.h = history.CursorPresentationHistoryTests("runTest")
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()
        self.root, self.node = self.h.root, self.h.node

    def append_bad_ack(self):
        row = validate_record({"schema_version": 0, "id": record_id("ack-", 999),
            "tenant_id": self.root.tenant_id, "timestamp": NOW, "kind": "ack_receipt",
            "recipient": self.node, "item_ids": [MISSING_ITEM]}, self.root.tenant_id,
            {"ack_receipt"}, integrity=True)
        path = self.root.resolve_relative(Path("receipts/acks") / (self.node + ".jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as output:
            output.write(encode_frame(row))

    def test_cold_history_and_current_selection_share_one_native_projection(self):
        old, _ = self.h.publish("one-projector-cold")
        plane = cursor_presentations.CursorPresentationLedger(self.root)
        with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
            with self.h.selected(plane=plane) as (_, selection):
                self.assertEqual([], selection["fresh_messages"])
                self.assertEqual([old["id"]], [item["item_id"] for item in selection["held_items"]])
                self.assertEqual(1, selection["held_items"][0]["presentation_count"])
        self.assertEqual(1, native.call_count, "historical and current replay duplicated the full projector")

    def test_unreferenced_bad_tail_read_succeeds_and_select_keeps_native_refusal(self):
        _, rows = self.h.publish("bad-tail-after-native-history")
        self.append_bad_ack()
        plane = cursor_presentations.CursorPresentationLedger(self.root)
        self.assertEqual(rows, plane.read(self.node))
        source = plane._sources(self.root.tenant_home, self.node)
        with self.assertRaises(IntegrityFailure) as expected:
            plane._states(source, self.node, self.root.tenant_id)
        with self.assertRaises(IntegrityFailure) as actual:
            with self.h.selected(plane=plane):
                self.fail("bad current ACK tail was selected")
        self.assertEqual((expected.exception.code, expected.exception.detail),
                         (actual.exception.code, actual.exception.detail))

    def test_historical_consent_refusal_precedes_deferred_full_tail_failure(self):
        _, rows = self.h.publish("consent-before-bad-current-tail")
        self.append_bad_ack()
        absent = rows[0]["consent_receipt_id"][:-1] + ("0" if rows[0]["consent_receipt_id"][-1] != "0" else "1")
        changed = [dict(row, consent_receipt_id=absent) for row in rows]
        prefix = encode_frame(changed[0])
        self.h.history.write_bytes(prefix + encode_frame(changed[1]))
        witness = json.loads(self.h.witness.read_bytes())
        witness.update(before_bytes=len(prefix), before_digest=hashlib.sha256(prefix).hexdigest(),
                       reserved_record=changed[1])
        self.h.witness.write_text(json.dumps(witness) + "\n")
        for operation in ("read", "select"):
            plane = cursor_presentations.CursorPresentationLedger(self.root)
            with self.subTest(operation=operation), self.assertRaises(IntegrityFailure) as actual:
                if operation == "read":
                    plane.read(self.node)
                else:
                    with self.h.selected(plane=plane):
                        self.fail("forged historical consent was selected")
            self.assertEqual("presentation_history_unavailable", actual.exception.code)
            self.assertIn("native consent and claim", actual.exception.detail)

    def test_historical_cache_hit_does_not_hide_new_recipient_mail_or_same_size_rewrite(self):
        old, _ = self.h.publish("historical-cache-before-append")
        plane = cursor_presentations.CursorPresentationLedger(self.root)
        with self.h.selected(plane=plane) as (_, first):
            self.assertEqual([], first["fresh_messages"])
        fresh = self.h.c.f.send("ordinary-tail-after-historical-cache")
        with self.h.selected(plane=plane) as (_, second):
            self.assertEqual([fresh], second["fresh_messages"])
            self.assertEqual([old["id"]], [item["item_id"] for item in second["held_items"]])
        path = self.root.resolve_relative("events.jsonl")
        original = path.read_bytes()
        rows = [json.loads(line) for line in original.splitlines()]
        for row in rows:
            if row["id"] == fresh["id"]:
                row["note"] = "live pong"
        changed = b"".join(encode_frame(row) for row in rows)
        self.assertEqual(len(original), len(changed))
        self.assertNotEqual(original, changed)
        path.write_bytes(changed)
        with self.h.selected(plane=plane) as (_, third):
            self.assertEqual("live pong", third["fresh_messages"][0]["note"])
            self.assertEqual(fresh["id"], third["fresh_messages"][0]["id"])

    def test_delivery_ack_and_worker_retraction_do_not_reuse_old_current_tuple(self):
        self.h.publish("old-held-before-current-changes")
        plane = cursor_presentations.CursorPresentationLedger(self.root)
        fresh = self.h.c.f.send("fresh-before-delivery-and-ack")
        with self.h.selected(plane=plane) as (_, selection):
            self.assertEqual([fresh], selection["fresh_messages"])
        # Publish real output through the native owner. A presenterless inbox
        # write is deliberately unpresented and cannot exercise held delivery.
        from floati.bridge_publication import BridgePublication
        workspace = self.h.c.f.workspace
        previous = Path.cwd()
        try:
            os.chdir(workspace)
            publication = BridgePublication(self.root.path, self.node, workspace, claimant.A)
            with publication.capture():
                messages, pending = EventLog(self.root).present(self.node)
            self.assertIsNone(pending)
            self.assertIn(fresh, messages)
            output = io.StringIO()
            decision = json.dumps({"decision": "block", "reason": json.dumps(messages)}) + "\n"
            receipt = publication.publish(messages, decision, output)
            self.assertEqual(decision, output.getvalue())
        finally:
            os.chdir(previous)
        self.assertEqual("delivery_receipt", receipt["kind"])
        self.assertEqual([message["id"] for message in messages], receipt["item_ids"])
        with self.h.selected(plane=plane) as (_, selection):
            states = current(plane, selection["_source"], self.node, selection["epoch_anchor"])
            item = next(item for item in states if item.message["id"] == fresh["id"])
            self.assertEqual(("held", 1), (item.state, item.presentation_count))
        SparseCursor(self.root).ack(self.node, [fresh["id"]], acting_session_id=claimant.A)
        with self.h.selected(plane=plane) as (_, selection):
            self.assertEqual([], selection["fresh_messages"])
            states = current(plane, selection["_source"], self.node, selection["epoch_anchor"])
            self.assertEqual("acknowledged", next(item.state for item in states if item.message["id"] == fresh["id"]))
        log = EventLog(self.root)
        worker = log.send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/ping.md", "worker retraction control", worker_session_id=claimant.A,
            idempotency_key="current-projection-worker-retraction")
        log.retract(worker["id"], worker_session_id=claimant.A, reason="sent_in_error", author="architect")
        with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
            with self.h.selected(plane=plane) as (_, selection):
                actual = current(plane, selection["_source"], self.node, selection["epoch_anchor"])
                expected = plane._states(selection["_source"], self.node, self.root.tenant_id)
                self.assertEqual(expected, actual)
                self.assertNotIn(worker["id"], [item.message["id"] for item in actual])
        self.assertGreaterEqual(native.call_count, 2, "changed event testimony reused an old full tuple")

    def test_irrelevant_native_append_keeps_history_and_prepares_the_new_event_digest(self):
        old, _ = self.h.publish("held-before-irrelevant-tail")
        fresh = self.h.c.f.send("fresh-before-irrelevant-tail")
        plane = cursor_presentations.CursorPresentationLedger(self.root)
        with self.h.selected(plane=plane) as (_, selection):
            old_digest = selection["_source"][1][-1]
            self.assertEqual([fresh], selection["fresh_messages"])
        log = EventLog(self.root)
        log.send("architect", "architect", "floati", "a" * 40,
            "docs/evidence/ping.md", "foreign append control",
            idempotency_key="current-projection-foreign-tail")
        log.send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/ping.md", "worker append control", worker_session_id=claimant.A,
            idempotency_key="current-projection-worker-tail")
        with mock.patch.object(cursor_presentations, "project_wake_items", wraps=project_wake_items) as native:
            with self.h.selected(plane=plane) as (_, selection):
                self.assertEqual([fresh], selection["fresh_messages"])
                self.assertEqual([old["id"]], [item["item_id"] for item in selection["held_items"]])
                self.assertEqual(1, selection["held_items"][0]["presentation_count"])
                self.assertEqual(1, selection["held_items"][0]["attempt_count"])
                new_digest = selection["_source"][1][-1]
                self.assertNotEqual(old_digest, new_digest)
                prepared = plane.prepare(selection, {"followup_message": "scratch exact envelopes"})
                self.assertEqual(new_digest, prepared["event_prefix_digest"])
                self.assertEqual([fresh["id"]], prepared["item_ids"])
        self.assertEqual(0, native.call_count, "irrelevant native append reprojected current ordinary states")

    def test_globally_invalid_foreign_suffix_still_refuses_before_current_reuse(self):
        self.h.publish("held-before-invalid-foreign-tail")
        plane = cursor_presentations.CursorPresentationLedger(self.root)
        with self.h.selected(plane=plane):
            pass
        source = NativeSource().envelope(FOREIGN_ITEM, recipient="charlie")
        row = dict(source.events[0], tenant_id=self.root.tenant_id, reply_to=MISSING_ITEM)
        row = validate_record(row, self.root.tenant_id, {"message_envelope"}, integrity=True)
        with self.root.resolve_relative("events.jsonl").open("ab") as output:
            output.write(encode_frame(row))
        with self.assertRaises(IntegrityFailure) as expected:
            EventLog(self.root).strict_event_records()
        with self.assertRaises(IntegrityFailure) as actual:
            with self.h.selected(plane=plane):
                self.fail("globally invalid foreign reply was selected")
        self.assertEqual((expected.exception.code, expected.exception.detail),
                         (actual.exception.code, actual.exception.detail))

    def test_warm_exposed_source_mutation_matches_the_next_native_source_observation(self):
        fresh = self.h.c.f.send("warm-exposed-source-control")
        plane = cursor_presentations.CursorPresentationLedger(self.root)
        with self.h.selected(plane=plane) as (_, selection):
            source = selection["_source"]
            envelope = next(row for row in source[0] if row["id"] == fresh["id"])
            envelope["note"] = "exposed warm source changed in memory"
        # A source reader may return detached rows. Compare the native oracle
        # on its next observation, rather than requiring mutation propagation.
        observed = plane._sources(self.root.tenant_home, self.node)
        expected = plane._states(observed, self.node, self.root.tenant_id)
        with self.h.selected(plane=plane) as (_, selection):
            self.assertEqual([dict(item.message) for item in expected], selection["fresh_messages"])

    def test_mutated_foreign_source_schema_keeps_native_refusal_when_reader_exposes_rows(self):
        self.h.c.f.send("warm-before-foreign-source-mutation")
        foreign = EventLog(self.root).send("architect", "architect", "floati", "a" * 40,
            "docs/evidence/ping.md", "foreign schema mutation control",
            idempotency_key="current-projection-foreign-schema")
        plane = cursor_presentations.CursorPresentationLedger(self.root)
        with self.h.selected(plane=plane) as (_, selection):
            row = next(row for row in selection["_source"][0] if row["id"] == foreign["id"])
            row["schema_version"] = False  # Equal to integer zero under ordinary Python equality.
        observed = plane._sources(self.root.tenant_home, self.node)
        try:
            expected = plane._states(observed, self.node, self.root.tenant_id)
        except IntegrityFailure as expected_failure:
            with self.assertRaises(IntegrityFailure) as actual:
                with self.h.selected(plane=plane):
                    self.fail("invalid exposed foreign row bypassed native validation")
            self.assertEqual((expected_failure.code, expected_failure.detail),
                             (actual.exception.code, actual.exception.detail))
        else:
            # Detached-source implementations retain the original valid bytes.
            with self.h.selected(plane=plane) as (_, selection):
                self.assertEqual([dict(item.message) for item in expected], selection["fresh_messages"])


if __name__ == "__main__":
    unittest.main()
