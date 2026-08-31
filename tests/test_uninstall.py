from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from floati.errors import ProtocolRefusal
from floati.uninstall import UninstallWriter, _owned_tool_path


class UninstallWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.destination = self.base / "install"
        self.destination.mkdir()

    @staticmethod
    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def install_file(self, relative: str, payload: bytes) -> dict[str, str]:
        path = self.destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return {"path": relative, "sha256": self.digest(payload)}

    def write_manifest(
        self,
        entries: list[dict[str, str]],
        *,
        schema_version: int = 0,
        ownership: dict[str, object] | None = None,
    ) -> Path:
        metadata = self.destination / ".floati-install" / "manifest.v0.json"
        metadata.parent.mkdir()
        payload: dict[str, object] = {
            "schema_version": schema_version,
            "source_ref": "refs/heads/main",
            "source_sha": "a" * 40,
            "files": entries,
        }
        if ownership is not None:
            payload["ownership"] = ownership
        metadata.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return metadata

    def test_uninstall_accepts_strict_schema_v1_ownership_metadata(self) -> None:
        """Catches v1 installs becoming impossible to remove safely."""

        owned = self.install_file("scripts/floati", b"#!/bin/sh\n")
        ownership = {
            "kind": "floati_standalone",
            "destination": str(self.destination.resolve()),
            "entrypoint": "scripts/floati",
            "entrypoint_sha256": owned["sha256"],
            "manager": None,
            "remedy": None,
        }
        self.write_manifest(
            [owned], schema_version=1, ownership=ownership
        )

        try:
            result = UninstallWriter(self.destination).run()
        except ProtocolRefusal as exc:
            self.fail(f"strict schema-v1 metadata must remain uninstallable: {exc}")

        self.assertEqual(2, result["removed_count"])
        self.assertFalse((self.destination / "scripts/floati").exists())

    def test_uninstall_preserves_every_file_absent_from_the_ownership_manifest(self) -> None:
        """Catches recursive destination pruning that deletes a foreign file."""
        owned = self.install_file("floati/runtime.py", b"owned\n")
        foreign = self.destination / "operator-notes.txt"
        foreign.write_text("not installed by Floati\n", encoding="utf-8")
        self.write_manifest([owned])

        result = UninstallWriter(self.destination).run()

        self.assertFalse((self.destination / "floati/runtime.py").exists())
        self.assertEqual("not installed by Floati\n", foreign.read_text(encoding="utf-8"))
        self.assertEqual(["operator-notes.txt"], result["foreign_preserved"])

    def test_uninstall_never_opens_or_removes_a_bus_ledger_inside_the_destination(self) -> None:
        """Catches uninstall treating durable bus data as disposable tool bytes."""
        owned = self.install_file("scripts/floati", b"#!/bin/sh\n")
        ledger = self.destination / "demo-fleet" / "registry" / "entries.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_bytes(b"durable ledger bytes\n")
        before = ledger.stat()
        self.write_manifest([owned])

        result = UninstallWriter(self.destination).run()

        after = ledger.stat()
        self.assertEqual(b"durable ledger bytes\n", ledger.read_bytes())
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual(
            "Bus roots and ledgers are retained; uninstall removes tool files only.",
            result["data_retention_notice"],
        )

    def test_dry_run_lists_the_complete_digest_bound_removal_set_without_mutation(self) -> None:
        """Catches a partial preview or a dry run that mutates installed bytes."""
        first = self.install_file("floati/a.py", b"a\n")
        second = self.install_file("scripts/floati", b"b\n")
        metadata = self.write_manifest([first, second])
        metadata_digest = self.digest(metadata.read_bytes())
        before = {
            path.relative_to(self.destination): path.read_bytes()
            for path in self.destination.rglob("*")
            if path.is_file()
        }

        result = UninstallWriter(self.destination, dry_run=True).run()

        self.assertTrue(result["dry_run"])
        self.assertEqual(
            [
                {"path": "floati/a.py", "sha256": first["sha256"]},
                {"path": "scripts/floati", "sha256": second["sha256"]},
                {
                    "path": ".floati-install/manifest.v0.json",
                    "sha256": metadata_digest,
                },
            ],
            result["removal_receipts"],
        )
        self.assertEqual(
            before,
            {
                path.relative_to(self.destination): path.read_bytes()
                for path in self.destination.rglob("*")
                if path.is_file()
            },
        )
        self.assertTrue(metadata.is_file())

    def test_uninstall_refuses_a_missing_manifest_without_removing_any_file(self) -> None:
        """Catches uninstall guessing ownership when installation metadata is absent."""
        installed = self.destination / "floati/runtime.py"
        installed.parent.mkdir()
        installed.write_text("unknown ownership\n", encoding="utf-8")

        with self.assertRaises(ProtocolRefusal) as raised:
            UninstallWriter(self.destination).run()

        self.assertEqual("uninstall_manifest_missing", raised.exception.code)
        self.assertEqual("unknown ownership\n", installed.read_text(encoding="utf-8"))

    def test_uninstall_refuses_any_digest_mismatch_before_removing_an_owned_file(self) -> None:
        """Catches mutation beginning before all manifest identities are validated."""
        first = self.install_file("floati/a.py", b"a\n")
        second = self.install_file("floati/b.py", b"b\n")
        self.write_manifest([first, second])
        (self.destination / "floati/b.py").write_bytes(b"foreign replacement\n")

        with self.assertRaises(ProtocolRefusal) as raised:
            UninstallWriter(self.destination).run()

        self.assertEqual("uninstall_manifest_mismatch", raised.exception.code)
        self.assertEqual(b"a\n", (self.destination / "floati/a.py").read_bytes())
        self.assertEqual(b"foreign replacement\n", (self.destination / "floati/b.py").read_bytes())

    def test_uninstall_refuses_same_inode_same_size_content_drift_at_removal_boundary(self) -> None:
        """Catches an in-place foreign rewrite between preflight and unlink."""
        owned = self.install_file("floati/a.py", b"a\n")
        self.write_manifest([owned])
        writer = UninstallWriter(self.destination)
        load_manifest = writer._load_manifest

        def load_then_replace(destination: Path):
            result = load_manifest(destination)
            (self.destination / "floati/a.py").write_bytes(b"z\n")
            return result

        writer._load_manifest = load_then_replace  # type: ignore[method-assign]

        with self.assertRaises(ProtocolRefusal) as raised:
            writer.run()

        self.assertEqual("uninstall_manifest_mismatch", raised.exception.code)
        self.assertEqual(b"z\n", (self.destination / "floati/a.py").read_bytes())

    def test_uninstall_accepts_every_current_bundle_manifest_owned_path(self) -> None:
        """Re-gates uninstall whenever the install-owned manifest set changes."""
        repository = Path(__file__).resolve().parents[1]
        manifest = json.loads(
            (repository / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )

        rejected = [
            entry["path"]
            for entry in manifest["files"]
            if not _owned_tool_path(PurePosixPath(entry["path"]))
        ]

        self.assertEqual([], rejected)


if __name__ == "__main__":
    unittest.main()
