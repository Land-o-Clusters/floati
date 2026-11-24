"""Native composition coverage controls, independent of persisted authority."""
import hashlib
import unittest
from unittest.mock import patch

from floati import archive_checkpoint, archive_survivor_sources, bus_epoch
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames
from floati.message_consumption import MessageConsumption
from tests import test_epoch_carry as carry_fixture

DOMAINS = {"projection", "presentation", "execution", "consumption", "roll_retry"}


class ArchiveSurvivorSourcesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = carry_fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def collect(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            return archive_survivor_sources.collect_survivor_sources(self.root)

    def tree(self):
        return {path.relative_to(self.root.path).as_posix():
                (None if path.is_dir() else path.read_bytes())
                for path in self.root.path.rglob("*")}

    def test_complete_expected_domains_include_explicit_empty_scopes(self):
        self.fixture.send("complete-coverage")
        result = self.collect()
        self.assertEqual(DOMAINS, set(result["sources"]))
        coverage = {row["domain"]: row for row in result["coverage"]}
        self.assertEqual(DOMAINS, set(coverage))
        self.assertGreater(coverage["projection"]["scope_count"], 0)
        for domain in ("presentation", "execution", "consumption", "roll_retry"):
            self.assertEqual(0, coverage[domain]["scope_count"])
        self.assertTrue(result["frames"])

    def test_future_first_act_has_native_sources_without_prior_consumption(self):
        message = self.fixture.send("not-yet-acted")
        _rows, delivery = EventLog(self.root).present("seat-a")
        ack = SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="future")
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        self.assertFalse((self.root.path / "receipts/consumed").exists())
        self.assertNotIn(message["id"], [row["id"] for row in decode_frames(
            (self.root.path / "events.jsonl").read_bytes())])
        result = self.collect()
        cuts = [cut for cut in result["sources"]["projection"]["cuts"]
                if any(row["id"] == message["id"] for row in cut["source"][0])]
        self.assertEqual(1, len(cuts))
        source = cuts[0]["source"]
        self.assertEqual(message, next(row for row in source[0] if row["id"] == message["id"]))
        self.assertEqual(delivery, next(row for row in source[2] if row["id"] == delivery["id"]))
        self.assertEqual(ack, next(row for row in source[4] if row["id"] == ack["id"]))
        self.assertTrue(cuts[0]["automatic"])
        frames = {row["id"]: row for row in result["frames"]}
        self.assertIn(message["id"], frames)
        self.assertIn(ack["id"], frames)
        self.assertEqual([], result["sources"]["consumption"]["receipts"])

    def test_mutable_results_never_change_native_source_or_next_plan(self):
        self.fixture.send("copied-plan")
        first = self.collect()
        before = self.tree()
        second = self.collect()
        self.assertEqual(first, second)
        self.assertEqual(before, self.tree())
        self.assertTrue(second["frames"])
        second["frames"][0]["timestamp"] = "caller-owned mutation"
        second["coverage"].clear()
        self.assertEqual(first, self.collect())

    def test_source_changes_between_domain_collectors_refuse_complete_view(self):
        self.fixture.send("domain-gap")
        native = archive_checkpoint.collect_presentation_sources
        changed = []
        def mutate(root):
            result = native(root)
            if not changed:
                path = root.path / "state/new-unselected-dependency.json"
                path.parent.mkdir(exist_ok=True)
                path.write_text("{}\n")
                changed.append(True)
            return result
        with patch.object(archive_checkpoint, "collect_presentation_sources", mutate):
            with self.assertRaises(IntegrityFailure):
                self.collect()

    def test_exclusive_scope_is_not_optional(self):
        self.fixture.send("scope-control")
        with self.assertRaises(ProtocolRefusal):
            archive_survivor_sources.collect_survivor_sources(self.root)

    def test_native_send_receipt_subtype_keeps_exact_canonical_coverage(self):
        from floati.events import SendReceipt
        receipt = self.fixture.send("native-send-receipt-domain")
        result = self.collect()
        for entry in result["coverage"]:
            with self.subTest(domain=entry["domain"]):
                name = entry["domain"]
                expected = hashlib.sha256(b"floati-native-survivor-domain-v1\0" +
                    name.encode("ascii") + b"\0" + archive_survivor_sources._canonical(result["sources"][name])).hexdigest()
                self.assertEqual(expected, entry["sha256"])
        self.assertIs(type(receipt), SendReceipt)
        data = {"notice": receipt}
        expected = hashlib.sha256(b"floati-native-survivor-domain-v1\0execution\0" +
            archive_survivor_sources._canonical(data)).hexdigest()
        try:
            actual = archive_survivor_sources._domain_digest("execution", data)
        except ProtocolRefusal as refused:
            self.fail("native SendReceipt was refused: " + refused.code)
        self.assertEqual(expected, actual)

    def test_coverage_refuses_supported_work_limits_before_whole_container_encoding(self):
        messages = [self.fixture.send("coverage-batch-" + str(index)) for index in range(4)]
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [row["id"] for row in messages], acting_session_id="acting")
        for message in messages:
            MessageConsumption(self.root).consume("seat-a", message["id"], "acting")
        native = archive_survivor_sources._canonical
        whole_containers = []
        def observed(value):
            if isinstance(value, (dict, list, tuple)):
                whole_containers.append(True)
                raise AssertionError("unbounded whole-container serialization started")
            return native(value)
        for limits in ({"MAX_DOMAIN_CANONICAL_BYTES": 1}, {"MAX_DOMAIN_EXPANDED_NODES": 0}):
            with self.subTest(limits=limits):
                with patch.multiple(archive_survivor_sources, **limits),\
                        patch.object(archive_survivor_sources, "_canonical", observed):
                    with self.assertRaises(ProtocolRefusal) as refused:
                        self.collect()
                self.assertEqual("archive_survivor_sources_unsupported", refused.exception.code)
                self.assertEqual([], whole_containers)

    def test_shared_subtree_depth_is_checked_at_every_occurrence(self):
        def wrap(value, count):
            for _ in range(count):
                value = [value]
            return value
        shared = wrap(0, 10)
        for value in ([shared, wrap(shared, 60)], [wrap(shared, 60), shared]):
            with self.subTest(shallow_first=value[0] is shared):
                with self.assertRaises(ProtocolRefusal) as refused:
                    archive_survivor_sources._domain_digest("projection", value)
                self.assertEqual("archive_survivor_sources_unsupported", refused.exception.code)

    def test_nonclosed_containers_refuse_before_scalar_serialization(self):
        class ForeignDict(dict):
            pass
        native = archive_survivor_sources._canonical
        def scalar_only(value):
            if isinstance(value, (dict, list, tuple)):
                raise AssertionError("container reached scalar serializer")
            return native(value)
        with patch.object(archive_survivor_sources, "_canonical", scalar_only):
            with self.assertRaises(ProtocolRefusal):
                archive_survivor_sources._domain_digest("projection", ForeignDict(rows=[0] * 4))

    def test_invalid_unicode_has_a_typed_support_refusal(self):
        with self.assertRaises(ProtocolRefusal) as refused:
            archive_survivor_sources._domain_digest("projection", chr(0xD800))
        self.assertEqual("archive_survivor_sources_unsupported", refused.exception.code)

    def test_domain_budget_precedes_repeated_frame_validation_and_encoding(self):
        self.fixture.send("early-domain-budget")
        with patch.object(archive_survivor_sources, "MAX_DOMAIN_EXPANDED_NODES", 0),\
                patch.object(archive_survivor_sources, "encode_frame",
                             side_effect=AssertionError("frame traversal preceded domain budget")):
            with self.assertRaises(ProtocolRefusal) as refused:
                self.collect()
        self.assertEqual("archive_survivor_sources_unsupported", refused.exception.code)


if __name__ == "__main__":
    unittest.main()
