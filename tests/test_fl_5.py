"""FL-5: a stat-keyed validation cache is blind inside one clock tick.

Ruling: docs/rulings/2026-09-12-fl-5-a-stat-cache-is-blind-inside-one-clock-tick.md
Architect: validated state is reused ONLY when every file it validated is
provably byte-identical. RED is a deterministic same-tick tamper (no sleeps).

Ways this check could be wrong, each with a case below:

1. Rely on the wall clock. Linux coarse mtime makes this flake; macOS APFS
   hides it. Built: freeze Path.stat to the pre-tamper result so the tick
   cannot save the test.
2. Same size + same mtime only. A rewrite that also changes ctime would
   still miss. Built: freeze the whole stat_result, including ctime.
3. Truncate then regrow to the same size. Size matches; bytes differ.
   Built: that rewrite under frozen stats.
4. Rename-swap two same-size segment files. Path set and sizes match.
   Built: swap 00000000.jsonl and 00000001.jsonl under frozen stats.
5. File added/removed. Path-set change already busts a stat cache. Built
   as controls that still refuse without freezing stats.
6. mtime rolled back on honest bytes. A digest key should still reuse;
   a stat key revalidates. Built: append still succeeds.
7. Freezing `_state_signature` itself. After a digest key that mock
   would hide tampers. Built: freeze Path.stat, never the signature
   function.
8. Loosening validity. The refusal code stays IntegrityFailure; no
   check is dropped. Built: the existing length_mismatch family remains.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.errors import DurabilityFailure, IntegrityFailure
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.records import segment_seal_digest
from floati.root import FloatiRoot
from floati.run_segments import SegmentConfig, SegmentedRunStore


@contextmanager
def _same_tick_stats(paths: list[Path]):
    """Replay pre-tamper Path.stat so a rewrite looks like one coarse tick."""

    original = Path.stat
    frozen = {
        os.path.realpath(path): original(path)
        for path in paths
        if path.exists()
    }

    def wrapped(self: Path, *args: object, **kwargs: object):
        key = os.path.realpath(self)
        if key in frozen:
            return frozen[key]
        return original(self, *args, **kwargs)

    with mock.patch.object(Path, "stat", wrapped):
        yield


class Fl5StatCacheTests(unittest.TestCase):
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

    def _activated(self, *, max_records: int = 1) -> tuple[SegmentedRunStore, Path]:
        store = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=max_records))
        store.activate(now=self.NOW)
        return store, self.root.resolve_relative("runs/segments")

    def test_same_size_metadata_tamper_inside_one_tick_refuses(self) -> None:
        """RED: length_mismatch under frozen stats is reused, not refused."""

        store, segments = self._activated()
        self.append(store, self.record("one"))
        self.append(store, self.record("two"))
        metadata = segments / "events.jsonl"
        rows = [json.loads(line) for line in metadata.read_text(encoding="utf-8").splitlines()]
        rows[1]["byte_length"] += 1
        rows[1]["seal_digest"] = segment_seal_digest(rows[1])
        rewritten = b"".join(encode_frame(row) for row in rows)
        self.assertEqual(len(metadata.read_bytes()), len(rewritten), "tamper must stay same-size")
        ledger = [path for path in segments.iterdir() if path.is_file()]
        with _same_tick_stats(ledger):
            metadata.write_bytes(rewritten)
            watched = {path: path.read_bytes() for path in ledger if path.exists()}
            with self.assertRaises(IntegrityFailure):
                self.append(store, self.record("after"))
            self.assertEqual(watched, {path: path.read_bytes() for path in ledger if path.exists()})

    def test_truncate_and_regrow_same_size_inside_one_tick_refuses(self) -> None:
        store, segments = self._activated()
        self.append(store, self.record("one"))
        self.append(store, self.record("two"))
        metadata = segments / "events.jsonl"
        original = metadata.read_bytes()
        rows = [json.loads(line) for line in original.splitlines()]
        rows[1]["segment_sha256"] = "b" * 64
        rows[1]["seal_digest"] = segment_seal_digest(rows[1])
        rewritten = b"".join(encode_frame(row) for row in rows)
        self.assertEqual(len(original), len(rewritten), "tamper must stay same-size")
        ledger = [path for path in segments.iterdir() if path.is_file()]
        with _same_tick_stats(ledger):
            metadata.write_bytes(b"")
            metadata.write_bytes(rewritten)
            with self.assertRaises(IntegrityFailure):
                self.append(store, self.record("after"))

    def test_rename_swap_same_size_segments_inside_one_tick_refuses(self) -> None:
        store, segments = self._activated()
        self.append(store, self.record("one"))
        self.append(store, self.record("two"))
        first = segments / "00000000.jsonl"
        second = segments / "00000001.jsonl"
        self.assertEqual(first.stat().st_size, second.stat().st_size)
        ledger = [path for path in segments.iterdir() if path.is_file()]
        with _same_tick_stats(ledger):
            tmp = segments / "swap.tmp"
            first.rename(tmp)
            second.rename(first)
            tmp.rename(second)
            with self.assertRaises(IntegrityFailure):
                self.append(store, self.record("after"))

    def test_added_segment_file_refuses_without_freezing_stats(self) -> None:
        store, segments = self._activated()
        self.append(store, self.record("one"))
        self.append(store, self.record("two"))
        (segments / "99999999.jsonl").write_bytes(b"{}\n")
        with self.assertRaises(IntegrityFailure):
            self.append(store, self.record("after"))

    def test_removed_segment_file_refuses_without_freezing_stats(self) -> None:
        store, segments = self._activated()
        self.append(store, self.record("one"))
        self.append(store, self.record("two"))
        (segments / "00000000.jsonl").unlink()
        with self.assertRaises(IntegrityFailure):
            self.append(store, self.record("after"))

    def test_mtime_rollback_on_honest_bytes_still_appends(self) -> None:
        store, segments = self._activated()
        self.append(store, self.record("one"))
        self.append(store, self.record("two"))
        metadata = segments / "events.jsonl"
        current = metadata.stat()
        os.utime(metadata, ns=(current.st_atime_ns, max(0, current.st_mtime_ns - 10**9)))
        self.assertEqual("ok", self.append(store, self.record("after")))

    def test_honest_second_append_reuses_validated_state(self) -> None:
        """Green control: measure uncached validates, not wall time."""

        store, _segments = self._activated()
        self.append(store, self.record("one"))
        uncached = store._validate_active_uncached
        calls = {"n": 0}

        def counted() -> object:
            calls["n"] += 1
            return uncached()

        with mock.patch.object(store, "_validate_active_uncached", side_effect=counted):
            self.append(store, self.record("two"))
        self.assertEqual(0, calls["n"])

    def test_large_ledger_honest_append_stays_on_the_cached_path(self) -> None:
        store, _segments = self._activated(max_records=1)
        for index in range(24):
            self.append(store, self.record(f"row-{index}"))
        uncached = store._validate_active_uncached
        calls = {"n": 0}

        def counted() -> object:
            calls["n"] += 1
            return uncached()

        with mock.patch.object(store, "_validate_active_uncached", side_effect=counted):
            self.append(store, self.record("cached-tail"))
        self.assertEqual(0, calls["n"])

    def _unreadable(self, path: Path) -> None:
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)

    def _assert_unreadable_root_read_only(self, store: SegmentedRunStore, path: Path) -> None:
        self._unreadable(path)
        try:
            self.append(store, self.record("after"))
        except DurabilityFailure as exc:
            self.assertEqual("root_read_only", exc.code)
        except Exception as exc:
            self.fail(f"append raised {type(exc).__name__}: {exc}")
        else:
            self.fail("unreadable ledger appended")
        try:
            store.records()
        except DurabilityFailure as exc:
            self.assertEqual("root_read_only", exc.code)
        except Exception as exc:
            self.fail(f"records() raised {type(exc).__name__}: {exc}")
        else:
            self.fail("unreadable ledger listed")
        fresh = SegmentedRunStore(self.root, self.KINDS, SegmentConfig(max_records=1))
        try:
            fresh.records()
        except DurabilityFailure as exc:
            self.assertEqual("root_read_only", exc.code)
        except Exception as exc:
            self.fail(f"fresh records() raised {type(exc).__name__}: {exc}")
        else:
            self.fail("unreadable ledger listed on a fresh reader")

    def test_unreadable_segment_is_root_read_only_on_append_records_and_fresh_reader(self) -> None:
        """RED: Path.read_bytes raises PermissionError instead of DurabilityFailure."""

        store, segments = self._activated()
        self.append(store, self.record("one"))
        self.append(store, self.record("two"))
        self._assert_unreadable_root_read_only(store, segments / "00000000.jsonl")

    def test_unreadable_metadata_is_root_read_only_on_append_records_and_fresh_reader(self) -> None:
        store, segments = self._activated()
        self.append(store, self.record("one"))
        self.append(store, self.record("two"))
        self._assert_unreadable_root_read_only(store, segments / "events.jsonl")

    def test_honest_append_does_not_rehash_the_ledger_after_the_write(self) -> None:
        """Each append hashed the whole ledger twice; extend with written bytes."""

        store, segments = self._activated(max_records=10000)
        for index in range(24):
            self.append(store, self.record(f"row-{index}"))
        ledger = [self.root.resolve_relative("runs/events.jsonl")]
        ledger.extend(path for path in segments.iterdir() if path.is_file() and path.name != "writer.lock")
        size = sum(path.stat().st_size for path in ledger if path.exists())
        original = Path.read_bytes
        reads = {"bytes": 0}

        def counted(self: Path) -> bytes:
            data = original(self)
            reads["bytes"] += len(data)
            return data

        with mock.patch.object(Path, "read_bytes", counted):
            self.append(store, self.record("once"))
        self.assertLessEqual(reads["bytes"], size)
