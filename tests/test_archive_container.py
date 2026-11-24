"""Opaque native archive round trips and hostile container boundaries."""
from __future__ import annotations

import tests  # Confine HOME/XDG before product imports.

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile
import zlib

from floati.archive_container import export_members, read_container_member, verify_container, write_container
from floati.errors import IntegrityFailure, ProtocolRefusal
from tests.temp_roots import REAL_TEMP_ROOT


def inventory(payloads):
    return [{"path": name,
             "plane": "events" if name == "events.jsonl" else name.split("/")[1],
             "byte_length": len(payloads[name]),
             "sha256": hashlib.sha256(payloads[name]).hexdigest()}
            for name in sorted(payloads, key=lambda value: value.encode("utf-8"))]


def file_facts(path):
    raw = path.read_bytes()
    return {"byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class ArchiveContainerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.path = self.base / "epoch.zip"
        self.payloads = {"events.jsonl": b"opaque\x00\xff\r\n",
                         "receipts/acks/node/deep/\u00e9.jsonl": b"ack bytes\n",
                         "receipts/deliveries/node.jsonl": b""}
        self.rows = inventory(self.payloads)

    def opener(self, name):
        return io.BytesIO(self.payloads[name])

    def raw_zip(self, members=None, *, method=zipfile.ZIP_DEFLATED):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.path, "w", compression=method) as container:
                for name, data in self.payloads.items() if members is None else members:
                    container.writestr(name, data)
        return file_facts(self.path)

    def refuses(self, facts=None, rows=None):
        with self.assertRaises(IntegrityFailure) as caught:
            verify_container(self.path, file_facts(self.path) if facts is None else facts,
                             self.rows if rows is None else rows)
        self.assertEqual("archive_container_invalid", caught.exception.code)

    def test_native_roll_round_trip_matches_original_receipt_and_exact_opaque_bytes(self):
        from floati.bus_epoch import roll_bus_epoch
        from floati.events import EventLog
        from floati.planes import AuthorityGrantStore
        from floati.registry import Registry
        from floati.root import FloatiRoot
        root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("actor", "recipient"):
            Registry(root).register(node, "Codex")
        AuthorityGrantStore(root).grant_exact("bus-epoch-roll", "actor", 1,
                                             datetime.now(timezone.utc))
        EventLog(root).send("actor", "recipient", "floati", "a" * 40,
                           "AGENTS.md", "codec fixture", idempotency_key="codec")
        receipt_path = root.tenant_home / "receipts/acks/node/deep/\u00e9.jsonl"
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_bytes(b"opaque receipt\x00\xff\r\n")
        receipt = roll_bus_epoch(root, actor="actor", idempotency_key="codec-roll")["receipt"]
        archive = Path(receipt["archive_path"])
        payloads = {member.relative_to(archive).as_posix(): member.read_bytes()
                    for member in archive.rglob("*") if member.is_file()}
        rows = inventory(payloads)
        facts = write_container(self.path, rows, lambda name: (archive / name).open("rb"))
        expected = {key: receipt[key] for key in
                    ("archive_sha256", "archive_file_count", "plane_counts", "span")}
        self.assertEqual(expected, verify_container(self.path, facts, rows))
        for name, raw in payloads.items():
            self.assertEqual(raw, read_container_member(self.path, facts, rows, name))
        self.assertIsNone(read_container_member(self.path, facts, rows, "receipts/acks/missing.jsonl"))

    def test_writer_is_deterministic_and_uses_ordinary_deflated_metadata(self):
        first = write_container(self.path, self.rows, self.opener)
        second_path = self.base / "second.zip"
        second = write_container(second_path, self.rows, self.opener)
        self.assertEqual(first, second)
        self.assertEqual(self.path.read_bytes(), second_path.read_bytes())
        with zipfile.ZipFile(self.path) as container:
            self.assertEqual([row["path"] for row in self.rows], container.namelist())
            for item in container.infolist():
                self.assertEqual(zipfile.ZIP_DEFLATED, item.compress_type)
                self.assertEqual((1980, 1, 1, 0, 0, 0), item.date_time)
                self.assertTrue(stat.S_ISREG(item.external_attr >> 16))

    def test_export_decoded_bytes_stay_linear_as_member_count_grows(self):
        native_inflater = zlib.decompressobj
        for scale in (1, 10):
            with self.subTest(scale=scale):
                payloads = {"events.jsonl": b"event\n" * 200}
                payloads.update({"receipts/acks/node-%s.jsonl" % number: b"ack\n" * 300
                                 for number in range(scale * 2)})
                path = self.base / ("scale-%s.zip" % scale)
                write_container(path, inventory(payloads), lambda name: io.BytesIO(payloads[name]))
                decoded = []
                requested = []
                class MeteredInflater:
                    def __init__(self, *args, **kwargs):
                        self.native = native_inflater(*args, **kwargs)
                    def __getattr__(self, name):
                        return getattr(self.native, name)
                    def decompress(self, *args, **kwargs):
                        requested.append(args[1] if len(args) > 1 else kwargs.get("max_length", 0))
                        block = self.native.decompress(*args, **kwargs)
                        decoded.append(len(block))
                        return block
                with mock.patch.object(zlib, "decompressobj", MeteredInflater):
                    facts, actual = export_members(path)
                self.assertEqual(actual, payloads)
                self.assertEqual(facts, file_facts(path))
                self.assertTrue(all(0 < value <= 65536 for value in requested),
                    "export requests an unbounded inflater output allocation")
                self.assertLessEqual(sum(decoded), 3 * sum(map(len, payloads.values())),
                    "export repeatedly inflates unrelated members instead of one bounded verification")
                decoded.clear()
                from floati.bus_epoch import _read_epoch_archive_payloads
                with mock.patch.object(zlib, "decompressobj", MeteredInflater):
                    native, _snapshot = _read_epoch_archive_payloads(path)
                self.assertEqual({name: raw for name, _plane, raw in native}, payloads)
                self.assertLessEqual(sum(decoded), 3 * sum(map(len, payloads.values())),
                    "native snapshot recheck inflates an unchanged verified container again")

    def test_writer_reads_fixed_buffers_and_preserves_bytes_above_live_record_bound(self):
        raw = b"\xff\x00x\n" * 300000
        payloads = {"events.jsonl": raw}
        class BoundedStream(io.BytesIO):
            def read(stream, size=-1):
                self.assertEqual(65536, size, "writer must request fixed buffers")
                return super().read(size)
        facts = write_container(self.path, inventory(payloads), lambda name: BoundedStream(raw))
        self.assertEqual(raw, read_container_member(self.path, facts, inventory(payloads), "events.jsonl"))

    def test_source_cannot_ignore_the_fixed_buffer_bound(self):
        raw = b"X" * 131072
        class OversizedRead(io.BytesIO):
            def read(stream, size):
                return super().read(131072)
        with self.assertRaises(IntegrityFailure):
            write_container(self.path, inventory({"events.jsonl": raw}), lambda name: OversizedRead(raw))
        self.assertTrue(self.path.exists(), "failed output has no pathname exclusion for safe cleanup")

    def test_retained_compressed_bytes_refuse_changed_files_and_path_aliases(self):
        from floati.bus_epoch import _assert_epoch_archive_snapshot, _read_epoch_archive_payloads
        for mutation in ("same-size", "append", "replace", "mode", "hardlink", "symlink", "ancestor"):
            with self.subTest(mutation=mutation):
                parent = self.base / mutation
                parent.mkdir()
                path = parent / "epoch.zip"
                write_container(path, self.rows, self.opener)
                _payloads, snapshot = _read_epoch_archive_payloads(path)
                original = path.stat()
                raw = path.read_bytes()
                if mutation == "same-size":
                    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
                    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
                elif mutation == "append":
                    path.write_bytes(raw + b"extra")
                elif mutation == "replace":
                    replacement = parent / "replacement"
                    replacement.write_bytes(raw)
                    replacement.replace(path)
                elif mutation == "mode":
                    path.chmod(0o400)
                elif mutation == "hardlink":
                    os.link(path, parent / "alias")
                elif mutation == "symlink":
                    path.rename(parent / "original")
                    path.symlink_to(parent / "original")
                else:
                    moved = self.base / "moved-parent"
                    parent.rename(moved)
                    parent.symlink_to(moved, target_is_directory=True)
                with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                    _assert_epoch_archive_snapshot(path, snapshot)

    def test_wrong_container_digest_and_length_refuse_even_for_missing_member_query(self):
        facts = self.raw_zip()
        for changes in ({"sha256": "0" * 64}, {"byte_length": facts["byte_length"] - 1},
                        {"byte_length": facts["byte_length"] + 1}, {"byte_length": True},
                        {"extra": 1}):
            with self.subTest(changes=changes):
                bad = dict(facts, **changes)
                self.refuses(bad)
                with self.assertRaises(IntegrityFailure):
                    read_container_member(self.path, bad, self.rows, "receipts/acks/missing.jsonl")

    def test_extra_missing_and_duplicate_members_refuse(self):
        original = list(self.payloads.items())
        for members in (original + [("receipts/acks/extra.jsonl", b"extra")], original[:-1],
                        original + [original[0]]):
            with self.subTest(names=[name for name, raw in members]):
                self.raw_zip(members)
                self.refuses()

    def test_absolute_escaping_alias_and_nul_member_names_refuse(self):
        for name in ("../events.jsonl", "/events.jsonl", "a/../events.jsonl", "./events.jsonl",
                     "receipts//acks/x.jsonl", "receipts\\acks\\x.jsonl", "events.jsonl\x00suffix"):
            with self.subTest(name=name):
                self.raw_zip([(name, b"bad")])
                self.refuses()

    def test_links_special_files_and_directory_members_refuse(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFDIR | 0o700):
            with self.subTest(mode=mode):
                item = zipfile.ZipInfo("events.jsonl")
                item.create_system = 3
                item.external_attr = mode << 16
                item.compress_type = zipfile.ZIP_DEFLATED
                self.raw_zip([(item, self.payloads["events.jsonl"])])
                self.refuses(rows=inventory({"events.jsonl": self.payloads["events.jsonl"]}))

    def test_encrypted_flag_and_unsupported_compression_refuse(self):
        self.raw_zip()
        raw = bytearray(self.path.read_bytes())
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            index = raw.index(signature)
            struct.pack_into("<H", raw, index + offset, struct.unpack_from("<H", raw, index + offset)[0] | 1)
        self.path.write_bytes(raw)
        self.refuses()
        self.raw_zip(method=zipfile.ZIP_STORED)
        self.refuses()

    def test_decoded_digest_and_per_member_length_are_independently_verified(self):
        original = self.payloads["events.jsonl"]
        for raw in (original + b"x", original[:-1], b"X" + original[1:]):
            with self.subTest(raw=raw):
                changed = dict(self.payloads, **{"events.jsonl": raw})
                self.raw_zip(list(changed.items()))
                self.refuses()

    def test_missing_query_cannot_hide_another_damaged_member(self):
        changed = dict(self.payloads, **{"events.jsonl": b"wrong"})
        facts = self.raw_zip(list(changed.items()))
        with self.assertRaises(IntegrityFailure):
            read_container_member(self.path, facts, self.rows, "receipts/acks/missing.jsonl")

    def test_invalid_inventory_is_typed_before_any_output(self):
        cases = [[], self.rows[::-1], self.rows + [self.rows[-1]],
                 [dict(self.rows[0], plane="acks")],
                 [dict(self.rows[0], byte_length=True)],
                 [dict(self.rows[0], byte_length=-1)],
                 [dict(self.rows[0], sha256="A" * 64)],
                 [dict(self.rows[0], extra=1)],
                 [dict(self.rows[0], path="receipts/workers.jsonl", plane="workers")]]
        cases.extend([dict(self.rows[0], path=path)] for path in (
            "/events.jsonl", "../events.jsonl", "receipts/acks/../x.jsonl",
            "receipts//acks/x.jsonl", "receipts\\acks\\x.jsonl", "state/x.jsonl"))
        for rows in cases:
            with self.subTest(rows=rows):
                destination = self.base / "bad.zip"
                with self.assertRaises(IntegrityFailure) as caught:
                    write_container(destination, rows, lambda name: io.BytesIO(b"fixture"))
                self.assertEqual("archive_container_invalid", caught.exception.code)
                self.assertFalse(destination.exists())

    def test_writer_refuses_source_length_or_digest_mismatch_and_preserves_uncertain_partial(self):
        for index, raw in enumerate((b"short", self.payloads["events.jsonl"] + b"extra", b"Z" + self.payloads["events.jsonl"][1:])):
            with self.subTest(raw=raw):
                self.path = self.base / ("mismatch-" + str(index) + ".zip")
                with self.assertRaises(IntegrityFailure):
                    write_container(self.path, [self.rows[0]], lambda name: io.BytesIO(raw))
                self.assertTrue(self.path.exists(), "uncertain partial must be preserved")

    def test_existing_destination_and_symlinked_parent_are_never_written(self):
        self.path.write_bytes(b"sentinel")
        with self.assertRaises(ProtocolRefusal):
            write_container(self.path, self.rows, self.opener)
        self.assertEqual(b"sentinel", self.path.read_bytes())
        real = self.base / "real"
        real.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ProtocolRefusal):
            write_container(alias / "unexpected.zip", self.rows, self.opener)
        self.assertEqual([], list(real.iterdir()))

    def test_reader_refuses_symlink_and_hardlinked_containers(self):
        facts = self.raw_zip()
        alias = self.base / "alias.zip"
        alias.symlink_to(self.path)
        with self.assertRaises(IntegrityFailure):
            verify_container(alias, facts, self.rows)
        alias.unlink()
        os.link(self.path, alias)
        self.refuses(facts)

    def test_source_failure_cleanup_preserves_substituted_output(self):
        @contextmanager
        def broken(name):
            self.path.unlink()
            self.path.write_bytes(b"foreign replacement")
            raise OSError("source failed after substitution")
            yield
        with self.assertRaises((OSError, IntegrityFailure)):
            write_container(self.path, self.rows, broken)
        self.assertEqual(b"foreign replacement", self.path.read_bytes())

    def test_relative_query_must_be_a_canonical_native_member_name(self):
        facts = self.raw_zip()
        for name in ("../events.jsonl", "/events.jsonl", "./events.jsonl", "receipts/workers.jsonl"):
            with self.subTest(name=name), self.assertRaises(IntegrityFailure):
                read_container_member(self.path, facts, self.rows, name)

    def test_truncated_or_non_zip_container_is_typed(self):
        for raw in (b"not a zip", b"PK\x05\x06", b""):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.refuses()

    def test_growing_container_stops_at_first_compressed_length_overrun(self):
        import floati.archive_container as codec
        facts = self.raw_zip()
        real_fdopen = os.fdopen
        class GrowingFile:
            def __init__(stream, handle):
                stream.handle, stream.reads = handle, 0
            def __enter__(stream):
                return stream
            def __exit__(stream, *args):
                stream.handle.close()
            def __getattr__(stream, name):
                return getattr(stream.handle, name)
            def read(stream, size):
                stream.reads += 1
                self.assertLessEqual(stream.reads, 2, "reader continued after exact compressed quota was exceeded")
                block = stream.handle.read(size)
                if stream.reads == 1:
                    with self.path.open("ab") as changed:
                        changed.write(b"X" * 65536)
                return block
        with mock.patch.object(codec.os, "fdopen", side_effect=lambda fd, mode: GrowingFile(real_fdopen(fd, mode))):
            with self.assertRaises(IntegrityFailure):
                verify_container(self.path, facts, self.rows)

    def test_zip64_local_and_end_records_preserve_original_bytes(self):
        payloads = {"events.jsonl": b"opaque\xff" * 1000}
        with mock.patch.object(zipfile, "ZIP64_LIMIT", 128):
            facts = write_container(self.path, inventory(payloads), lambda name: io.BytesIO(payloads[name]))
        self.assertEqual(payloads["events.jsonl"], read_container_member(self.path, facts, inventory(payloads), "events.jsonl"))

    def test_zip64_end_record_is_read_when_classic_counts_still_fit(self):
        try:
            with mock.patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 1):
                facts = write_container(self.path, self.rows, self.opener)
        except IntegrityFailure as error:
            self.fail("a valid ZIP64 end record was refused: " + error.detail)
        self.assertEqual({"events": 1, "acks": 1, "deliveries": 1},
                         verify_container(self.path, facts, self.rows)["plane_counts"])

    def test_zip64_locator_cannot_seek_outside_the_verified_container(self):
        with mock.patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 1):
            self.raw_zip()
        raw = bytearray(self.path.read_bytes())
        locator = raw.index(b"PK\x06\x07")
        struct.pack_into("<Q", raw, locator + 8, 0xffffffffffffffff)
        self.path.write_bytes(raw)
        try:
            self.refuses()
        except OverflowError:
            self.fail("untrusted ZIP64 locator escaped the typed container bound")

    def test_writer_cannot_return_an_aliased_container(self):
        alias = self.base / "alias.zip"
        @contextmanager
        def changed(name):
            yield io.BytesIO(self.payloads[name])
            os.link(self.path, alias)
        with self.assertRaises(IntegrityFailure):
            write_container(self.path, [self.rows[0]], changed)
        self.assertTrue(alias.exists(), "cleanup must not remove another pathname")

    def test_local_header_name_or_crc_cannot_disagree_with_central_directory(self):
        for offset in (14, 30):
            with self.subTest(offset=offset):
                self.raw_zip()
                raw = bytearray(self.path.read_bytes())
                raw[offset] ^= 1
                self.path.write_bytes(raw)
                self.refuses()

    def test_hidden_deflate_overrun_refuses_even_when_header_lengths_match_inventory(self):
        self.raw_zip([("events.jsonl", b"X" * 200000)])
        raw = bytearray(self.path.read_bytes())
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<I", raw, 22, 1)
        struct.pack_into("<I", raw, central + 24, 1)
        self.path.write_bytes(raw)
        with self.assertRaises(IntegrityFailure) as caught:
            verify_container(self.path, file_facts(self.path), inventory({"events.jsonl": b"X"}))
        self.assertIn("exceeds its verified inventory length", caught.exception.detail)

    def test_writer_failure_preserves_partial_without_a_pathname_exclusion_contract(self):
        @contextmanager
        def broken(name):
            raise OSError("unavailable source")
            yield
        with self.assertRaises(IntegrityFailure):
            write_container(self.path, self.rows, broken)
        self.assertTrue(self.path.exists(), "identity comparison cannot make pathname unlink atomic")

    def test_writer_does_not_return_facts_for_corrupted_own_output(self):
        @contextmanager
        def changed(name):
            yield io.BytesIO(self.payloads[name])
            with self.path.open("r+b") as output:
                output.write(b"BAD!")
        with self.assertRaises(IntegrityFailure):
            write_container(self.path, [self.rows[0]], changed)
        self.assertTrue(self.path.exists(), "externally corrupted output must not be deleted")
        self.assertEqual(b"BAD!", self.path.read_bytes()[:4])

    def changed_output_before_failure(self, change):
        import floati.archive_container as codec
        witnessed = {}
        def fail_after_change(*args):
            before = self.path.stat()
            witnessed["original_inode"] = (before.st_dev, before.st_ino)
            change()
            after = self.path.stat()
            witnessed["identity"] = (after.st_dev, after.st_ino)
            witnessed["mode"] = stat.S_IMODE(after.st_mode)
            witnessed["links"] = after.st_nlink
            witnessed["bytes"] = self.path.read_bytes()
            raise OSError("injected verifier read failure after external change")
        with mock.patch.object(codec, "_directory", side_effect=fail_after_change):
            with self.assertRaises(IntegrityFailure) as caught:
                write_container(self.path, self.rows, self.opener)
        self.assertEqual("archive_container_invalid", caught.exception.code)
        self.assertEqual(witnessed["original_inode"], witnessed["identity"], "fixture must change the same inode")
        self.assertTrue(self.path.exists(), "externally changed same-inode destination was deleted")
        after = self.path.stat()
        self.assertEqual(witnessed["identity"], (after.st_dev, after.st_ino))
        self.assertEqual(witnessed["mode"], stat.S_IMODE(after.st_mode))
        self.assertEqual(witnessed["links"], after.st_nlink)
        self.assertEqual(witnessed["bytes"], self.path.read_bytes())

    def test_same_inode_append_before_failure_is_preserved(self):
        def append():
            with self.path.open("ab") as changed:
                changed.write(b"externally appended sentinel")
        alias = self.base / "changed-alias.zip"
        changes = (("append", append), ("chmod", lambda: self.path.chmod(0o400)),
                   ("hardlink", lambda: os.link(self.path, alias)))
        for name, change in changes:
            with self.subTest(change=name):
                self.path = self.base / (name + ".zip")
                self.changed_output_before_failure(change)
                if name == "hardlink":
                    self.assertEqual(self.path.read_bytes(), alias.read_bytes())

    def test_special_container_file_is_refused_without_reading_its_bytes(self):
        os.mkfifo(self.path)
        descriptor = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        try:
            with self.assertRaises(IntegrityFailure):
                verify_container(self.path, {"byte_length": 0, "sha256": hashlib.sha256(b"").hexdigest()}, self.rows)
        finally:
            os.close(descriptor)

    def test_streaming_member_larger_than_live_ledger_limit_has_no_codec_ceiling(self):
        length = 64 * 1024 * 1024 + 1
        digest = hashlib.sha256()
        for unused in range(1024):
            digest.update(b"\xff" * 65536)
        digest.update(b"\xff")
        rows = [{"path": "events.jsonl", "plane": "events", "byte_length": length,
                 "sha256": digest.hexdigest()}]
        class OpaqueStream:
            def __init__(stream):
                stream.remaining = length
            def __enter__(stream):
                return stream
            def __exit__(stream, *args):
                pass
            def read(stream, size):
                self.assertEqual(65536, size)
                amount = min(size, stream.remaining)
                stream.remaining -= amount
                return b"\xff" * amount
        facts = write_container(self.path, rows, lambda name: OpaqueStream())
        self.assertEqual({"byte_start": 0, "byte_end": length},
                         verify_container(self.path, facts, rows)["span"])
