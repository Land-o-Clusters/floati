"""Native M1 envelope membership; Doctor is the sole migrated caller.

Source-only preparation. Native oracles precede missing-surface assertions.
Missing-function RED cannot prove the later reader/namespace/budget controls;
those branches require a watched post-API bank before claiming interception.
"""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_catalog, bus_epoch
from floati import doctor_probe, epoch_history, history_query_join, jsonl, snapshot
from floati import archive_survivor_sources as query_work
from floati.cursor import SparseCursor
from floati.doctor_probe import DoctorProbe
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS, EventLog
from floati.framing import decode_frames, encode_frame
from floati.registry import Registry
from floati.root import FloatiRoot
from tests import test_epoch_carry as native_fixture


def changed_membership_code(*args, **kwargs):
    # No closure: code substitution must reach the native reader guard,
    # rather than fail because Python cannot assign incompatible freevars.
    raise AssertionError("changed membership code executed")


class HistoryQueryMembershipTests(unittest.TestCase):
    def setUp(self):
        self.native = native_fixture.EpochCarryTests()
        self.addCleanup(self.native.doCleanups)
        self.native.setUp()
        self.native.send("retained-history-original")
        bus_epoch.roll_bus_epoch(self.native.root, actor="sender", idempotency_key="retained-history-original-roll")
        self.fixture, self.root = self.native, self.native.root

    def api(self):
        method = getattr(epoch_history, "envelope_membership", None)
        self.assertTrue(callable(method), "root-only guarded envelope membership is absent")
        return method


    def oracle(self, identity):
        """Existing native Doctor read semantics, without the proposed helper."""
        with bus_epoch.epoch_guard(self.root):
            if any(row["kind"] == "message_envelope" and row["id"] == identity
                   for row in EventLog(self.root).event_records()):
                return "live"
            for archive in snapshot._owned_epoch_archives(self.root):
                rows = jsonl.read_records_snapshot(self.root,
                    archive.relative_to(self.root.tenant_home) / "events.jsonl",
                    allowed_kinds=set(EVENT_KINDS))
                if any(row["kind"] == "message_envelope" and row["id"] == identity for row in rows):
                    return "archive"
            return "absent"

    def absent_id(self):
        # A genuinely issued native message ID, absent from the queried root.
        foreign = FloatiRoot.open_direct_home(self.fixture.base / "foreign-membership", create=True)
        Registry(foreign).register("sender", "Codex")
        Registry(foreign).register("seat-a", "Codex")
        return EventLog(foreign).send("sender", "seat-a", "floati", "a" * 40,
            "docs/evidence/membership.md", "foreign native identity")["id"]

    def owned_files(self):
        return {path.relative_to(self.root.tenant_home).as_posix(): path.read_bytes()
                for path in self.root.tenant_home.rglob("*") if path.is_file()}

    def manual_roll(self, key):
        return bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key=key)["receipt"]

    def append_future(self):
        # Same stable future-envelope framing pattern as test_skew_2; this is
        # deliberately not a current message_envelope and never a new proof.
        row = {"schema_version": 99, "id": "future-membership-kind",
            "tenant_id": self.root.tenant_id, "timestamp": "2026-10-20T20:00:00.000Z",
            "kind": "future_membership_kind"}
        path = self.root.tenant_home / "events.jsonl"
        path.write_bytes(path.read_bytes() + encode_frame(row))
        return row

    def test_membership_matches_native_live_manual_archive_and_absent(self):
        archived = self.fixture.send("membership-manual")
        retracted = self.fixture.send("membership-retracted", worker_session_id="worker-plane")
        EventLog(self.root).retract(retracted["id"], worker_session_id="worker-plane",
            reason="sent_in_error", author="sender")
        receipt = self.manual_roll("membership-manual-roll")
        live = self.fixture.send("membership-current")
        unseen = self.absent_id()
        expected = {archived["id"]: "archive", retracted["id"]: "archive",
                    live["id"]: "live", unseen: "absent"}
        for identity, status in expected.items():
            self.assertEqual(status, self.oracle(identity))
            self.assertEqual(status == "archive", DoctorProbe(self.root)._probe_was_archived(identity))
        # A real envelope frame in an unowned directory must not join lineage.
        orphan = self.root.tenant_home / "state/unowned-membership/events.jsonl"
        orphan.parent.mkdir(parents=True)
        forged_home_only = deepcopy(live)
        forged_home_only["id"] = unseen
        orphan.write_bytes(encode_frame(forged_home_only))
        self.assertEqual("absent", self.oracle(unseen))
        self.assertEqual(receipt["archive_path"], str(snapshot._owned_epoch_archives(self.root)[0]))
        lookup = self.api()
        before = self.owned_files()
        for identity, status in expected.items():
            with self.subTest(identity=identity):
                self.assertEqual(status, lookup(self.root, identity))
        self.assertEqual(before, self.owned_files())

    def test_committed_membership_preserves_live_precedence_after_two_automatic_rolls(self):
        terminal = self.fixture.send("membership-terminal", node="seat-b")
        EventLog(self.root).present("seat-b")
        SparseCursor(self.root).ack("seat-b", [terminal["id"]], acting_session_id="membership-terminal")
        carried = self.fixture.send("membership-carried")
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        unseen = self.absent_id()
        expected = {carried["id"]: "live", terminal["id"]: "archive", unseen: "absent"}
        for identity, status in expected.items():
            self.assertEqual(status, self.oracle(identity))
        context = epoch_history.open_history(self.root)
        self.assertIsNone(context.generation)
        live_source = context.source(context.live_coordinate, "seat-a")[0]
        self.assertIn(carried["id"], [row["id"] for row in live_source])
        copies = sum(any(row["id"] == carried["id"] for row in
            context.source(epoch.coordinate, "seat-a")[0]) for epoch in context.epochs)
        self.assertGreaterEqual(copies, 2, "the LIVE oracle must beat actual archived carried copies")
        lookup = self.api()
        for identity, status in expected.items():
            with self.subTest(identity=identity):
                self.assertEqual(status, lookup(self.root, identity))
                self.assertEqual(status == "archive", DoctorProbe(self.root)._probe_was_archived(identity))

    def test_doctor_automatic_carry_does_not_replace_live_probe(self):
        sends, rolls = [], []
        def sleeper(_seconds):
            rolls.append(self.fixture.automatic_roll()["receipt"])
        probe = DoctorProbe(self.root, budget_seconds=2, poll_interval_seconds=1, sleeper=sleeper)
        original = probe._send_probe
        def sent(node):
            envelope = original(node)
            sends.append(envelope)
            return envelope
        with mock.patch.object(probe, "_send_probe", sent):
            report = probe.run(["seat-a"])
        self.assertEqual(("DEAF", 2, 35),
            (report.by_node["seat-a"].verdict, report.by_node["seat-a"].elapsed_ticks, report.rc))
        self.assertEqual(2, len(rolls))
        self.assertEqual(1, len(sends), "automatic carry replaced the original native loopback probe")
        self.assertEqual("live", self.oracle(sends[0]["id"]))
        self.assertFalse(probe._probe_was_archived(sends[0]["id"]))
        self.assertEqual({sends[0]["id"]}, {row["id"] for row in EventLog(self.root).records()
            if row["note"].startswith(doctor_probe.PROBE_NOTE_MARKER)})
        self.assertEqual(("seat-a", "seat-a", "probe", "doctor-probe"),
            tuple(sends[0][key] for key in ("sender", "recipient", "repo", "doc")))

    def test_doctor_manual_archive_replaces_only_archived_probe(self):
        sends, ticks = [], []
        def sleeper(_seconds):
            ticks.append(True)
            if len(ticks) == 1:
                self.manual_roll("membership-probe-manual")
        probe = DoctorProbe(self.root, budget_seconds=2, poll_interval_seconds=1, sleeper=sleeper)
        original = probe._send_probe
        def sent(node):
            envelope = original(node)
            sends.append(envelope)
            return envelope
        with mock.patch.object(probe, "_send_probe", sent):
            report = probe.run(["seat-a"])
        self.assertEqual(("DEAF", 2, 35),
            (report.by_node["seat-a"].verdict, report.by_node["seat-a"].elapsed_ticks, report.rc))
        self.assertEqual(2, len(sends))
        self.assertNotEqual(sends[0]["id"], sends[1]["id"])
        self.assertEqual("archive", self.oracle(sends[0]["id"]))
        self.assertEqual("live", self.oracle(sends[1]["id"]))
        self.assertTrue(probe._probe_was_archived(sends[0]["id"]))
        self.assertFalse(probe._probe_was_archived(sends[1]["id"]))

    def test_doctor_malformed_commit_refuses_before_live_or_unknown_fallback(self):
        message = self.fixture.send("membership-invalid-commit-live")
        unseen = self.absent_id()
        self.assertEqual("live", self.oracle(message["id"]))
        self.assertEqual("absent", self.oracle(unseen))
        self.assertIsNone(epoch_history.open_history(self.root).generation)
        targets = (self.root.tenant_home / "events.jsonl",
            snapshot._owned_epoch_archives(self.root)[0] / "events.jsonl")
        probe = DoctorProbe(self.root)
        fallback, sent = [], []
        def legacy(*args, **kwargs):
            fallback.append(True)
            raise AssertionError("malformed authority fell back to the legacy Path walker")
        def replacement(*args, **kwargs):
            sent.append(True)
            raise AssertionError("a refused membership lookup emitted a replacement probe")
        for path in targets:
            original = path.read_bytes()
            try:
                path.write_bytes(b'{"membership_fixture":"incomplete"')
                before = self.owned_files()
                for identity in (message["id"], unseen):
                    with self.subTest(target=path.name, live=identity == message["id"]),\
                            mock.patch.object(doctor_probe, "_owned_epoch_archives", legacy, create=True),\
                            mock.patch.object(probe, "_send_probe", replacement):
                        with self.assertRaises((IntegrityFailure, ProtocolRefusal)) as caught:
                            probe._probe_was_archived(identity)
                        self.assertNotEqual("", caught.exception.code)
                self.assertEqual(before, self.owned_files())
            finally:
                path.write_bytes(original)
        self.assertEqual([], fallback)
        self.assertEqual([], sent)

    def test_membership_absence_and_record_kind_semantics_match_native_reader(self):
        message = self.fixture.send("membership-reader-shapes")
        unseen = self.absent_id()
        receipt_id = next(row["id"] for row in EventLog(self.root).event_records()
                          if row["kind"] == "bus_epoch_roll_receipt")
        for identity in (unseen, receipt_id, "not-a-message-id", ""):
            self.assertEqual("absent", self.oracle(identity))
        lookup = self.api()
        for identity in (unseen, receipt_id, "not-a-message-id", ""):
            self.assertEqual("absent", lookup(self.root, identity))
        calls = []
        class ChangedString(str):
            def __eq__(self, other):
                calls.append("equal")
                raise AssertionError("caller ID equality executed")
            def __hash__(self):
                calls.append("hash")
                raise AssertionError("caller ID hash executed")
            def __str__(self):
                calls.append("string")
                raise AssertionError("caller ID conversion executed")
        for supplied in (ChangedString(message["id"]), object(), {"id": message["id"]}):
            with self.subTest(type=type(supplied).__name__), self.assertRaises(IntegrityFailure):
                lookup(self.root, supplied)
        self.assertEqual([], calls)
        path = self.root.tenant_home / "events.jsonl"
        original = path.read_bytes()
        rows = decode_frames(original)
        malformed = next(row for row in rows if row["id"] == message["id"])
        del malformed["recipient"]  # canonical framing, invalid LIVE schema
        try:
            path.write_bytes(b"".join(encode_frame(row) for row in rows))
            # No-commit context opening is framing-only; it cannot replace the
            # required compatible LIVE reader for this permanently-false ID.
            self.assertIsNone(epoch_history.open_history(self.root).generation)
            with self.assertRaises(IntegrityFailure) as baseline:
                self.oracle("not-a-message-id")
            with self.assertRaises(IntegrityFailure) as actual:
                lookup(self.root, "not-a-message-id")
            self.assertEqual(baseline.exception.code, actual.exception.code)
        finally:
            path.write_bytes(original)
        future = self.append_future()
        self.assertEqual("absent", self.oracle("not-a-message-id"))
        self.assertEqual("absent", lookup(self.root, "not-a-message-id"))
        receipt = self.manual_roll("membership-strict-future-archive")
        archive = Path(receipt["archive_path"])
        self.assertIn(future, decode_frames((archive / "events.jsonl").read_bytes()))
        self.assertIsNone(epoch_history.open_history(self.root).generation)
        with self.assertRaises(IntegrityFailure) as baseline:
            self.oracle("not-a-message-id")
        with self.assertRaises(IntegrityFailure) as actual:
            lookup(self.root, "not-a-message-id")
        self.assertEqual(baseline.exception.code, actual.exception.code)

    def test_membership_rechecks_reader_aliases_and_changed_namespace_before_result(self):
        message = self.fixture.send("membership-readers")
        self.assertEqual("live", self.oracle(message["id"]))
        lookup = self.api()
        self.assertEqual("live", lookup(self.root, message["id"]))
        open_history = epoch_history.open_history
        helper = getattr(history_query_join, "envelope_membership", None)
        self.assertTrue(callable(helper), "bounded captured-event membership driver is absent")
        for name in ("envelope_membership", "_query_charge"):
            calls = []
            def replacement(*args, **kwargs):
                calls.append(True)
                raise AssertionError("substituted membership reader executed")
            with ExitStack() as changes:
                def admitted(root):
                    context = open_history(root)
                    changes.enter_context(mock.patch.object(history_query_join, name, replacement))
                    return context
                with mock.patch.object(epoch_history, "open_history", admitted):
                    with self.subTest(alias=name), self.assertRaises(IntegrityFailure):
                        lookup(self.root, message["id"])
            self.assertEqual([], calls)
        original_code = helper.__code__
        try:
            def admitted(root):
                context = open_history(root)
                helper.__code__ = changed_membership_code.__code__
                return context
            with mock.patch.object(epoch_history, "open_history", admitted):
                with self.assertRaises(IntegrityFailure):
                    lookup(self.root, message["id"])
        finally:
            helper.__code__ = original_code
        archive = snapshot._owned_epoch_archives(self.root)[0] / "events.jsonl"
        decoder = jsonl._decode_path_records
        for target in (self.root.tenant_home / "events.jsonl", archive):
            armed, changed = [False], []
            def decoded(*args, **kwargs):
                result = decoder(*args, **kwargs)
                if armed[0] and not changed:
                    changed.append(True)
                    replacement_path = self.fixture.base / "membership-same-byte-replacement"
                    replacement_path.write_bytes(target.read_bytes())
                    replacement_path.replace(target)
                return result
            def admitted(root):
                context = open_history(root)
                armed[0] = True
                return context
            with mock.patch.object(jsonl, "_decode_path_records", decoded),\
                    mock.patch.object(history_query_join, "_decode_path_records", decoded, create=True),\
                    mock.patch.object(epoch_history, "open_history", admitted):
                with self.subTest(changed=target.name), self.assertRaises(IntegrityFailure):
                    lookup(self.root, message["id"])
            self.assertEqual([True], changed, "the real membership decoder was not reached")

    def test_membership_combined_work_refuses_before_decode_and_scan(self):
        message = self.fixture.send("membership-budget-current")
        self.assertEqual("live", self.oracle(message["id"]))
        unseen = self.absent_id()
        lookup = self.api()
        self.assertEqual("absent", lookup(self.root, unseen))
        # Include a genuine no-envelope archive, carried duplicates, worker
        # traffic, and an unused delivery/ACK plane in the debit comparison.
        self.manual_roll("membership-empty-following")
        self.manual_roll("membership-empty-archive")
        worker = self.fixture.send("membership-worker", worker_session_id="membership-worker")
        _messages, delivery = EventLog(self.root).present("seat-a", worker_session_id="membership-worker")
        self.assertIn(worker["id"], delivery["item_ids"])
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        context = epoch_history.open_history(self.root)
        native = epoch_history._CONTEXTS[context].native
        self.assertTrue(any(not any(row["kind"] == "message_envelope" for row in
            decode_frames(next(raw for relative, plane, raw in epoch.payloads if relative == "events.jsonl")))
            for epoch in native))
        helper = getattr(history_query_join, "envelope_membership", None)
        self.assertTrue(callable(helper))
        empty_root = FloatiRoot.open_direct_home(self.fixture.base / "empty-membership", create=True)
        # Ordinary empty optional native ledgers carry no frames or authority.
        # Make unused member-name traversal exceed the empty event/source
        # debit so an added payload-count charge cannot hide behind row work.
        for plane in ("deliveries", "acks"):
            directory = empty_root.tenant_home / "receipts" / plane
            directory.mkdir(parents=True, exist_ok=True)
            for index in range(64):
                (directory / ("unused-" + str(index) + ".jsonl")).write_bytes(b"")
        self.assertEqual([], EventLog(empty_root).event_records())
        empty_context = epoch_history.open_history(empty_root)
        empty_native = epoch_history._CONTEXTS[empty_context].native
        self.assertGreaterEqual(len(empty_native[0].payloads), 128)
        totals = {}
        # These direct private-driver calls measure work on actual captured
        # DATA; no subset is promoted to admitted lineage/proof authority.
        for root, epochs in ((self.root, native), (empty_root, empty_native)):
            total_source, total_membership = 0, 0
            for epoch in epochs:
                with query_work._query_work_scope(epoch_history.MAX_QUERY_WORK) as remaining:
                    start = remaining.remaining
                    source = history_query_join.source(root, epoch, "sender", None,
                        work=history_query_join._Work(0), max_result_bytes=epoch_history.MAX_QUERY_RESULT_BYTES)
                    remaining.charge(len(source[0]))
                    source_debit = start - remaining.remaining
                with query_work._query_work_scope(epoch_history.MAX_QUERY_WORK) as remaining:
                    start = remaining.remaining
                    self.assertEqual("absent", helper(root, (epoch,), unseen,
                        work=history_query_join._Work(0), max_result_bytes=epoch_history.MAX_QUERY_RESULT_BYTES))
                    member_debit = start - remaining.remaining
                with self.subTest(coordinate=epoch.coordinate, empty=root is empty_root):
                    self.assertLessEqual(member_debit, source_debit,
                        "existing issuer census does not dominate the real membership path")
                total_source += source_debit
                total_membership += member_debit
            self.assertLessEqual(total_membership, total_source)
            with query_work._query_work_scope(epoch_history.MAX_QUERY_WORK) as remaining:
                start = remaining.remaining
                self.assertEqual("absent", helper(root, epochs, unseen,
                    work=history_query_join._Work(0), max_result_bytes=epoch_history.MAX_QUERY_RESULT_BYTES))
                whole_debit = start - remaining.remaining
            self.assertLessEqual(whole_debit, total_source)
            totals[root.tenant_home] = (total_source, whole_debit)
        self.assertEqual(2, len(totals))
        # Observe genuine paid boundaries. The wrappers delegate all native
        # admission/decoding/validation and never replace a charge/return value.
        open_history, decoder = epoch_history.open_history, jsonl._decode_path_records
        validate = history_query_join.validate_event_records
        armed, entry, boundaries = [False], [None], {}
        decoded_calls, scan_calls = [], []
        target_allowance = [None]
        def admitted(root):
            context = open_history(root)
            state = query_work._QUERY_ALLOWANCE.get()
            self.assertIsNotNone(state, "lookup failed to enclose admission in its work scope")
            if target_allowance[0] is not None:
                self.assertGreaterEqual(state.remaining, target_allowance[0])
                state.charge(state.remaining - target_allowance[0])
            entry[0] = state.remaining
            armed[0] = True
            return context
        def decoded(*args, **kwargs):
            if armed[0]:
                decoded_calls.append(True)
                boundaries.setdefault("decode", entry[0] - query_work._QUERY_ALLOWANCE.get().remaining)
            return decoder(*args, **kwargs)
        def scanned(rows):
            if armed[0]:
                scan_calls.append(True)
                boundaries.setdefault("scan", entry[0] - query_work._QUERY_ALLOWANCE.get().remaining)
                boundaries.setdefault("rows", len(rows))
            return validate(rows)
        with mock.patch.object(epoch_history, "open_history", admitted),\
                mock.patch.object(jsonl, "_decode_path_records", decoded),\
                mock.patch.object(history_query_join, "_decode_path_records", decoded, create=True),\
                mock.patch.object(history_query_join, "validate_event_records", scanned):
            self.assertEqual("absent", lookup(self.root, unseen))
            self.assertTrue(decoded_calls)
            self.assertTrue(scan_calls)
            paid_decode, paid_scan = boundaries["decode"], boundaries["scan"]
            self.assertGreater(paid_decode, 0)
            self.assertGreaterEqual(paid_scan - paid_decode, boundaries["rows"])
            before = self.owned_files()
            for boundary, paid in (("decode", paid_decode), ("scan", paid_scan)):
                for shortage in (1, 0):
                    armed[0] = False
                    decoded_calls.clear()
                    scan_calls.clear()
                    target_allowance[0] = paid - shortage
                    with self.subTest(boundary=boundary, shortage=shortage),\
                            self.assertRaises(ProtocolRefusal) as caught:
                        lookup(self.root, unseen)
                    self.assertEqual("epoch_history_query_unsupported", caught.exception.code)
                    observed = decoded_calls if boundary == "decode" else scan_calls
                    self.assertEqual(shortage == 0, bool(observed))
            self.assertEqual(before, self.owned_files())


if __name__ == "__main__":
    unittest.main()
