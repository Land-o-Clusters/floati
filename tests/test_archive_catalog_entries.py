"""Catalog DATA controls over real native lineage and completed-roll sources."""
import tests  # Pin HOME/XDG before native imports.

from copy import deepcopy
import hashlib
import json
import unittest
from unittest import mock

from floati import archive_catalog_entries as catalog
from floati import archive_source_pool, archive_survivor_pool, archive_survivor_sources, bus_epoch
from floati.archive_container import _facts
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import encode_frame
from tests import test_epoch_carry as native_fixture


class ArchiveCatalogEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = cls()
        cls.addClassCleanup(cls.template.doCleanups)
        cls.template.set_up_native()

    def setUp(self):
        if self._testMethodName == "test_native_off_lineage_retry_is_preserved_without_becoming_a_predecessor":
            self.set_up_native()
            return
        self.fixture = self.template.fixture
        self.root = self.template.root
        self.tenant = self.template.tenant
        self.plan = self.template.plan
        self.sources = self.template.sources
        self.entries = deepcopy(self.template.entries)

    def set_up_native(self):
        self.fixture = native_fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.tenant = self.root.tenant_id
        message = self.fixture.send("catalog-manual")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="catalog-acting")
        bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="catalog-manual-roll")
        self.fixture.send("catalog-automatic")
        self.fixture.automatic_roll()
        self.refresh()

    def refresh(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            plan = archive_survivor_sources.collect_survivor_sources(self.root)
        self.plan = plan
        self.sources = archive_source_pool.read_pool(
            archive_survivor_pool.encode_survivor_source_pool(plan), self.tenant)
        frames = {self.sources.entry(key)["record"]["id"]: key
                  for key in self.sources.by_tag("frame")}
        epochs = {self.sources.entry(key)["owner"]: (key, self.sources.entry(key))
                  for key in self.sources.by_tag("epoch")
                  if self.sources.entry(key)["owner"] is not None}
        self.entries = []
        for retry in plan["sources"]["roll_retry"]["entries"]:
            receipt = retry["receipt"]
            frame = frames[receipt["id"]]
            epoch, header = epochs.get(frame, (None, None))
            self.entries.append(catalog.catalog_entry(self.tenant,
                epoch_id=receipt["epoch_id"], roll_frame=frame, source_epoch=epoch,
                predecessor=header["predecessor"] if header is not None else None,
                scope="lineage" if epoch is not None else "off_lineage",
                boundary="automatic" if "automatic" in receipt else "manual",
                rolled_at=receipt["timestamp"], facts=deepcopy(retry["facts"]),
                inventory=deepcopy(retry["inventory"]),
                payload={"state": "retained", "representation": "legacy",
                         "archive_path": receipt["archive_path"], "retirement_generation": None}))

    def raw(self, entries=None):
        return (b"".join(encode_frame(row) for row in sorted(
            self.entries if entries is None else entries, key=lambda row: row["key"])),)

    def altered(self, index=0, **changes):
        entries = deepcopy(self.entries)
        entries[index].update(changes)
        entries[index]["key"] = catalog.entry_key(entries[index])
        return entries

    def refuse(self, entries):
        with self.assertRaises(IntegrityFailure) as caught:
            catalog.read_catalog(self.raw(entries), self.tenant, self.sources)
        return caught.exception

    def test_native_manual_and_automatic_roundtrip_binds_original_wire_bytes(self):
        chunks = catalog.encode_catalog(self.entries, self.sources)
        result = catalog.read_catalog(chunks, self.tenant, self.sources)
        self.assertEqual(len(result.keys), 2)
        self.assertEqual(len(result.by_scope("lineage")), 2)
        self.assertEqual({row["boundary"] for row in self.entries}, {"manual", "automatic"})
        for row in self.entries:
            receipt = self.sources.entry(row["roll_frame"])["record"]
            self.assertEqual(result.receipt_bytes(row["key"]), encode_frame(receipt))
            self.assertEqual(result.retry_key(receipt["idempotency_key"]), row["key"])
            self.assertIn(row["key"], result.by_epoch(row["epoch_id"]))

    def test_native_catalog_size_bound_precedes_whole_entry_encoding_and_hashing(self):
        for name, operation in (("content_key", lambda: catalog.entry_key(self.entries[0])),
                ("encode", lambda: catalog.encode_catalog(self.entries, self.sources))):
            with self.subTest(operation=name), mock.patch.object(catalog, "MAX_CHUNK_BYTES", 128),\
                    mock.patch.object(catalog, "encode_frame",
                        side_effect=AssertionError("catalog container encoded before entry-size preflight")):
                with self.assertRaises(ProtocolRefusal) as refused:
                    operation()
                self.assertEqual("archive_catalog_entries_unsupported", refused.exception.code)

    def test_deterministic_chunking_and_detached_immutable_indices(self):
        chunks = catalog.encode_catalog(self.entries, self.sources)
        self.assertTrue(chunks)
        self.assertEqual(chunks, catalog.encode_catalog(reversed(self.entries), self.sources))
        result = catalog.read_catalog(self.raw(), self.tenant, self.sources)
        detached = result.entry(self.entries[0]["key"])
        detached["inventory"].clear()
        self.assertTrue(result.entry(self.entries[0]["key"])["inventory"])
        with self.assertRaises(TypeError):
            result.entries["caller"] = b"forged"

    def test_inventory_exact_native_facts_and_member_hashes_are_required(self):
        index = max(range(len(self.entries)), key=lambda i: len(self.entries[i]["inventory"]))
        original = self.entries[index]["inventory"]
        self.assertGreater(len(original), 1, "native fixture must exercise physical inventory ordering")
        self.refuse(self.altered(index, inventory=deepcopy(original) + [deepcopy(original[0])]))
        self.refuse(self.altered(index, inventory=list(reversed(original))))
        inventory = deepcopy(original)
        inventory[0]["plane"] = "acks"
        self.refuse(self.altered(index, inventory=inventory))

        for field, value in (("sha256", "0" * 64), ("byte_length", 1)):
            with self.subTest(field=field):
                inventory = deepcopy(self.entries[0]["inventory"])
                inventory[0][field] = value
                self.refuse(self.altered(inventory=inventory))
        facts = deepcopy(self.entries[0]["facts"])
        facts["archive_file_count"] += 1
        self.refuse(self.altered(facts=facts))
        facts = deepcopy(self.entries[0]["facts"])
        facts["span"]["byte_end"] += 1
        self.refuse(self.altered(facts=facts))

    def test_wrong_tenant_epoch_native_frame_and_timestamp_refuse(self):
        with self.assertRaises(IntegrityFailure):
            catalog.read_catalog(self.raw(), "foreign", self.sources)
        self.refuse(self.altered(epoch_id="epoch-" + "f" * 32))
        self.refuse(self.altered(roll_frame="f" * 64))
        event = next(key for key in self.sources.by_tag("frame")
                     if self.sources.entry(key)["record"]["kind"] == "message_envelope")
        self.refuse(self.altered(roll_frame=event))
        for timestamp in ("1999-01-01T00:00:00Z", "2026-10-16T00:00:00+01:00", "not UTC"):
            with self.subTest(timestamp=timestamp):
                self.refuse(self.altered(rolled_at=timestamp))

    def test_manual_automatic_and_predecessor_must_derive_from_original_sources(self):
        opposite = "automatic" if self.entries[0]["boundary"] == "manual" else "manual"
        self.refuse(self.altered(boundary=opposite))
        self.refuse(self.altered(predecessor="f" * 64))
        self.refuse(self.altered(source_epoch=self.entries[1]["source_epoch"]))

    def test_complete_lineage_catalog_cannot_omit_an_archived_epoch(self):
        self.refuse(self.entries[:1])

    def test_lineage_cannot_be_reclassified_as_off_lineage(self):
        self.refuse(self.altered(scope="off_lineage", source_epoch=None, predecessor=None))

    def test_native_off_lineage_retry_is_preserved_without_becoming_a_predecessor(self):
        # Relocate the current native first frame as the existing native retry
        # collector fixture does; its receipt-owned archive remains untouched.
        rolled = self.sources.entry(next(key for key in self.sources.by_tag("frame")
            if self.sources.entry(key)["record"]["kind"] == "bus_epoch_roll_receipt" and
            "automatic" in self.sources.entry(key)["record"]))["record"]
        orphan = self.root.path / "archive-catalog-off-lineage-fixture"
        orphan.mkdir()
        (orphan / "events.jsonl").write_bytes(encode_frame(rolled))
        message = self.fixture.send("new-catalog-genesis")
        (self.root.path / "events.jsonl").write_bytes(encode_frame(dict(message["message"])))
        self.refresh()
        self.assertTrue(self.entries)
        self.assertTrue(all(row["scope"] == "off_lineage" for row in self.entries))
        result = catalog.read_catalog(self.raw(), self.tenant, self.sources)
        self.assertEqual(len(result.by_scope("off_lineage")), len(self.entries))
        self.refuse(self.altered(scope="lineage", source_epoch=None))
        self.refuse(self.altered(predecessor=next(iter(self.sources.by_tag("epoch")))))

    def test_duplicate_entry_roll_identity_and_native_retry_key_refuse(self):
        self.refuse(self.entries + [deepcopy(self.entries[0])])
        # Same native receipt with another self-consistent catalog key is still
        # a duplicate original roll identity, even if a caller changes payload.
        duplicate = deepcopy(self.entries[0])
        duplicate["payload"]["retirement_generation"] = "a" * 64
        duplicate["key"] = catalog.entry_key(duplicate)
        self.refuse(self.entries + [duplicate])
        # Native completed-roll keys are globally unique, including off-lineage.
        # Replace a valid native roll frame with a distinct valid ID whose key
        # aliases another original. The source pool accepts these as DATA.
        rows = [self.sources.entry(key) for key in self.sources.keys]
        receipt = deepcopy(self.sources.entry(self.entries[0]["roll_frame"])["record"])
        receipt["id"] = "bus-epoch-roll-receipt-00000000000070008000000000000001"
        frame = archive_source_pool.keyed_entry("frame", self.tenant, record=receipt)
        rows.append(frame)
        sources = archive_source_pool.read_pool(archive_source_pool.encode_pool(rows), self.tenant)
        duplicate = deepcopy(self.entries[0])
        duplicate.update(roll_frame=frame["key"], source_epoch=None, predecessor=None, scope="off_lineage")
        duplicate["key"] = catalog.entry_key(duplicate)
        with self.assertRaises(IntegrityFailure) as caught:
            catalog.read_catalog(self.raw(self.entries + [duplicate]), self.tenant, sources)
        self.assertIn("retry", caught.exception.detail)

    def test_legacy_payload_path_and_retirement_generation_must_be_exact(self):
        for field, value in (("archive_path", "/foreign/archive"),
                             ("retirement_generation", "a" * 64), ("state", "missing")):
            with self.subTest(field=field):
                payload = deepcopy(self.entries[0]["payload"])
                payload[field] = value
                self.refuse(self.altered(payload=payload))

    def test_future_payload_representations_are_unsupported_without_authority(self):
        for representation, state in (("compressed", "retained"), ("compressed", "expired")):
            with self.subTest(representation=representation, state=state):
                payload = deepcopy(self.entries[0]["payload"])
                payload.update(representation=representation, state=state)
                with self.assertRaises(ProtocolRefusal) as caught:
                    catalog.read_catalog(self.raw(self.altered(payload=payload)), self.tenant, self.sources)
                self.assertEqual(caught.exception.code, "archive_catalog_entries_unsupported")

    def test_open_shape_duplicate_json_keys_and_noncanonical_bytes_refuse(self):
        self.refuse(self.altered(coverage="complete"))
        for raw in (b'{"key":"a","key":"b"}\n', b"\n", self.raw()[0][:-1],
                    json.dumps(self.entries[0]).encode() + b"\n"):
            with self.subTest(raw=raw[:40]), self.assertRaises(IntegrityFailure):
                catalog.read_catalog((raw,), self.tenant, self.sources)

    def test_bound_overflow_is_unsupported_without_silent_truncation(self):
        for name in ("MAX_CHUNK_BYTES", "MAX_ENTRIES", "MAX_INVENTORY_MEMBERS"):
            with self.subTest(bound=name), mock.patch.object(catalog, name, 1):
                with self.assertRaises(ProtocolRefusal) as caught:
                    catalog.encode_catalog(self.entries, self.sources)
                self.assertEqual(caught.exception.code, "archive_catalog_entries_unsupported")

    def test_encoder_refuses_first_provable_aggregate_overflow_before_generator_resumes(self):
        row = self.entries[0]
        size = len(encode_frame(row))
        consumed = []
        def entries():
            for ordinal in range(2):
                consumed.append(ordinal)
                yield deepcopy(row)
            raise AssertionError("generator consumed after proven aggregate overflow")
        with mock.patch.object(catalog, "MAX_CHUNKS", 1), mock.patch.object(catalog, "MAX_CHUNK_BYTES", size):
            try:
                catalog.encode_catalog(entries(), self.sources)
            except Exception as exc:
                self.assertIsInstance(exc, ProtocolRefusal)
                self.assertEqual(exc.code, "archive_catalog_entries_unsupported")
            else:
                self.fail("aggregate byte overflow was accepted")
        self.assertEqual(consumed, [0, 1])

    def test_bounded_chunks_preserve_complete_catalog(self):
        largest = max(len(encode_frame(row)) for row in self.entries)
        with mock.patch.object(catalog, "MAX_CHUNK_BYTES", largest):
            chunks = catalog.encode_catalog(self.entries, self.sources)
            self.assertEqual(len(chunks), 2)
            self.assertTrue(all(len(chunk) <= largest for chunk in chunks))
            self.assertEqual(len(catalog.read_catalog(chunks, self.tenant, self.sources).keys), 2)

    def test_opaque_payload_length_above_live_ledger_limit_is_supported_as_data(self):
        # This descriptor does not allocate/read payload or claim a verified
        # archive. Its native receipt/metadata shape establishes format support.
        inventory = [{"path": "events.jsonl", "plane": "events", "byte_length": 2 ** 63 - 1,
                      "sha256": hashlib.sha256(b"opaque-not-read").hexdigest()}]
        facts = _facts(inventory)
        receipt = deepcopy(self.sources.entry(self.entries[0]["roll_frame"])["record"])
        receipt.pop("automatic", None)
        receipt.update(facts)
        receipt.update(id="bus-epoch-roll-receipt-00000000000070008000000000000002",
                       epoch_id="epoch-" + facts["archive_sha256"][:32], idempotency_key="large-opaque-data")
        frame = archive_source_pool.keyed_entry("frame", self.tenant, record=receipt)
        rows = [self.sources.entry(key) for key in self.sources.keys] + [frame]
        sources = archive_source_pool.read_pool(archive_source_pool.encode_pool(rows), self.tenant)
        entry = catalog.catalog_entry(self.tenant, epoch_id=receipt["epoch_id"], roll_frame=frame["key"],
            source_epoch=None, predecessor=None, scope="off_lineage", boundary="manual",
            rolled_at=receipt["timestamp"], facts=facts, inventory=inventory,
            payload={"state": "retained", "representation": "legacy",
                     "archive_path": receipt["archive_path"], "retirement_generation": None})
        result = catalog.read_catalog(self.raw(self.entries + [entry]), self.tenant, sources)
        self.assertIn(entry["key"], result.keys)

    def test_numeric_envelope_and_inventory_fields_require_lexical_integers(self):
        for value in (True, 1.0):
            with self.subTest(schema_version=value):
                self.refuse(self.altered(schema_version=value))
        for value in (True, 1.0, -1, 2 ** 63):
            with self.subTest(byte_length=value):
                inventory = deepcopy(self.entries[0]["inventory"])
                inventory[0]["byte_length"] = value
                self.refuse(self.altered(inventory=inventory))
        facts = deepcopy(self.entries[0]["facts"])
        facts["archive_file_count"] = float(facts["archive_file_count"])
        self.refuse(self.altered(facts=facts))

    def test_source_format_indices_and_tenant_binding_are_required(self):
        with self.assertRaises(IntegrityFailure):
            catalog.read_catalog(self.raw(), self.tenant, {})
        foreign = deepcopy(self.entries[0])
        foreign["tenant_id"] = "foreign"
        foreign["key"] = catalog.entry_key(foreign)
        self.refuse([foreign] + self.entries[1:])

    def extra_retry(self, *, native_float=False):
        receipt = deepcopy(self.sources.entry(self.entries[0]["roll_frame"])["record"])
        receipt.update(id="bus-epoch-roll-receipt-00000000000070008000000000000003",
                       idempotency_key="distinct-same-epoch")
        if native_float:
            receipt["span"]["byte_start"] = 0.0
        frame = archive_source_pool.keyed_entry("frame", self.tenant, record=receipt)
        rows = [self.sources.entry(key) for key in self.sources.keys] + [frame]
        sources = archive_source_pool.read_pool(archive_source_pool.encode_pool(rows), self.tenant)
        row = deepcopy(self.entries[0])
        row.update(roll_frame=frame["key"], source_epoch=None, predecessor=None, scope="off_lineage")
        row["key"] = catalog.entry_key(row)
        return receipt, row, sources

    def test_distinct_original_retry_receipts_sharing_epoch_id_are_not_erased(self):
        receipt, row, sources = self.extra_retry()
        result = catalog.read_catalog(self.raw(self.entries + [row]), self.tenant, sources)
        self.assertEqual(len(result.by_epoch(row["epoch_id"])), 2)
        self.assertEqual(result.retry_key(receipt["idempotency_key"]), row["key"])

    def test_native_accepted_zero_span_original_frame_spelling_is_preserved(self):
        receipt, row, sources = self.extra_retry(native_float=True)
        result = catalog.read_catalog(self.raw(self.entries + [row]), self.tenant, sources)
        self.assertEqual(result.receipt_bytes(row["key"]), encode_frame(receipt))
        self.assertIn(b'"byte_start":0.0', result.receipt_bytes(row["key"]))

    def test_exact_packing_guard_remains_when_total_bytes_fit(self):
        rows = [deepcopy(self.entries[0]) for _ in range(3)]
        size = len(encode_frame(rows[0]))
        # Three entries exceed two bins when each entry is just over half a
        # bin, despite aggregate bytes fitting in their combined capacity.
        with mock.patch.object(catalog, "MAX_CHUNK_BYTES", 2 * size - 1), mock.patch.object(catalog, "MAX_CHUNKS", 2):
            with self.assertRaises(ProtocolRefusal) as caught:
                catalog.encode_catalog(rows, self.sources)
        self.assertIn("packing", caught.exception.detail)

    def test_read_chunk_and_total_inventory_bounds_are_explicit_metadata_limits(self):
        with mock.patch.object(catalog, "MAX_CHUNKS", 1):
            with self.assertRaises(ProtocolRefusal):
                catalog.read_catalog((self.raw()[0], self.raw()[0]), self.tenant, self.sources)
        with mock.patch.object(catalog, "MAX_TOTAL_INVENTORY_MEMBERS", 1):
            with self.assertRaises(ProtocolRefusal) as caught:
                catalog.read_catalog(self.raw(), self.tenant, self.sources)
        self.assertEqual(caught.exception.code, "archive_catalog_entries_unsupported")
