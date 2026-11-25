"""Selected gateway transport integrity is an offline, scratch-only prerequisite."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.install_limits import MAX_INSTALL_FILES, MAX_INSTALL_FILE_BYTES, MAX_INSTALL_TOTAL_BYTES
from floati.seat_transport_preflight import preflight_transport
from tests.temp_roots import REAL_TEMP_ROOT


CLIENTS = ("floati/__init__.py", "floati/__main__.py", "floati/cli.py",
           "floati/codex_hook_install.py", "floati/codex_wait.py")
# Published readers keep their compiled bounds after the runtime updates.
MIN_DEPLOYED_INSTALL_FILES = 4096


class SeatTransportPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.install = self.base / "installed"
        self.install.mkdir()
        self.interpreter = self.base / "python"
        self.interpreter.write_bytes(b"#!/bin/sh\nexit 99\n")
        self.interpreter.chmod(0o700)  # A valid path; this fixture must never run.
        entries = []
        for relative in CLIENTS:
            path = self.install / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = ("ordinary client fixture " + relative + "\n").encode()
            path.write_bytes(encoded)
            entries.append({"path": relative, "sha256": hashlib.sha256(encoded).hexdigest()})
        self.manifest = self.install / ".floati-install/manifest.v0.json"
        self.manifest.parent.mkdir()
        self.document = {"schema_version": 0, "source_ref": "fixture-ref",
                         "source_sha": "b" * 40, "files": entries}
        self.transport = {"kind": "floati-install-manifest-v0", "install_root": str(self.install),
            "manifest_path": str(self.manifest), "interpreter": str(self.interpreter),
            "source_sha": "b" * 40, "manifest_sha256": "a" * 64,
            "max_files": 512, "max_file_bytes": 1048576, "max_total_bytes": 33554432}
        self.save_manifest()

    def save_manifest(self, encoded=None):
        encoded = encoded if encoded is not None else (json.dumps(self.document) + "\n").encode()
        self.manifest.write_bytes(encoded)
        self.transport["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()

    def snapshot(self):
        return {str(path.relative_to(self.base)): ("symlink", os.readlink(path)) if path.is_symlink()
                else ("directory", path.stat().st_mode) if path.is_dir()
                else ("file", path.stat().st_mode, path.read_bytes())
                for path in self.base.rglob("*")}

    def refuses(self, transport=None):
        before = self.snapshot()
        with self.assertRaises(ProtocolRefusal) as caught:
            preflight_transport(self.transport if transport is None else transport)
        self.assertEqual("seat_transport_preflight_invalid", caught.exception.code)
        self.assertTrue(caught.exception.remedy)
        self.assertEqual(before, self.snapshot())
        return caught.exception

    def test_verified_transport_reports_exact_pins_counts_and_performs_no_writes(self):
        before = self.snapshot()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no transport execution")):
            result = preflight_transport(self.transport)
        self.assertEqual("verified", result["state"])
        self.assertEqual(self.transport["manifest_sha256"], result["manifest_sha256"])
        self.assertEqual("b" * 40, result["source_sha"])
        self.assertEqual(5, result["file_count"])
        self.assertEqual(sum((self.install / name).stat().st_size for name in CLIENTS), result["total_bytes"])
        self.assertEqual(before, self.snapshot())

    def test_gateway_optional_ownership_schema_is_supported(self):
        self.document.update(schema_version=1, ownership={"fixture": "opaque gateway metadata"})
        self.save_manifest()
        self.assertEqual("verified", preflight_transport(self.transport)["state"])

    def test_shipped_bundle_has_room_for_three_more_contract_cuts(self):
        """Reserve the next indexed cut and two more against deployed readers."""
        from scripts.ledger_contract import archive_schema_path

        source = Path(__file__).resolve().parents[1]
        entries = json.loads((source / "bundle-manifest.v0.json").read_bytes())["files"]
        cut_files = Counter()
        cut_bytes = Counter()
        archived, live = set(), set()
        indexed_cuts = set()
        total = largest = 0
        for entry in entries:
            relative = entry["path"]
            size = (source / relative).stat().st_size
            total += size
            largest = max(largest, size)
            parts = relative.split("/")
            if len(parts) > 2 and parts[0] == "bundle" and parts[1].startswith("c7."):
                cut_files[parts[1]] += 1
                cut_bytes[parts[1]] += size
                if (len(parts) == 3 and parts[2] == "ledger-contract.json"
                        and json.loads((source / relative).read_bytes())["format_version"] == 2):
                    indexed_cuts.add(parts[1])
            if archive_schema_path(relative) is not None:
                archived.add(entry["sha256"])
            if relative.startswith("schemas/") and relative.endswith(".json"):
                live.add(entry["sha256"])
        # The next cut uses two envelope files and one file per novel schema digest.
        candidate_growth = 2 + len(live - archived)
        growth = max([candidate_growth] + [cut_files[version] for version in indexed_cuts])
        required = len(entries) + 3 * growth
        self.assertLessEqual(
            required, MIN_DEPLOYED_INSTALL_FILES,
            f"{len(entries)} shipped files and three cuts of {growth} files require {required} slots",
        )
        self.assertLessEqual(largest, MAX_INSTALL_FILE_BYTES)
        self.assertLessEqual(total + 3 * max(cut_bytes.values()), MAX_INSTALL_TOTAL_BYTES)
        self.assertEqual(
            "verified", preflight_transport({**self.transport, "max_files": required})["state"],
            f"{len(entries)} shipped files and three cuts of {growth} files require {required} slots",
        )

    def test_missing_install_is_refused(self):
        absent = self.base / "missing-install"
        self.refuses({**self.transport, "install_root": str(absent),
                      "manifest_path": str(absent / ".floati-install/manifest.v0.json")})

    def test_missing_interpreter_is_refused(self):
        self.interpreter.unlink()
        self.refuses()

    def test_nonexecutable_or_directory_interpreter_is_refused(self):
        self.interpreter.chmod(0o600)
        self.refuses()
        self.interpreter.unlink()
        self.interpreter.mkdir()
        self.refuses()

    def test_symlink_interpreter_is_refused(self):
        other = self.base / "other-python"
        self.interpreter.rename(other)
        self.interpreter.symlink_to(other)
        self.refuses()

    def test_missing_manifest_is_refused(self):
        self.manifest.unlink()
        self.refuses()

    def test_manifest_digest_and_source_pin_must_match_observed_bytes(self):
        self.transport["manifest_sha256"] = "f" * 64
        self.refuses()
        self.save_manifest()
        self.transport["source_sha"] = "c" * 40
        self.refuses()

    def test_manifest_closed_shape_duplicate_json_and_ownership_type_are_refused(self):
        original = dict(self.document)
        for document in ({**original, "foreign": True}, {**original, "ownership": []},
                         {key: value for key, value in original.items() if key != "source_ref"}):
            with self.subTest(document=document):
                self.document = document
                self.save_manifest()
                self.refuses()
        self.document = original
        encoded = json.dumps(original).replace('"schema_version": 0', '"schema_version": 0, "schema_version": 0').encode()
        self.save_manifest(encoded)
        self.refuses()

    def test_invalid_manifest_schema_and_source_ref_are_refused(self):
        for version in (True, -1, 2, "0"):
            with self.subTest(version=version):
                self.document["schema_version"] = version
                self.save_manifest()
                self.refuses()
        self.document["schema_version"] = 0
        for reference in ("", "bad ref", "x" * 1025, 1):
            with self.subTest(reference=reference):
                self.document["source_ref"] = reference
                self.save_manifest()
                self.refuses()

    def test_manifest_count_file_bytes_and_total_bytes_are_bounded(self):
        for updates in ({"max_files": 4}, {"max_file_bytes": 1},
                        {"max_file_bytes": 60, "max_total_bytes": 60}):
            with self.subTest(updates=updates):
                self.refuses({**self.transport, **updates})
        self.document["files"] = []
        self.save_manifest()
        self.refuses()

    def test_manifest_bytes_are_bounded_before_parsing(self):
        from floati.seat_transport_preflight import MAX_MANIFEST_BYTES
        self.save_manifest(b" " * (MAX_MANIFEST_BYTES + 1))
        self.assertIn("bounded ordinary file", self.refuses().detail)

    def test_special_manifest_file_is_refused_without_reading(self):
        from floati import seat_transport_preflight as preflight
        self.manifest.unlink()
        os.mkfifo(self.manifest, 0o600)
        before = self.manifest.lstat()
        with mock.patch.object(preflight.os, "read", side_effect=AssertionError("no FIFO read")):
            with self.assertRaises(ProtocolRefusal) as caught:
                preflight_transport(self.transport)
        self.assertEqual("seat_transport_preflight_invalid", caught.exception.code)
        self.assertTrue(caught.exception.remedy)
        after = self.manifest.lstat()
        self.assertEqual((before.st_dev, before.st_ino, before.st_mode),
                         (after.st_dev, after.st_ino, after.st_mode))

    def test_unsafe_and_duplicate_manifest_paths_are_refused(self):
        entries = self.document["files"]
        for name in ("/outside", "../outside", "floati/../outside", "floati//cli.py", "floati/./cli.py"):
            with self.subTest(path=name):
                self.document["files"] = [{**entries[0], "path": name}, *entries[1:]]
                self.save_manifest()
                self.refuses()
        self.document["files"] = [*entries, entries[0]]
        self.save_manifest()
        self.refuses()

    def test_fixed_client_entries_are_required(self):
        self.document["files"] = self.document["files"][:-1]
        self.save_manifest()
        self.refuses()

    def test_changed_or_missing_installed_client_bytes_are_refused(self):
        client = self.install / "floati/cli.py"
        client.write_bytes(b"changed after installation\n")
        self.refuses()
        client.unlink()
        self.refuses()

    def test_symlink_installed_client_and_parent_are_refused(self):
        client = self.install / "floati/cli.py"
        other = self.base / "foreign-client"
        client.rename(other)
        client.symlink_to(other)
        self.refuses()
        client.unlink()
        other.rename(client)
        parent = self.install / "floati"
        moved = self.base / "foreign-client-directory"
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        self.refuses()

    def test_manifest_changed_during_read_is_refused_without_repair(self):
        from floati import seat_transport_preflight as preflight
        native_read = preflight.os.read
        changed = []
        def read_then_change(descriptor, count):
            encoded = native_read(descriptor, count)
            if encoded and not changed:
                changed.append(True)
                with self.manifest.open("ab") as handle:
                    handle.write(b" ")
            return encoded
        with mock.patch.object(preflight.os, "read", read_then_change):
            with self.assertRaises(ProtocolRefusal) as caught:
                preflight_transport(self.transport)
        self.assertIn("changed", caught.exception.detail)
        self.assertTrue(changed)
        self.assertTrue(self.manifest.read_bytes().endswith(b" "))

    def test_parent_replaced_with_same_inode_symlink_during_read_is_refused(self):
        from floati import seat_transport_preflight as preflight
        native_read = preflight.os.read
        changed = []
        moved = self.install / "moved-metadata"
        def read_then_replace_parent(descriptor, count):
            encoded = native_read(descriptor, count)
            if encoded and not changed:
                changed.append(True)
                self.manifest.parent.rename(moved)
                self.manifest.parent.symlink_to(moved, target_is_directory=True)
            return encoded
        with mock.patch.object(preflight.os, "read", read_then_replace_parent):
            with self.assertRaises(ProtocolRefusal) as caught:
                preflight_transport(self.transport)
        self.assertEqual("seat_transport_preflight_invalid", caught.exception.code)
        self.assertTrue(changed)
        self.assertTrue(self.manifest.parent.is_symlink())
        self.assertEqual(self.transport["manifest_sha256"],
                         hashlib.sha256(self.manifest.read_bytes()).hexdigest())

    def test_transport_closed_metadata_and_bound_types_are_refused(self):
        for updates in ({"foreign": True}, {"kind": "foreign"}, {"max_files": True},
                        {"max_files": MAX_INSTALL_FILES + 1}, {"max_file_bytes": 67108865},
                        {"max_total_bytes": 268435457}, {"source_sha": "z" * 40},
                        {"manifest_sha256": "short"}, {"max_file_bytes": 100, "max_total_bytes": 99}):
            with self.subTest(updates=updates):
                self.refuses({**self.transport, **updates})

    def test_manifest_coordinate_must_be_exact_install_metadata_path(self):
        self.refuses({**self.transport, "manifest_path": str(self.base / "foreign-manifest.json")})
        self.refuses({**self.transport, "install_root": "relative-install"})


if __name__ == "__main__":
    unittest.main()
