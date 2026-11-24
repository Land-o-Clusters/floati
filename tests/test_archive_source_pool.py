"""Source-pool format controls over genuine native messages and epoch receipts."""
import tests  # Pin HOME/XDG before importing native modules.

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati import archive_source_pool as pool
from floati.bus_epoch import roll_bus_epoch
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.jsonl import VerifiedLedgerCursor
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import WakeHoldController
from tests.temp_roots import REAL_TEMP_ROOT


DOMAINS = {"events": WakeHoldController._EVENT_DOMAIN,
           "deliveries": WakeHoldController._DELIVERY_DOMAIN,
           "acks": WakeHoldController._ACK_DOMAIN}


def digest(rows, plane):
    value = VerifiedLedgerCursor._initial_digest(DOMAINS[plane])
    for row in rows:
        value.update(encode_frame(row))
    return value.hexdigest()


class SourcePoolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("actor", "recipient", "other"):
            Registry(self.root).register(node, "Codex")
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "actor", 1,
                                                  datetime.now(timezone.utc))
        message = EventLog(self.root).send("actor", "recipient", "floati", "a" * 40,
            "AGENTS.md", "native source pool", idempotency_key="pool")
        EventLog(self.root).present("recipient")
        SparseCursor(self.root).ack("recipient", [message["id"]], acting_session_id="native")
        first = roll_bus_epoch(self.root, actor="actor", idempotency_key="pool-first")["receipt"]
        second = roll_bus_epoch(self.root, actor="actor", idempotency_key="pool-empty-manual")["receipt"]
        self.tenant = self.root.tenant_id
        self.entries = []
        self.frames = {}
        def frame(row):
            result = pool.keyed_entry("frame", self.tenant, record=row)
            if result["key"] not in self.frames:
                self.frames[result["key"]] = result
                self.entries.append(result)
            return result["key"]
        self.frame = frame
        owner1, owner2 = frame(first), frame(second)
        older = pool.keyed_entry("epoch", self.tenant, epoch_id=first["epoch_id"],
                                owner=owner1, anchor=None, predecessor=None)
        middle = pool.keyed_entry("epoch", self.tenant, epoch_id=second["epoch_id"],
                                 owner=owner2, anchor=owner1, predecessor=older["key"])
        live = pool.keyed_entry("epoch", self.tenant, epoch_id=None, owner=None,
                               anchor=owner2, predecessor=middle["key"])
        self.entries.extend([older, middle, live])
        self.epochs = [older, middle, live]
        self.axes = []
        self.cuts = []
        for epoch, home in zip(self.epochs, [Path(first["archive_path"]),
                                            Path(second["archive_path"]), self.root.tenant_home]):
            axes = {}
            for plane, relative in (("events", "events.jsonl"),
                                    ("deliveries", "receipts/deliveries/recipient.jsonl"),
                                    ("acks", "receipts/acks/recipient.jsonl")):
                path = home / relative
                rows = list(decode_frames(path.read_bytes())) if path.exists() else []
                axis = pool.keyed_entry("axis", self.tenant, epoch=epoch["key"], plane=plane,
                    recipient=None if plane == "events" else "recipient", worker_session_id=None,
                    frames=[frame(row) for row in rows], record_count=len(rows),
                    digest_domain=DOMAINS[plane], digest=digest(rows, plane))
                self.entries.append(axis)
                self.axes.append(axis)
                axes[plane] = axis
            cut = pool.keyed_entry("cut", self.tenant, epoch=epoch["key"], recipient="recipient",
                worker_session_id=None, axes={p: {"axis": a["key"], "count": a["record_count"],
                                                 "digest": a["digest"]} for p, a in axes.items()})
            self.entries.append(cut)
            self.cuts.append(cut)

    def raw(self, entries=None):
        return (b"".join(encode_frame(row) for row in sorted(
            self.entries if entries is None else entries, key=lambda row: row["key"])),)

    def refuse(self, entries):
        with self.assertRaises(IntegrityFailure) as caught:
            pool.read_pool(self.raw(entries), self.tenant)
        return caught.exception

    def altered(self, source, **changes):
        entries = deepcopy(self.entries)
        target = next(row for row in entries if row["key"] == source["key"])
        target.update(changes)
        old = target["key"]
        target["key"] = pool.entry_key(target)
        # Replace references recursively so the negative control reaches its
        # intended contract rather than failing only a stale content key.
        def replace(value):
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace(item) for item in value]
            return target["key"] if value == old else value
        # Dependent entry keys are recomputed until the DAG is stable.
        for _ in range(len(entries)):
            updated = [replace(row) if row is not target else row for row in entries]
            changed = next((row for row in updated if pool.entry_key(row) != row["key"]), None)
            if changed is None:
                return updated
            entries = updated
            old = changed["key"]
            target = changed
            target["key"] = pool.entry_key(target)
        raise AssertionError("fixture rekey did not converge")

    def test_native_roundtrip_exact_bytes_and_empty_manual_header(self):
        chunks = pool.encode_pool(self.entries)
        result = pool.read_pool(chunks, self.tenant)
        self.assertEqual(len(result.keys), len(self.entries))
        self.assertEqual(len(result.by_tag("epoch")), 3)
        for key, entry in self.frames.items():
            self.assertEqual(result.frame_bytes(key), encode_frame(entry["record"]))
            self.assertEqual(pool.keyed_entry("frame", self.tenant,
                record=deepcopy(entry["record"])), entry)

    def malformed_axis_type(self, frames):
        entries = self.altered(self.axes[0], frames=frames)
        error = None
        try:
            pool.read_pool(self.raw(entries), self.tenant)
        except Exception as failure:
            error = failure
        self.assertIsInstance(error, IntegrityFailure,
            "canonical rekeyed axis leaked a raw exception before typed validation")
        self.assertEqual("archive_source_pool_invalid", error.code)

    def test_null_axis_frames_preserve_typed_integrity_refusal(self):
        for frames in (None, 17):
            with self.subTest(frames=frames):
                self.malformed_axis_type(frames)

    def test_deterministic_order_and_read_requires_canonical_jsonl(self):
        chunks = pool.encode_pool(self.entries)
        self.assertEqual(chunks, pool.encode_pool(reversed(self.entries)))
        self.assertEqual(chunks, pool.encode_pool(deepcopy(self.entries)))
        self.assertTrue(chunks)
        noncanonical = json.dumps(self.entries[0]).encode() + b"\n"
        with self.assertRaises(IntegrityFailure):
            pool.read_pool((noncanonical,), self.tenant)

    def test_open_shape_and_wrong_tenant_refuse(self):
        self.refuse(self.altered(self.cuts[0], coverage="all"))
        with self.assertRaises(IntegrityFailure):
            pool.read_pool(self.raw(), "foreign")

    def test_dangling_and_wrong_tag_references_refuse(self):
        self.refuse(self.altered(self.axes[0], frames=["a" * 64]))
        self.refuse(self.altered(self.axes[0], frames=[self.epochs[0]["key"]]))

    def test_duplicate_entry_keys_and_duplicate_axis_frames_refuse(self):
        self.refuse(self.entries + [self.entries[0]])
        self.refuse(self.altered(self.axes[0], frames=self.axes[0]["frames"] * 2))

    def test_conflicting_native_id_refuses_even_with_distinct_content_key(self):
        source = next(row for row in self.frames.values() if row["record"]["kind"] == "message_envelope")
        record = deepcopy(source["record"])
        record["note"] = "conflicting same native ID"
        record["sha"] = "b" * 40
        failure = self.refuse(self.entries + [pool.keyed_entry("frame", self.tenant, record=record)])
        self.assertEqual(failure.code, "archive_source_pool_invalid")
        self.assertIn("conflicting", failure.detail)

    def test_native_record_validation_and_unknown_tag_refuse(self):
        record = deepcopy(next(iter(self.frames.values()))["record"])
        record["unknown"] = True
        self.refuse(self.entries + [pool.keyed_entry("frame", self.tenant, record=record)])
        unknown = deepcopy(self.entries[0])
        unknown["tag"] = "coverage"
        self.refuse(self.entries + [unknown])

    def test_digest_count_and_native_digest_domain_refuse(self):
        for changes in ({"digest": "0" * 64}, {"record_count": True},
                        {"record_count": 999}, {"digest_domain": "wrong-domain"}):
            self.refuse(self.altered(self.axes[0], **changes))

    def test_pool_envelope_counts_and_version_are_lexical_canonical_integers(self):
        # This new data format deliberately has no JSON-Schema integer claim.
        # Native record body normalization remains the native validator's job.
        for value in (1.0, True):
            self.refuse(self.altered(self.entries[0], schema_version=value))
        for value in (float(self.axes[0]["record_count"]), True, 2 ** 63 - 1,
                      float(2 ** 63 - 1)):
            self.refuse(self.altered(self.axes[0], record_count=value))
        for value in (float(self.cuts[0]["axes"]["events"]["count"]), True,
                      2 ** 63 - 1, float(2 ** 63 - 1)):
            axes = deepcopy(self.cuts[0]["axes"])
            axes["events"]["count"] = value
            self.refuse(self.altered(self.cuts[0], axes=axes))

    def test_wrong_family_and_recipient_worker_plane_refuse(self):
        self.refuse(self.altered(self.axes[1], frames=self.axes[0]["frames"]))
        self.refuse(self.altered(self.axes[1], recipient="other"))
        self.refuse(self.altered(self.axes[1], worker_session_id="other-worker"))

    def test_cut_prefix_and_typed_axes_refuse(self):
        axes = deepcopy(self.cuts[0]["axes"])
        axes["events"]["digest"] = "f" * 64
        self.refuse(self.altered(self.cuts[0], axes=axes))
        axes["events"] = axes["acks"]
        self.refuse(self.altered(self.cuts[0], axes=axes))

    def test_partial_prefix_accepted_only_with_exact_native_digest(self):
        axes = deepcopy(self.cuts[0]["axes"])
        axes["events"].update(count=0, digest=digest([], "events"))
        result = pool.read_pool(self.raw(self.altered(self.cuts[0], axes=axes)), self.tenant)
        self.assertIsNotNone(result)

    def test_missing_empty_epoch_link_and_missing_event_axis_refuse(self):
        self.refuse([row for row in self.entries if row["key"] != self.epochs[1]["key"]])
        self.refuse(self.altered(self.epochs[2], predecessor=None))
        self.refuse([row for row in self.entries if row["key"] != self.axes[3]["key"]])

    def test_returned_entries_cannot_mutate_indices(self):
        result = pool.read_pool(self.raw(), self.tenant)
        entry = result.entry(self.axes[0]["key"])
        entry["frames"].clear()
        self.assertEqual(result.entry(self.axes[0]["key"])["frames"], self.axes[0]["frames"])
        with self.assertRaises(TypeError):
            result.entries["foreign"] = b""

    def test_record_axis_collection_and_chunk_bounds_are_typed_unsupported(self):
        for name, limit in (("MAX_RECORD_BYTES", 1), ("MAX_AXIS_RECORDS", 1),
                            ("MAX_ENTRIES", 1), ("MAX_CHUNK_BYTES", 1)):
            with self.subTest(name=name), mock.patch.object(pool, name, limit):
                with self.assertRaises(ProtocolRefusal) as caught:
                    pool.encode_pool(self.entries)
                self.assertEqual(caught.exception.code, "archive_source_pool_unsupported")

    def test_chunk_packing_is_bounded_and_has_no_silent_drop(self):
        largest = max(len(encode_frame(entry)) for entry in self.entries)
        with mock.patch.object(pool, "MAX_CHUNK_BYTES", largest):
            chunks = pool.encode_pool(self.entries)
            self.assertGreater(len(chunks), 1)
            self.assertTrue(all(len(chunk) <= largest for chunk in chunks))
            self.assertEqual(len(pool.read_pool(chunks, self.tenant).keys), len(self.entries))

    def test_duplicate_json_keys_and_incomplete_lines_refuse(self):
        for raw in (b'{"key":"a","key":"b"}\n', self.raw()[0][:-1], b"\n"):
            with self.subTest(raw=raw[:40]), self.assertRaises(IntegrityFailure):
                pool.read_pool((raw,), self.tenant)

    def test_aggregate_references_bound_is_typed_unsupported(self):
        with mock.patch.object(pool, "MAX_REFERENCES", 1):
            with self.assertRaises(ProtocolRefusal) as caught:
                pool.read_pool(self.raw(), self.tenant)
        self.assertEqual(caught.exception.code, "archive_source_pool_unsupported")

    def test_reusing_one_roll_receipt_as_two_epoch_owners_refuses(self):
        # Every local edge and event anchor agrees, but R1 cannot own two epochs.
        owner = self.epochs[0]["owner"]
        oldest = self.epochs[0]
        middle = pool.keyed_entry("epoch", self.tenant, owner=owner, anchor=owner,
            epoch_id=oldest["epoch_id"], predecessor=oldest["key"])
        live = pool.keyed_entry("epoch", self.tenant, owner=None, anchor=owner,
            epoch_id=None, predecessor=middle["key"])
        entries = [self.frames[owner], oldest, middle, live]
        for epoch, refs in ((oldest, []), (middle, [owner]), (live, [owner])):
            entries.append(pool.keyed_entry("axis", self.tenant, epoch=epoch["key"],
                plane="events", recipient=None, worker_session_id=None, frames=refs,
                record_count=len(refs), digest_domain=DOMAINS["events"],
                digest=digest([self.frames[key]["record"] for key in refs], "events")))
        failure = self.refuse(entries)
        self.assertIn("owner", failure.detail)

    def test_full_prefix_digests_are_computed_once_per_axis(self):
        entries = deepcopy(self.entries)
        # Existing full cuts reuse their previously hashed axis prefixes.
        original = VerifiedLedgerCursor._initial_digest
        updates = []
        class Meter:
            def __init__(self, domain):
                self.value = original(domain)
            def update(self, raw):
                updates.append(raw)
                self.value.update(raw)
            def hexdigest(self):
                return self.value.hexdigest()
        with mock.patch.object(VerifiedLedgerCursor, "_initial_digest", Meter):
            pool.read_pool(self.raw(entries), self.tenant)
        self.assertLessEqual(len(updates), sum(axis["record_count"] for axis in self.axes))

    def test_closed_helper_rejects_unhashable_tag_as_typed_failure(self):
        try:
            pool.entry_key({"tag": []})
        except Exception as exc:
            self.assertIsInstance(exc, IntegrityFailure)
        else:
            self.fail("malformed tag was accepted")

    def test_chunk_count_overflow_is_typed_unsupported(self):
        with mock.patch.object(pool, "MAX_CHUNKS", 1):
            with self.assertRaises(ProtocolRefusal):
                pool.read_pool((self.raw()[0], self.raw()[0]), self.tenant)

    def test_native_worker_plane_legacy_receipts_without_worker_field(self):
        message = EventLog(self.root).send("actor", "recipient", "floati", "a" * 40,
            "AGENTS.md", "worker fixture", idempotency_key="worker", worker_session_id="worker-one")
        EventLog(self.root).present("recipient", worker_session_id="worker-one")
        SparseCursor(self.root).ack("recipient", [message["id"]], acting_session_id="worker-one",
                                    worker_session_id="worker-one")
        cursor = SparseCursor(self.root)
        event_rows = list(decode_frames((self.root.tenant_home / "events.jsonl").read_bytes()))
        refs = [self.frame(row) for row in event_rows]
        entries = self.altered(self.axes[6], frames=refs, record_count=len(refs),
                               digest=digest(event_rows, "events"))
        for plane, relative in (("deliveries", cursor._delivery_relative_path_for("recipient", worker_session_id="worker-one")),
                                ("acks", cursor._relative_path_for("recipient", worker_session_id="worker-one"))):
            rows = list(decode_frames((self.root.tenant_home / relative).read_bytes()))
            self.assertTrue(rows)
            self.assertTrue(all("worker_session_id" not in row for row in rows))
            frame_entries = [pool.keyed_entry("frame", self.tenant, record=row) for row in rows]
            entries.extend(frame_entries)
            entries.append(pool.keyed_entry("axis", self.tenant, epoch=self.epochs[2]["key"],
                plane=plane, recipient="recipient", worker_session_id="worker-one",
                frames=[row["key"] for row in frame_entries], record_count=len(rows),
                digest_domain=DOMAINS[plane], digest=digest(rows, plane)))
        self.assertIsNotNone(pool.read_pool(self.raw(entries), self.tenant))

    def test_native_event_index_work_is_once_per_epoch_under_empty_axis_fanout(self):
        from floati import records
        entries = deepcopy(self.entries)
        for ordinal in range(32):
            entries.append(pool.keyed_entry("axis", self.tenant, epoch=self.epochs[0]["key"],
                plane="deliveries", recipient="fanout-" + str(ordinal), worker_session_id=None,
                frames=[], record_count=0, digest_domain=DOMAINS["deliveries"],
                digest=digest([], "deliveries")))
        accesses = []
        enabled = [True]
        class CountingDict(dict):
            def __getitem__(self, key):
                if enabled[0] and key in ("kind", "id"):
                    accesses.append(key)
                return super().__getitem__(key)
        native_decode, native_validate = pool.decode_frames, records.validate_record
        def decode(raw):
            rows = native_decode(raw)
            for row in rows:
                if row.get("tag") == "frame":
                    row["record"] = CountingDict(row["record"])
            return rows
        def validate(*args, **kwargs):
            enabled[0] = False
            try:
                return CountingDict(native_validate(*args, **kwargs))
            finally:
                enabled[0] = True
        with mock.patch.object(pool, "decode_frames", decode), mock.patch.object(records, "validate_record", validate):
            result = pool.read_pool(self.raw(entries), self.tenant)
        self.assertEqual(len(result.by_tag("axis")), len(self.axes) + 32)
        self.assertGreater(len(accesses), 0, "counter must observe actual downstream records")
        # Native validation itself is excluded. Format identity, family, anchor
        # and message-index passes may each visit records a fixed number of times.
        linear_bound = 5 * len(self.frames) + 4 * sum(axis["record_count"] for axis in self.axes)
        self.assertLessEqual(len(accesses), linear_bound)

    def test_encoder_stops_generator_at_first_provable_aggregate_byte_overflow(self):
        entry = next(iter(self.frames.values()))
        size = len(encode_frame(entry))
        consumed = []
        def entries():
            for ordinal in range(2):
                consumed.append(ordinal)
                yield deepcopy(entry)  # both are genuine native frame entries
            raise AssertionError("encoder consumed past first provable collection byte overflow")
        with mock.patch.object(pool, "MAX_CHUNK_BYTES", size), mock.patch.object(pool, "MAX_CHUNKS", 1):
            try:
                pool.encode_pool(entries())
            except Exception as exc:
                self.assertIsInstance(exc, ProtocolRefusal)
                self.assertEqual(exc.code, "archive_source_pool_unsupported")
            else:
                self.fail("aggregate byte overflow was accepted")
        self.assertEqual(consumed, [0, 1])
