from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.jsonl import _append_frame, append_record, _read_path_records
from floati.records import segment_seal_digest, validate_record
from floati.root import FloatiRoot
from floati.run_segments import PhysicalCoordinate, SegmentConfig, SegmentedRunStore
from tests.schema_validation import SchemaValidationError, validate_json_schema


UUID = "018f7e9b3c117abc8def0123456789ab"
OPENING_ID = "run-segment-opened-" + UUID
SEAL_DIGEST = "4c89b900a7a5a82283129101a7bb3fb4940de8c25557271d26ed35e7c6472740"


class RunSegmentRecordTests(unittest.TestCase):
    """Metadata contracts reject malformed segment boundaries before persistence."""

    def opened(self, *, segment_number: int = 0, previous_seal_digest: object = None) -> dict:
        return {
            "schema_version": 1,
            "id": OPENING_ID,
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "segment_opened",
            "segment_number": segment_number,
            "first_global_ordinal": 1,
            "previous_seal_digest": previous_seal_digest,
            "max_records": 100000,
            "max_bytes": 65536,
        }

    def sealed(self) -> dict:
        return {
            "schema_version": 1,
            "id": "run-segment-sealed-018f7e9b3c127abc8def0123456789ab",
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:01:00.000Z",
            "kind": "segment_sealed",
            "segment_number": 1,
            "opening_record_id": OPENING_ID,
            "last_global_ordinal": 7,
            "record_count": 3,
            "byte_length": 900,
            "segment_sha256": "a" * 64,
            "seal_digest": SEAL_DIGEST,
        }

    def validate(self, record: dict) -> dict:
        return validate_record(record, "alpha", frozenset({record["kind"]}), integrity=False)

    def test_accepts_open_and_seal_metadata_with_hand_derived_digest(self) -> None:
        """Catches omission of either v1 segment kind or the governed seal domain."""
        opened = self.opened()
        sealed = self.sealed()
        self.assertEqual(opened, self.validate(opened))
        self.assertEqual(sealed, self.validate(sealed))
        self.assertEqual(SEAL_DIGEST, segment_seal_digest(sealed))

    def test_rejects_non_v1_and_nonexact_segment_fields(self) -> None:
        """Catches version widening or an open record that admits missing or caller-owned fields."""
        for mutate, code in (
            (lambda r: r.update(schema_version=0), "schema_version_invalid"),
            (lambda r: r.update(extra=True), "record_fields_invalid"),
            (lambda r: r.pop("max_bytes"), "record_fields_invalid"),
        ):
            with self.subTest(code=code):
                candidate = self.opened()
                mutate(candidate)
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.validate(candidate)
                self.assertEqual(code, caught.exception.code)

    def test_rejects_segment_bounds_and_predecessor_law(self) -> None:
        """Catches off-by-one limits and reversing the root-versus-continuation lineage rule."""
        later_digest = "b" * 64
        cases = (
            (self.opened(segment_number=-1), "segment_number_invalid"),
            (dict(self.opened(), first_global_ordinal=0), "first_global_ordinal_invalid"),
            (dict(self.opened(), max_records=100001), "max_records_invalid"),
            (dict(self.opened(), max_bytes=65535), "max_bytes_invalid"),
            (self.opened(previous_seal_digest=later_digest), "previous_seal_digest_invalid"),
            (self.opened(segment_number=1), "previous_seal_digest_invalid"),
            (self.opened(segment_number=1, previous_seal_digest="B" * 64), "previous_seal_digest_invalid"),
        )
        for candidate, code in cases:
            with self.subTest(code=code), self.assertRaises(ProtocolRefusal) as caught:
                self.validate(candidate)
            self.assertEqual(code, caught.exception.code)
        self.assertEqual(
            self.opened(segment_number=1, previous_seal_digest=later_digest),
            self.validate(self.opened(segment_number=1, previous_seal_digest=later_digest)),
        )

    def test_rejects_bad_seal_references_hashes_and_bounds(self) -> None:
        """Catches seal metadata that cannot bind a real opened record and exact segment bytes."""
        cases = (
            ("opening_record_id", "opened-" + UUID, "opening_record_id_invalid"),
            ("segment_sha256", "A" * 64, "segment_sha256_invalid"),
            ("seal_digest", "c" * 64, "seal_digest_invalid"),
            ("record_count", 0, "record_count_invalid"),
            ("byte_length", 64 * 1024 * 1024 + 1, "byte_length_invalid"),
            ("last_global_ordinal", 0, "last_global_ordinal_invalid"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                candidate = self.sealed()
                candidate[field] = value
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.validate(candidate)
                self.assertEqual(code, caught.exception.code)

    def test_v1_schemas_match_strict_metadata_contracts(self) -> None:
        """Catches shipped schemas that accept a wider metadata surface than the validator."""
        for name, record in (
            ("run-segment-opened-record.schema.json", self.opened()),
            ("run-segment-sealed-record.schema.json", self.sealed()),
        ):
            path = Path("schemas/v1") / name
            with self.subTest(name=name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(1, schema["properties"]["schema_version"]["const"])
                validate_json_schema(record, path)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(dict(record, extra=True), path)


class SegmentedRunStoreTests(unittest.TestCase):
    KINDS = frozenset({"registry_entry"})
    NOW = datetime(2026, 8, 9, 12, 0, 0, 123456, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")

    def record(self, node: str, *, record_id: str | None = None) -> dict:
        return {
            "schema_version": 0,
            "id": record_id or "registry-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "registry_entry",
            "node_id": node,
            "role": "worker",
            "state": "active",
        }

    def append(self, store: SegmentedRunStore, record: dict, result: object = "ok") -> object:
        return store.transact(lambda snapshot: (result, record))

    def paths(self) -> tuple[Path, Path, Path]:
        runs = self.root.resolve_relative("runs")
        return runs / "events.jsonl", runs / "segments", runs / "segments" / "events.jsonl"

    def test_inactive_legacy_read_and_transact_do_not_create_segments(self) -> None:
        legacy, segments, _ = self.paths()
        first, second = self.record("legacy"), self.record("new")
        append_record(self.root, "runs/events.jsonl", first, allowed_kinds=set(self.KINDS))
        store = SegmentedRunStore(self.root, self.KINDS)
        self.assertFalse(store.is_active())
        self.assertEqual([first], store.records())
        self.assertEqual("result", self.append(store, second, "result"))
        self.assertEqual([first, second], store.records())
        self.assertTrue(legacy.exists())
        self.assertFalse(segments.exists())

    def test_inactive_batch_and_identical_retry_remain_on_legacy_path(self) -> None:
        store = SegmentedRunStore(self.root, self.KINDS)
        first, second = self.record("first"), self.record("second")
        self.assertEqual([1, 2], store.transact_batch(lambda snapshot: ([1, 2], [first, second])))
        self.assertEqual([first, second], store.records())
        self.assertEqual("retry", self.append(store, deepcopy(first), "retry"))
        self.assertEqual([first, second], store.records())
        self.assertFalse(self.paths()[1].exists())

    def test_inactive_append_positive_control_succeeds_on_legacy(self) -> None:
        """Catches a transition lock that incorrectly blocks ordinary inactive appends."""
        store = SegmentedRunStore(self.root, self.KINDS)
        record = self.record("ordinary-inactive")
        self.assertEqual("written", self.append(store, record, "written"))
        self.assertEqual([record], _read_path_records(self.paths()[0], "alpha", self.KINDS))
        self.assertFalse(self.paths()[1].exists())

    def test_stale_inactive_operations_recheck_after_activation_transition(self) -> None:
        """Catches legacy append/read after activation metadata has become durable."""
        store = SegmentedRunStore(self.root, self.KINDS)
        legacy, _, _ = self.paths()
        legacy_record, raced_record, active_record = self.record("legacy"), self.record("raced"), self.record("active")
        append_record(self.root, "runs/events.jsonl", legacy_record, allowed_kinds=set(self.KINDS))
        checked, release = threading.Event(), threading.Event()
        original_is_active = store.is_active

        def stale_inactive() -> bool:
            checked.set()
            self.assertTrue(release.wait(2))
            return False

        append_result: list[object] = []
        with mock.patch.object(store, "is_active", side_effect=stale_inactive):
            worker = threading.Thread(target=lambda: append_result.append(self.append(store, raced_record, "raced")))
            worker.start()
            self.assertTrue(checked.wait(2))
            self.assertTrue(original_is_active() is False)
            store.activate(now=self.NOW)
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(["raced"], append_result)
        self.assertEqual([legacy_record], _read_path_records(legacy, "alpha", self.KINDS))
        self.assertEqual([legacy_record, raced_record], store.records())
        self.append(store, active_record)

        read_checked, read_release = threading.Event(), threading.Event()

        def stale_read() -> bool:
            read_checked.set()
            self.assertTrue(read_release.wait(2))
            return False

        read_result: list[list[dict]] = []
        with mock.patch.object(store, "is_active", side_effect=stale_read):
            reader = threading.Thread(target=lambda: read_result.append(store.records()))
            reader.start()
            self.assertTrue(read_checked.wait(2))
            read_release.set()
            reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual([legacy_record, raced_record, active_record], read_result[0])

    def test_activation_fsyncs_new_directory_parents_and_exact_retry_survives_failure(self) -> None:
        """Catches acknowledging activation before durable runs and segments parent entries."""
        for fail_parent in ("runs", "segments"):
            with self.subTest(fail_parent=fail_parent), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                store = SegmentedRunStore(root, self.KINDS)
                tenant_home = root.tenant_home
                runs, segments = tenant_home / "runs", tenant_home / "runs" / "segments"
                real_fsync = os.fsync
                synced: list[Path] = []

                def fail_selected_parent(fd: int) -> None:
                    inode = os.fstat(fd).st_ino
                    for path in (tenant_home, runs):
                        if path.exists() and path.stat().st_ino == inode:
                            synced.append(path)
                            if (path == tenant_home and fail_parent == "runs") or (path == runs and fail_parent == "segments"):
                                raise OSError("injected parent fsync failure")
                    real_fsync(fd)

                with mock.patch("floati.jsonl.os.fsync", side_effect=fail_selected_parent), self.assertRaises(DurabilityFailure):
                    store.activate(now=self.NOW)
                self.assertFalse((segments / "events.jsonl").exists())
                self.assertIn(tenant_home, synced)
                if fail_parent == "segments":
                    self.assertIn(runs, synced)
                store.activate(now=self.NOW)
                self.assertEqual([], store.records())

    def test_activation_preserves_legacy_bytes_and_opens_zero(self) -> None:
        legacy, segments, metadata = self.paths()
        append_record(self.root, "runs/events.jsonl", self.record("legacy"), allowed_kinds=set(self.KINDS))
        before = legacy.read_bytes()
        store = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=2))
        opened = store.activate(now=self.NOW)
        self.assertEqual(before, legacy.read_bytes())
        self.assertEqual("segment_opened", opened["kind"])
        self.assertEqual(0, opened["segment_number"])
        self.assertEqual(2, opened["first_global_ordinal"])
        self.assertEqual("2026-08-09T12:00:00.123Z", opened["timestamp"])
        self.assertTrue(metadata.exists())
        self.assertTrue((segments / "00000000.jsonl").exists())
        with self.assertRaises(ProtocolRefusal):
            store.activate(now=self.NOW)

    def test_append_rotation_batch_coordinates_lookup_and_retry(self) -> None:
        store = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=2))
        store.activate(now=self.NOW)
        one, two, three = self.record("one"), self.record("two"), self.record("three")
        self.assertEqual("one", self.append(store, one, "one"))
        self.assertEqual(["two", "three"], store.transact_batch(lambda s: (["two", "three"], [two, three])))
        self.assertEqual([one, two, three], store.records())
        self.assertEqual(PhysicalCoordinate(0, 1, 1), store.lookup(one["id"]).coordinate)
        self.assertEqual(PhysicalCoordinate(0, 2, 2), store.lookup(two["id"]).coordinate)
        self.assertEqual(PhysicalCoordinate(1, 1, 3), store.lookup(three["id"]).coordinate)
        self.assertEqual("retry", self.append(store, deepcopy(two), "retry"))
        self.assertEqual(3, len(store.records()))
        divergent = dict(two, node_id="changed")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.append(store, divergent)
        self.assertEqual("duplicate_record_id", caught.exception.code)

    def test_byte_threshold_rotates_before_frame_that_would_overflow(self) -> None:
        store = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=100000, max_bytes=65536))
        store.activate(now=self.NOW)
        records = [self.record("n" + str(i).zfill(5)) for i in range(400)]
        store.transact_batch(lambda s: (list(range(len(records))), records))
        segment_files = sorted(self.paths()[1].glob("[0-9]*.jsonl"))
        self.assertGreater(len(segment_files), 1)
        self.assertTrue(all(path.stat().st_size <= 65536 for path in segment_files))
        self.assertEqual(records, store.records())

    def test_legacy_and_segment_coordinates_are_global_and_canonical(self) -> None:
        first, second = self.record("legacy-one"), self.record("legacy-two")
        append_record(self.root, "runs/events.jsonl", first, allowed_kinds=set(self.KINDS))
        append_record(self.root, "runs/events.jsonl", second, allowed_kinds=set(self.KINDS))
        store = SegmentedRunStore(self.root, self.KINDS)
        store.activate(now=self.NOW)
        third = self.record("segment")
        self.append(store, third)
        self.assertEqual(PhysicalCoordinate(-1, 2, 2), store.lookup(second["id"]).coordinate)
        self.assertEqual(PhysicalCoordinate(0, 1, 3), store.lookup(third["id"]).coordinate)
        self.assertEqual([first, second, third], list(store.iter_records()))

    def test_iter_records_holds_its_read_lock_until_iteration_finishes(self) -> None:
        """Catches returning an iterator only after its protecting read-lock context has closed."""
        store = SegmentedRunStore(self.root, self.KINDS)
        record = self.record("legacy")
        self.append(store, record)
        events: list[str] = []

        @contextmanager
        def observed_lock(path: Path, *, exclusive: bool):
            self.assertFalse(exclusive)
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        with mock.patch("floati.run_segments._locked_path", side_effect=observed_lock):
            iterator = store.iter_records()
            self.assertEqual([], events)
            self.assertEqual(record, next(iterator))
            self.assertEqual(["enter"], events)
            with self.assertRaises(StopIteration):
                next(iterator)
            self.assertEqual(["enter", "exit"], events)

    def test_invalid_constructor_inputs_refuse(self) -> None:
        for root, kinds, config in (
            (object(), self.KINDS, SegmentConfig()),
            (self.root, frozenset(), SegmentConfig()),
            (self.root, frozenset({"not_a_kind"}), SegmentConfig()),
            (self.root, self.KINDS, SegmentConfig(max_records=True)),
            (self.root, self.KINDS, SegmentConfig(max_records=0)),
            (self.root, self.KINDS, SegmentConfig(max_bytes=65535)),
            (self.root, self.KINDS, SegmentConfig(max_bytes=64 * 1024 * 1024 + 1)),
        ):
            with self.subTest(root=root, kinds=kinds, config=config), self.assertRaises((ProtocolRefusal, TypeError)):
                SegmentedRunStore(root, kinds, config)  # type: ignore[arg-type]

    def test_append_error_rolls_back_complete_frame(self) -> None:
        store = SegmentedRunStore(self.root, self.KINDS)
        store.activate(now=self.NOW)
        before = self.paths()[1].joinpath("00000000.jsonl").read_bytes()
        real_write = __import__("os").write
        def short(fd: int, data: bytes) -> int:
            real_write(fd, data[: len(data) // 2])
            return len(data) // 2
        with mock.patch("floati.jsonl.os.write", side_effect=short), self.assertRaises(DurabilityFailure):
            self.append(store, self.record("partial"))
        self.assertEqual(before, self.paths()[1].joinpath("00000000.jsonl").read_bytes())

    def test_metadata_append_error_leaves_prior_chain_and_segment_bytes(self) -> None:
        store = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=1))
        store.activate(now=self.NOW)
        self.append(store, self.record("first"))
        segment = self.paths()[1] / "00000000.jsonl"
        metadata = self.paths()[2]
        before_segment, before_metadata = segment.read_bytes(), metadata.read_bytes()
        real_write = __import__("os").write
        def short(fd: int, data: bytes) -> int:
            real_write(fd, data[: len(data) // 2])
            return len(data) // 2
        with mock.patch("floati.jsonl.os.write", side_effect=short), self.assertRaises(DurabilityFailure):
            self.append(store, self.record("second"))
        self.assertEqual(before_segment, segment.read_bytes())
        self.assertEqual(before_metadata, metadata.read_bytes())
        self.assertEqual(1, len(store.records()))

    def test_retry_recovers_a_sealed_tail_before_successor_file(self) -> None:
        """Catches rejecting the durable seal-only crash prefix instead of reopening it on retry."""
        store = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=1))
        store.activate(now=self.NOW)
        first, retry = self.record("first"), self.record("retry")
        self.append(store, first)
        real_append = _append_frame
        calls = 0

        def crash_after_seal(path: Path, encoded: bytes) -> None:
            nonlocal calls
            calls += 1
            real_append(path, encoded)
            if calls == 1:
                raise RuntimeError("crash after seal fsync")

        with mock.patch("floati.run_segments._append_frame", side_effect=crash_after_seal), self.assertRaisesRegex(RuntimeError, "after seal"):
            self.append(store, retry)
        metadata = self.paths()[2]
        before = {path: path.read_bytes() for path in self.paths()[1].iterdir() if path.is_file()}
        self.assertEqual(2, len(_read_path_records(metadata, "alpha", frozenset({"segment_opened", "segment_sealed"}))))
        self.assertEqual([first], store.records())
        self.assertEqual(before, {path: path.read_bytes() for path in self.paths()[1].iterdir() if path.is_file()})
        self.assertEqual("retry", self.append(store, retry, "retry"))
        self.assertEqual([first, retry], store.records())
        self.assertEqual(1, len((self.paths()[1] / "00000001.jsonl").read_text(encoding="utf-8").splitlines()))

    def test_retry_recovers_a_sealed_tail_with_exact_zero_byte_successor(self) -> None:
        """Catches treating a crash-created empty successor as corruption before its opening metadata."""
        store = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=1))
        store.activate(now=self.NOW)
        first, retry = self.record("first"), self.record("retry")
        self.append(store, first)
        real_append = _append_frame
        calls = 0

        def crash_before_open(path: Path, encoded: bytes) -> None:
            nonlocal calls
            calls += 1
            real_append(path, encoded)
            if calls == 2:
                raise RuntimeError("crash before segment_opened")

        with mock.patch("floati.run_segments._append_frame", side_effect=crash_before_open), self.assertRaisesRegex(RuntimeError, "before segment_opened"):
            self.append(store, retry)
        successor = self.paths()[1] / "00000001.jsonl"
        self.assertEqual(b"", successor.read_bytes())
        before = {path: path.read_bytes() for path in self.paths()[1].iterdir() if path.is_file()}
        self.assertEqual([first], store.records())
        self.assertEqual(before, {path: path.read_bytes() for path in self.paths()[1].iterdir() if path.is_file()})
        self.assertEqual("retry", self.append(store, retry, "retry"))
        self.assertEqual([first, retry], store.records())
        self.assertEqual(1, len(successor.read_text(encoding="utf-8").splitlines()))

    def test_activation_recovers_only_an_exact_zero_byte_segment_zero(self) -> None:
        """Catches activation rejecting its own pre-metadata crash artifact or accepting nonempty debris."""
        segments = self.paths()[1]
        segments.mkdir(parents=True)
        zero = segments / "00000000.jsonl"
        zero.write_bytes(b"")
        store = SegmentedRunStore(self.root, self.KINDS)
        store.activate(now=self.NOW)
        self.assertTrue(store.is_active())
        self.assertEqual([], store.records())

    def test_unreferenced_successor_debris_refuses(self) -> None:
        """Catches recovery treating a nonempty or wrongly named unreferenced file as retry state."""
        for name, contents in (("00000000.jsonl", b"x"), ("wrong.jsonl", b"")):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = FloatiRoot.open(Path(directory), "alpha")
                    segments = root.resolve_relative("runs/segments")
                    segments.mkdir(parents=True)
                    (segments / name).write_bytes(contents)
                    with self.assertRaises(IntegrityFailure):
                        SegmentedRunStore(root, self.KINDS).activate(now=self.NOW)

    def test_corruption_refuses_before_writer_mutation(self) -> None:
        corruptions = {
            "missing_metadata": lambda seg, meta: meta.unlink(),
            "missing_segment": lambda seg, meta: (seg / "00000000.jsonl").unlink(),
            "unexpected_filename": lambda seg, meta: (seg / "junk.jsonl").write_bytes(b""),
            "truncated": lambda seg, meta: (seg / "00000000.jsonl").write_bytes(b"{}"),
            "non_utf8": lambda seg, meta: (seg / "00000000.jsonl").write_bytes(b"\xff\n"),
        }
        for name, corrupt in corruptions.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = FloatiRoot.open(Path(directory), "alpha")
                    store = SegmentedRunStore(root, self.KINDS)
                    store.activate(now=self.NOW)
                    self.append(store, self.record(name))
                    seg = root.resolve_relative("runs/segments")
                    meta = seg / "events.jsonl"
                    corrupt(seg, meta)
                    watched = {p: p.read_bytes() for p in seg.rglob("*") if p.is_file()}
                    with self.assertRaises(IntegrityFailure):
                        self.append(store, self.record("after-" + name))
                    self.assertEqual(watched, {p: p.read_bytes() for p in seg.rglob("*") if p.is_file()})

    def test_seal_tamper_duplicate_history_and_metadata_gaps_refuse(self) -> None:
        def built() -> tuple[SegmentedRunStore, Path, Path]:
            store = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=1))
            store.activate(now=self.NOW)
            self.append(store, self.record("one"))
            self.append(store, self.record("two"))
            seg = self.root.resolve_relative("runs/segments")
            return store, seg, seg / "events.jsonl"
        store, seg, metadata = built()
        before = metadata.read_bytes()
        sealed = seg / "00000000.jsonl"
        sealed.write_bytes(sealed.read_bytes() + encode_frame(self.record("after-seal")))
        with self.assertRaises(IntegrityFailure):
            store.records()
        self.assertEqual(before, metadata.read_bytes())

    def test_metadata_lineage_seal_fields_oversize_and_global_duplicates_refuse(self) -> None:
        def exercise(name: str) -> None:
            with tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                store = SegmentedRunStore(root, self.KINDS, SegmentConfig(max_records=1))
                store.activate(now=self.NOW)
                first, second = self.record(name + "-one"), self.record(name + "-two")
                self.append(store, first)
                self.append(store, second)
                segments = root.resolve_relative("runs/segments")
                metadata = segments / "events.jsonl"
                rows = [json.loads(line) for line in metadata.read_text(encoding="utf-8").splitlines()]
                if name == "reordered":
                    rows[0], rows[1] = rows[1], rows[0]
                elif name == "duplicate_metadata":
                    rows.append(deepcopy(rows[2]))
                elif name == "number_gap":
                    rows[2]["segment_number"] = 2
                elif name == "ordinal_gap":
                    rows[2]["first_global_ordinal"] += 1
                elif name == "predecessor_mismatch":
                    rows[2]["previous_seal_digest"] = "f" * 64
                elif name == "count_mismatch":
                    rows[1]["record_count"] += 1
                    rows[1]["seal_digest"] = segment_seal_digest(rows[1])
                elif name == "length_mismatch":
                    rows[1]["byte_length"] += 1
                    rows[1]["seal_digest"] = segment_seal_digest(rows[1])
                elif name == "digest_mismatch":
                    rows[1]["segment_sha256"] = "b" * 64
                    rows[1]["seal_digest"] = segment_seal_digest(rows[1])
                elif name == "duplicate_global_id":
                    (segments / "00000001.jsonl").write_bytes((segments / "00000000.jsonl").read_bytes())
                elif name == "oversized_frame":
                    (segments / "00000001.jsonl").write_bytes(b"x" * 65536 + b"\n")
                if name not in {"duplicate_global_id", "oversized_frame"}:
                    metadata.write_bytes(b"".join(encode_frame(row) for row in rows))
                watched = {path: path.read_bytes() for path in segments.iterdir() if path.is_file()}
                with self.assertRaises(IntegrityFailure):
                    self.append(store, self.record(name + "-after"))
                self.assertEqual(watched, {path: path.read_bytes() for path in segments.iterdir() if path.is_file()})

        for name in (
            "reordered", "duplicate_metadata", "number_gap", "ordinal_gap",
            "predecessor_mismatch", "count_mismatch", "length_mismatch",
            "digest_mismatch", "duplicate_global_id", "oversized_frame",
        ):
            with self.subTest(name=name):
                exercise(name)

    def test_helpers_do_not_widen_observation_or_arbitrary_path_authority(self) -> None:
        from floati.jsonl import _append_frame
        FloatiRoot.open(Path(self.temp.name), "bravo")
        observation = self.root.observe_tenant(self.root.grant_observation("bravo"), "bravo")
        with self.assertRaises((TypeError, AttributeError, ProtocolRefusal)):
            SegmentedRunStore(observation, self.KINDS)  # type: ignore[arg-type]
        self.assertFalse(hasattr(observation, "tenant_home"))
        store = SegmentedRunStore(self.root, self.KINDS)
        with mock.patch("floati.run_segments._append_frame", wraps=_append_frame) as fixed_append:
            store.activate(now=self.NOW)
            self.append(store, self.record("fixed"))
        relative_paths = {
            call.args[0].relative_to(self.root.tenant_home).as_posix()
            for call in fixed_append.call_args_list
        }
        self.assertEqual(
            {"runs/segments/00000000.jsonl", "runs/segments/events.jsonl"},
            relative_paths,
        )

    def test_raw_path_mechanics_remain_private(self) -> None:
        """Catches exporting raw Path read/write/lock authority beyond the trusted segment engine."""
        import floati.jsonl as jsonl
        for name in ("locked_path", "read_path_records", "encode_record", "append_frame"):
            self.assertFalse(hasattr(jsonl, name))
