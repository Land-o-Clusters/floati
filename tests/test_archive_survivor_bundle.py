"""Bundle DATA tests use actual native empty and multi-epoch source plans."""
from copy import deepcopy
import hashlib
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_survivor_bundle as bundle, bus_epoch
from floati.archive_survivor_sources import collect_survivor_sources
from floati.archive_survivor_pool import encode_survivor_source_pool
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from floati.message_consumption import MessageConsumption
from floati.wake_hold import WakeHoldController
from tests import test_epoch_carry as native_fixture


class ArchiveSurvivorBundleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def collected(self, *, populated=False):
        if populated:
            message = self.fixture.send("bundle-held-acted")
            WakeHoldController(self.root).evaluate("seat-a", idempotency_key="bundle-original-hold")
            self.fixture.presentation(flushed=True)
            SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="seen")
            self.receipt = MessageConsumption(self.root).consume("seat-a", message["id"], "acting")
            self.fixture.automatic_roll()
            self.fixture.automatic_roll()
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            plan = collect_survivor_sources(self.root)
        return encode_survivor_source_pool(plan), plan

    def encoded(self, **options):
        source, plan = self.collected(**options)
        chunks = bundle.encode_survivor_bundle(source, self.root.tenant_id, plan)
        self.assertTrue(chunks, "bundle omitted all three explicit leaf domains")
        return source, plan, chunks

    def manifest(self, chunks):
        rows = decode_frames(chunks[0])
        self.assertEqual(1, len(rows))
        return rows[0]

    def changed(self, chunks, change):
        header = deepcopy(self.manifest(chunks))
        change(header)
        return (encode_frame(header),) + chunks[1:]

    def pure(self, operation, *args):
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("cold payload read")),\
                mock.patch.object(Path, "write_bytes", side_effect=AssertionError("cold payload write")),\
                mock.patch("builtins.open", side_effect=AssertionError("cold file opened")):
            return operation(*args)

    def refused(self, source, chunks):
        with self.assertRaises(IntegrityFailure) as caught:
            self.pure(bundle.verify_survivor_bundle, source, self.root.tenant_id, chunks)
        self.assertEqual("archive_survivor_bundle_invalid", caught.exception.code)

    def test_empty_native_plan_roundtrip_keeps_three_explicit_leaf_domains(self):
        source, plan, chunks = self.encoded()
        header = self.manifest(chunks)
        self.assertEqual(list(bundle.LEAF_DOMAINS), header["leaf_domains"])
        self.assertEqual(list(bundle.LEAF_DOMAINS), [row["domain"] for row in header["leaf_chunks"]])
        result = self.pure(bundle.verify_survivor_bundle, source, self.root.tenant_id, chunks)
        self.assertEqual(plan["coverage"], result["declared_coverage"])
        self.assertEqual(plan["source_witness"], result["declared_source_witness"])
        self.assertEqual([], result["consumption"]["receipts"])
        self.assertEqual([], result["execution"]["executions"])
        self.assertEqual([], result["presentation"]["entries"])
        self.assertTrue(result["projection"]["epochs"])

    def test_native_held_presentation_acked_acted_two_roll_bundle_replays(self):
        source, plan, chunks = self.encoded(populated=True)
        result = self.pure(bundle.verify_survivor_bundle, source, self.root.tenant_id, chunks)
        self.assertEqual([self.receipt], result["consumption"]["receipts"])
        self.assertTrue(result["presentation"]["entries"][0]["replay"]["groups"])
        self.assertTrue(result["projection"]["holds"])
        self.assertEqual(3, len(result["projection"]["epochs"]))

    def test_following_chunks_are_raw_exact_leaf_bytes_without_wrapping(self):
        source, plan, chunks = self.encoded()
        expected = (bundle.encode_consumption_domain(source, self.root.tenant_id, plan["sources"]["consumption"])
                    + bundle.encode_execution_domain(source, self.root.tenant_id, plan["sources"]["execution"])
                    + bundle.encode_presentation_domain(source, self.root.tenant_id, plan["sources"]["presentation"]))
        self.assertEqual(expected, chunks[1:])
        self.assertTrue(all(type(raw) is bytes and len(raw) <= 8 * 1024 * 1024 for raw in chunks))

    def test_leaf_byte_change_refuses_before_leaf_semantics(self):
        source, unused, chunks = self.encoded()
        tampered = chunks[:1] + (chunks[1] + b" ",) + chunks[2:]
        with mock.patch.object(bundle, "verify_consumption_domain", side_effect=AssertionError("mismatched bytes reached leaf")):
            self.refused(source, tampered)

    def test_missing_extra_duplicate_or_reordered_leaf_chunks_refuse(self):
        source, unused, chunks = self.encoded()
        for case, changed in (("missing", chunks[:-1]), ("extra", chunks + chunks[-1:]),
                              ("duplicate", chunks[:2] + chunks[1:2] + chunks[2:]),
                              ("reordered", chunks[:1] + tuple(reversed(chunks[1:])))):
            with self.subTest(case=case):
                self.refused(source, changed)
        edits = (
            ("missing-leaf-descriptor", lambda h: h["leaf_chunks"].pop()),
            ("extra-leaf-descriptor", lambda h: h["leaf_chunks"].append(deepcopy(h["leaf_chunks"][-1]))),
            ("reordered-leaf-descriptors", lambda h: h["leaf_chunks"].reverse()),
            ("wrong-leaf-ordinal", lambda h: h["leaf_chunks"][0].update(ordinal=1)),
            ("reordered-leaf-domains", lambda h: h["leaf_domains"].reverse()),
            ("wrong-version", lambda h: h.update(schema_version=True)),
            ("wrong-tenant", lambda h: h.update(tenant_id="other")),
            ("wrong-domain", lambda h: h.update(domain="sealed_issuer")),
            ("open-field", lambda h: h.update(authority=True)),
            ("projection-commitment", lambda h: h.update(projection_sha256="f" * 64)),
            ("missing-coverage", lambda h: h["coverage"].pop()),
            ("duplicate-coverage", lambda h: h["coverage"].append(deepcopy(h["coverage"][0]))),
            ("unsafe-witness-path", lambda h: h["source_witness"][0].update(path="../foreign")),
            ("invalid-witness-identity", lambda h: h["source_witness"][0].update(identity=[1])),
            ("invalid-coverage-count", lambda h: h["coverage"][0].update(scope_count=True)),
        )
        for case, edit in edits:
            with self.subTest(case=case):
                self.refused(source, self.changed(chunks, edit))

    def test_cross_source_graft_and_changed_source_descriptor_refuse(self):
        source, unused, chunks = self.encoded()
        self.assertEqual([{"ordinal": index, "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for index, raw in enumerate(source)], self.manifest(chunks)["source_chunks"])
        self.fixture.send("different-pool")
        other, unused = self.collected()
        self.refused(other, chunks)
        for field, value in (("sha256", "0" * 64), ("byte_length", len(source[0]) + 1), ("ordinal", 1)):
            with self.subTest(case=field):
                self.refused(source, self.changed(chunks, lambda h: h["source_chunks"][0].update({field: value})))

    def test_same_bytes_with_changed_leaf_result_and_refreshed_descriptor_refuse(self):
        source, unused, chunks = self.encoded(populated=True)
        header = self.manifest(chunks)
        ordinal = next(index for index, row in enumerate(header["leaf_chunks"]) if row["domain"] == "consumption")
        rows = decode_frames(chunks[ordinal + 1])
        next(row for row in rows if row["tag"] == "readiness")["automatic_eligible"] = True
        # Alter an independently recomputed boolean, retaining well-formed JSON.
        next(row for row in rows if row["tag"] == "readiness")["already_consumed"] = False
        raw = b"".join(encode_frame(row) for row in rows)
        header["leaf_chunks"][ordinal].update(byte_length=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        changed = (encode_frame(header),) + chunks[1:ordinal + 1] + (raw,) + chunks[ordinal + 2:]
        self.refused(source, changed)

    def test_encoder_recomputes_all_five_native_coverage_counts_and_hashes(self):
        source, plan = self.collected(populated=True)
        self.assertEqual({"projection", "presentation", "execution", "consumption", "roll_retry"},
                         {row["domain"] for row in plan["coverage"]})
        for entry in plan["coverage"]:
            for field, value in (("scope_count", entry["scope_count"] + 1), ("sha256", "f" * 64)):
                with self.subTest(case=entry["domain"], field=field):
                    changed = deepcopy(plan)
                    next(row for row in changed["coverage"] if row["domain"] == entry["domain"])[field] = value
                    with self.assertRaises(IntegrityFailure) as refused:
                        bundle.encode_survivor_bundle(source, self.root.tenant_id, changed)
                    self.assertEqual("archive_survivor_bundle_invalid", refused.exception.code)

    def test_encoder_checks_projection_artifacts_against_pool_replay(self):
        source, plan = self.collected(populated=True)
        plan["sources"]["projection"]["holds"][0]["artifact"]["fresh_total"] += 1
        for row in plan["coverage"]:
            row["sha256"] = bundle._domain_digest(row["domain"], plan["sources"][row["domain"]])
        with self.assertRaises(IntegrityFailure):
            bundle.encode_survivor_bundle(source, self.root.tenant_id, plan)

    def test_isolated_hold_before_source_cannot_borrow_complete_axes_after_coverage_refresh(self):
        source, original = self.collected(populated=True)
        original_before = original["sources"]["projection"]["holds"][0]["before"]
        self.assertTrue(original_before[0], "native hold must retain its original event cut")
        for change in ("row", "initial_prefix"):
            with self.subTest(change=change):
                plan = deepcopy(original)
                hold = plan["sources"]["projection"]["holds"][0]
                isolated = [deepcopy(list(axis)) for axis in hold["before"]]
                # Replace this occurrence, preserving all complete source axes,
                # native frames and other aliased before/source occurrences.
                hold["before"] = isolated
                if change == "row":
                    ordinal = next(index for index, row in enumerate(isolated[0]) if row["kind"] == "message_envelope")
                    isolated[0][ordinal] = dict(isolated[0][ordinal], note="isolated before replacement")
                    self.assertNotEqual(original_before[0][ordinal], isolated[0][ordinal])
                else:
                    self.assertGreater(len(isolated[1]), 1, "initial prefix must be distinct from selected final digest")
                    isolated[1][0] = "f" * 64
                    self.assertEqual(original_before[1][-1], isolated[1][-1])
                self.assertEqual(source, encode_survivor_source_pool(original))
                for row in plan["coverage"]:
                    row["sha256"] = bundle._domain_digest(row["domain"], plan["sources"][row["domain"]])
                with self.assertRaises(IntegrityFailure) as caught:
                    bundle.encode_survivor_bundle(source, self.root.tenant_id, plan)
                self.assertEqual("archive_survivor_bundle_invalid", caught.exception.code)

    def test_encoder_cannot_borrow_source_chunks_from_another_native_plan(self):
        source, unused = self.collected()
        self.fixture.send("changed-native-source")
        unused, plan = self.collected()
        with self.assertRaises(IntegrityFailure):
            bundle.encode_survivor_bundle(source, self.root.tenant_id, plan)

    def test_cold_declared_metadata_is_data_and_never_authenticated_by_this_codec(self):
        source, unused, chunks = self.encoded()
        def change(header):
            header["source_witness"][0]["identity"][0] += 1
        changed = self.changed(chunks, change)
        result = self.pure(bundle.verify_survivor_bundle, source, self.root.tenant_id, changed)
        self.assertEqual(self.manifest(changed)["source_witness"], result["declared_source_witness"])
        self.assertNotIn("authority", result)

    def test_aggregate_repeated_source_bytes_refuse_before_projection_or_leaf(self):
        source, plan = self.collected()
        with mock.patch.object(bundle, "MAX_BYTES", sum(map(len, source))), mock.patch.object(
                bundle, "replay_projection_pool", side_effect=AssertionError("pool replay before aggregate quota")):
            with self.assertRaises(ProtocolRefusal) as caught:
                bundle.encode_survivor_bundle(source, self.root.tenant_id, plan)
        self.assertEqual("archive_survivor_bundle_unsupported", caught.exception.code)

    def test_work_refusal_precedes_leaf_encoders(self):
        source, plan = self.collected(populated=True)
        with mock.patch.object(bundle, "MAX_WORK", 1), mock.patch.object(
                bundle, "encode_consumption_domain", side_effect=AssertionError("leaf before bounded preflight")):
            with self.assertRaises(ProtocolRefusal) as caught:
                bundle.encode_survivor_bundle(source, self.root.tenant_id, plan)
        self.assertEqual("archive_survivor_bundle_unsupported", caught.exception.code)

    def test_manifest_limit_precedes_whole_container_encoding(self):
        source, plan = self.collected()
        with mock.patch.object(bundle, "MAX_CHUNK_BYTES", 512):
            with self.assertRaises(ProtocolRefusal) as caught:
                bundle.encode_survivor_bundle(source, self.root.tenant_id, plan)
        self.assertEqual("archive_survivor_bundle_unsupported", caught.exception.code)

    def test_finite_plan_and_chunks_refuse_without_consuming_generators(self):
        source, plan = self.collected()
        def endless():
            self.fail("unbounded input generator was consumed")
            yield None
        for bad_source, bad_plan in ((endless(), plan), (source, dict(plan, coverage=endless()))):
            with self.subTest(source=type(bad_source).__name__):
                with self.assertRaises(IntegrityFailure):
                    bundle.encode_survivor_bundle(bad_source, self.root.tenant_id, bad_plan)

    def test_bundle_chunk_count_and_total_byte_bounds_refuse(self):
        source, unused, chunks = self.encoded()
        for controls in ({"MAX_CHUNKS": 3}, {"MAX_BYTES": 1}):
            with self.subTest(case=controls):
                with mock.patch.multiple(bundle, **controls), self.assertRaises(ProtocolRefusal) as caught:
                    bundle.verify_survivor_bundle(source, self.root.tenant_id, chunks)
                self.assertEqual("archive_survivor_bundle_unsupported", caught.exception.code)

    def test_deep_input_refuses_before_runtime_recursion(self):
        value = 0
        for unused in range(10):
            value = [value]
        with mock.patch.object(bundle, "MAX_DEPTH", 8):
            with self.assertRaises(ProtocolRefusal) as caught:
                bundle._Budget().scan(value)
        self.assertEqual("archive_survivor_bundle_unsupported", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
