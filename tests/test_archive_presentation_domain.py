"""Closed presentation-domain controls from native reservation/flush fixtures."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest import mock

from floati import archive_presentation_domain as domain, archive_survivor_sources as coverage
from floati.archive_presentation_replay import replay_presentation_history
from floati.archive_survivor_sources import collect_survivor_sources
from floati.archive_survivor_pool import encode_survivor_source_pool
from floati.archive_source_pool import encode_pool
from floati.bus_epoch import epoch_guard
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger
from floati.errors import FloatiError, IntegrityFailure, ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from floati.wake_hold import wake_coordination_guard
from tests import test_archive_presentation_replay as fixture
from tests import test_archive_execution_replay as pure_fixture


class ArchivePresentationDomainTests(unittest.TestCase):
    setUp = fixture.ArchivePresentationReplayTests.setUp
    prepare = fixture.ArchivePresentationReplayTests.prepare
    pure = pure_fixture.ArchiveExecutionReplayTests.pure

    def collected(self, *, kind="flushed", rolls=0):
        if kind in {"flushed", "prepared"}:
            self.prepare(flushed=kind == "flushed")
        elif kind == "reserved_prepare":
            self.fixture.send("domain-reserved-prepare")
            with mock.patch.object(CursorPresentationLedger, "_append_reserved", side_effect=OSError("native prepare fault")):
                with self.assertRaises(OSError):
                    self.fixture.presentation()
        elif kind == "reserved_flush":
            self.fixture.send("domain-reserved-flush")
            native = CursorPresentationLedger._append_reserved
            def fail_terminal(plane, node, raw, row):
                if row["phase"] == "flushed":
                    raise OSError("native flush fault")
                return native(plane, node, raw, row)
            with mock.patch.object(CursorPresentationLedger, "_append_reserved", fail_terminal):
                with self.assertRaises(OSError):
                    self.fixture.presentation(flushed=True)
        elif kind == "initialized_empty":
            with epoch_guard(self.root, exclusive=True), wake_coordination_guard(self.root, "seat-a"):
                CursorPresentationLedger(self.root)._load("seat-a")
        if rolls:
            SparseCursor(self.root).ack("seat-a", [self.message["id"]], acting_session_id="conversation-a")
            for unused in range(rolls):
                self.fixture.automatic_roll()
        with epoch_guard(self.root, exclusive=True):
            plan = collect_survivor_sources(self.root)
        pool = encode_survivor_source_pool(plan)
        presentation = plan["sources"]["presentation"]
        expected = []
        for node in presentation["entries"]:
            physical = (self.root.path / CursorPresentationLedger.relative(node["recipient"])).read_bytes()
            consents = {group["consent"]["id"]: group["consent"] for group in node["groups"]}
            claims = {group["claim"]["id"]: group["claim"] for group in node["groups"] if group["claim"] is not None}
            replay = replay_presentation_history(pool, tenant_id=self.root.tenant_id, node_id=node["recipient"],
                witness=node["witness"], physical_bytes=physical,
                consents=list(consents.values()), claims=list(claims.values()))
            expected.append({"recipient": node["recipient"], "witness": node["witness"],
                "physical_byte_length": len(physical), "physical_sha256": hashlib.sha256(physical).hexdigest(),
                "replay": replay})
        return pool, presentation, {"schema_version": 1, "domain": "presentation_domain",
                                   "tenant_id": self.root.tenant_id, "entries": expected}

    def encoded(self, **options):
        pool, plan, expected = self.collected(**options)
        chunks = domain.encode_presentation_domain(pool, self.root.tenant_id, plan)
        return pool, plan, expected, chunks

    def rows(self, chunks):
        return [row for chunk in chunks for row in decode_frames(chunk)]

    def changed(self, chunks, edit):
        rows = deepcopy(self.rows(chunks))
        edit(rows)
        return (b"".join(encode_frame(row) for row in rows),)

    def refused(self, pool, chunks):
        error = None
        try:
            self.pure(domain.verify_presentation_domain, pool, self.root.tenant_id, chunks)
        except Exception as caught:
            error = caught
        self.assertIsInstance(error, IntegrityFailure)
        self.assertEqual("archive_presentation_domain_invalid", error.code)

    def test_flushed_acked_two_roll_cold_roundtrip_matches_native_groups(self):
        pool, unused, expected, chunks = self.encoded(rolls=2)
        actual = self.pure(domain.verify_presentation_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual(json.dumps(expected, sort_keys=True), json.dumps(actual, sort_keys=True))
        self.assertFalse(actual["entries"][0]["replay"]["uncertain"])
        self.assertTrue(actual["entries"][0]["replay"]["groups"])
        actual["entries"][0]["replay"]["groups"][0]["records"][0]["item_ids"].clear()
        self.assertEqual(expected, self.pure(domain.verify_presentation_domain, pool, self.root.tenant_id, chunks))
        self.assertTrue(all(type(chunk) is bytes and len(chunk) <= 8 * 1024 * 1024 for chunk in chunks))

    def test_unappended_reserved_prepare_keeps_empty_physical_and_effective_reservation(self):
        pool, unused, expected, chunks = self.encoded(kind="reserved_prepare")
        actual = self.pure(domain.verify_presentation_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual(expected, actual)
        node = actual["entries"][0]
        self.assertEqual(0, node["physical_byte_length"])
        self.assertEqual(["prepared"], [row["phase"] for row in node["replay"]["records"]])
        self.assertTrue(node["replay"]["uncertain"])
        self.assertEqual([node["witness"]["reserved_record"]], node["replay"]["records"])
        self.assertIsNone(node["replay"]["groups"][0]["following_delivery"])

    def test_unappended_reserved_flush_is_censused_without_synthesizing_terminal(self):
        pool, unused, expected, chunks = self.encoded(kind="reserved_flush")
        actual = self.pure(domain.verify_presentation_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual(expected, actual)
        node = actual["entries"][0]
        self.assertEqual("flushed", node["witness"]["reserved_record"]["phase"])
        self.assertEqual(["prepared"], [row["phase"] for row in node["replay"]["records"]])
        self.assertIsNone(node["replay"]["groups"][0]["following_delivery"])
        self.assertTrue(node["replay"]["uncertain"])

    def test_missing_framed_node_duplicate_scope_and_missing_group_refuse(self):
        pool, unused, unused_expected, chunks = self.encoded()
        edits = (lambda rows: rows[0].update(nodes=[]),
                 lambda rows: rows.append(deepcopy(next(row for row in rows if row["tag"] == "node"))),
                 lambda rows: rows.remove(next(row for row in rows if row["tag"] == "group")))
        for edit in edits:
            with self.subTest(edit=edits.index(edit)):
                self.refused(pool, self.changed(chunks, edit))

    def test_missing_effective_or_reserved_terminal_frame_cannot_borrow_pool(self):
        pool, unused, unused_expected, chunks = self.encoded(kind="reserved_flush")
        self.refused(pool, self.changed(chunks, lambda rows:
            next(row for row in rows if row["tag"] == "node")["records"].clear()))
        def replace_terminal(rows):
            node = next(row for row in rows if row["tag"] == "node")
            node["witness"]["reserved_record"] = node["records"][0]
        self.refused(pool, self.changed(chunks, replace_terminal))

    def test_computed_group_output_order_and_digest_are_recomputed(self):
        pool, unused, unused_expected, chunks = self.encoded()
        def change_digest(rows):
            group = next(row for row in rows if row["tag"] == "group")
            group["result"]["records"][0]["output_digest"] = "f" * 64
        edits = (change_digest,
                 lambda rows: next(row for row in rows if row["tag"] == "group").update(ordinal=99),
                 lambda rows: next(row for row in rows if row["tag"] == "group")["result"].update(eligible_ids=[]))
        for edit in edits:
            self.refused(pool, self.changed(chunks, edit))

    def test_foreign_claim_ref_missing_original_cut_and_missing_frame_refuse(self):
        pool, unused, unused_expected, chunks = self.encoded()
        def foreign_claim(rows):
            node = next(row for row in rows if row["tag"] == "node")
            node["claims"] = list(node["consents"])
        self.refused(pool, self.changed(chunks, foreign_claim))
        entries = self.rows(pool)
        no_cuts = encode_pool(row for row in entries if row["tag"] != "cut")
        self.refused(no_cuts, chunks)
        key = next(row for row in self.rows(chunks) if row["tag"] == "node")["records"][0]
        missing_frame = encode_pool(row for row in entries if row["key"] != key)
        self.refused(missing_frame, chunks)

    def test_physical_length_hash_witness_and_open_fields_are_bound(self):
        pool, unused, unused_expected, chunks = self.encoded()
        edits = (lambda node: node.update(physical_byte_length=0),
                 lambda node: node.update(physical_sha256="f" * 64),
                 lambda node: node["witness"].update(before_digest="f" * 64),
                 lambda node: node.update(extra=True))
        for edit in edits:
            self.refused(pool, self.changed(chunks, lambda rows: edit(next(row for row in rows if row["tag"] == "node"))))

    def test_encoder_rejects_caller_computed_group_replacement(self):
        pool, plan, unused_expected = self.collected()
        plan["entries"][0]["groups"][0]["eligible_ids"] = []
        error = None
        try:
            domain.encode_presentation_domain(pool, self.root.tenant_id, plan)
        except Exception as caught:
            error = caught
        self.assertIsInstance(error, IntegrityFailure)
        self.assertEqual("archive_presentation_domain_invalid", error.code)

    def test_valid_noncanonical_physical_bytes_are_unsupported_not_invented(self):
        self.prepare()
        path = self.root.path / CursorPresentationLedger.relative("seat-a")
        state_path = self.root.path / CursorPresentationLedger.witness_relative("seat-a")
        lines = path.read_bytes().splitlines(keepends=True)
        lines[0] = b" " + lines[0]
        path.write_bytes(b"".join(lines))
        state = json.loads(state_path.read_bytes())
        prefix = b"".join(lines[:-1])
        state.update(before_bytes=len(prefix), before_digest=hashlib.sha256(prefix).hexdigest())
        state_path.write_text(json.dumps(state))
        pool, plan, unused_expected = self.collected(kind=None)
        error = None
        try:
            domain.encode_presentation_domain(pool, self.root.tenant_id, plan)
        except FloatiError as caught:
            error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_presentation_domain_unsupported", error.code)

    def test_initialized_empty_scope_roundtrip_is_declared_data(self):
        pool, unused, expected, chunks = self.encoded(kind="initialized_empty")
        actual = self.pure(domain.verify_presentation_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual(expected, actual)
        self.assertEqual(["seat-a"], self.rows(chunks)[0]["nodes"])
        self.assertEqual([], actual["entries"][0]["replay"]["records"])

    def test_no_presentation_domain_has_an_explicit_empty_scope_header(self):
        pool, unused, expected, chunks = self.encoded(kind=None)
        self.assertEqual([], self.rows(chunks)[0]["nodes"])
        self.assertEqual(expected, self.pure(domain.verify_presentation_domain, pool, self.root.tenant_id, chunks))

    def test_expanded_work_and_depth_refuse_before_replay_or_encoding(self):
        pool, plan, unused_expected = self.collected()
        for controls in ({"MAX_DOMAIN_EXPANDED_NODES": 24}, {"MAX_DOMAIN_DEPTH": 8}):
            hostile = deepcopy(plan)
            graph = 0
            for unused in range(10):
                graph = [0] * 7 + [graph]
            hostile["entries"][0]["witness"]["reserved_record"] = graph
            error = None
            with mock.patch.multiple(coverage, **controls), mock.patch.object(
                    domain, "_replay", side_effect=AssertionError("replay reached before bounded preflight")) as replay:
                try:
                    domain.encode_presentation_domain(pool, self.root.tenant_id, hostile)
                except Exception as caught:
                    error = caught
            self.assertIsInstance(error, ProtocolRefusal)
            self.assertEqual("archive_presentation_domain_unsupported", error.code)
            replay.assert_not_called()

    def test_chunk_limit_and_foreign_tenant_refuse(self):
        pool, unused, unused_expected, chunks = self.encoded()
        error = None
        with mock.patch.object(domain, "MAX_CHUNK_BYTES", 1):
            try:
                domain.verify_presentation_domain(pool, self.root.tenant_id, chunks)
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_presentation_domain_unsupported", error.code)
        self.refused(pool, self.changed(chunks, lambda rows: rows[0].update(tenant_id="foreign")))

    def test_pool_cut_and_epoch_refs_are_charged_before_logical_view(self):
        pool, plan, unused_expected = self.collected()
        data = domain.read_pool(pool, self.root.tenant_id)
        self.assertTrue(data.by_tag("cut"), "native fixture needs retained original cuts")
        axis_refs = sum(1 + len(data.entry(key)["frames"]) for key in data.by_tag("axis"))
        error = None
        with mock.patch.object(domain, "MAX_REFERENCES", axis_refs), mock.patch.object(
                domain, "PoolEpochView", side_effect=AssertionError("neutral refs checked after view copying")):
            try:
                domain.encode_presentation_domain(pool, self.root.tenant_id, plan)
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_presentation_domain_unsupported", error.code)

    def test_aggregate_replay_bytes_refuse_before_pool_parsing(self):
        pool, plan, unused_expected = self.collected()
        error = None
        # The genuine pool fits the input chunk bounds, but three native/view
        # visits exceed this scaled total allowance. No parsing is admissible.
        with mock.patch.object(domain, "MAX_CHUNKS", len(pool)), mock.patch.object(
                domain, "MAX_CHUNK_BYTES", sum(map(len, pool))), mock.patch.object(
                domain, "read_pool", side_effect=AssertionError("pool parsed before byte-work refusal")) as reader:
            try:
                domain.encode_presentation_domain(pool, self.root.tenant_id, plan)
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_presentation_domain_unsupported", error.code)
        reader.assert_not_called()

    def test_entry_size_refuses_before_whole_container_encoding(self):
        unused_pool, plan, unused_expected = self.collected()
        value = plan["entries"][0]["groups"][0]
        self.assertGreater(len(encode_frame(value)), 512, "native group must exceed scaled entry allowance")
        native_encode = domain.encode_frame
        def no_container(member):
            if isinstance(member, (dict, list, tuple)):
                raise AssertionError("whole container encoded before entry bound")
            return native_encode(member)
        error = None
        with mock.patch.object(domain, "MAX_CHUNK_BYTES", 512), mock.patch.object(domain, "encode_frame", no_container):
            try:
                domain._canonical(value, domain._Budget())
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_presentation_domain_unsupported", error.code)
