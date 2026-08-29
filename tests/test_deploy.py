from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.uninstall import UninstallWriter

try:
    import floati.deploy as deploy
    from floati.deploy import DeploymentWriter
except (ImportError, ModuleNotFoundError):
    deploy = None
    DeploymentWriter = None


class DeploymentWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self._git("init", "--quiet", "--initial-branch=lane/hm0")
        self._git("config", "user.name", "Floati Test")
        self._git("config", "user.email", "floati-test@example.invalid")
        self._write_source("floati/__init__.py", "VERSION = 'one'\n")
        self._write_source("schemas/v0/example.json", '{"schema_version":0}\n')
        self._write_source("scripts/floati", "#!/bin/sh\nexit 0\n")
        (self.source / "scripts/floati").chmod(0o755)
        self._write_source("scripts/floati-codex-wait", "#!/bin/sh\nexit 0\n")
        (self.source / "scripts/floati-codex-wait").chmod(0o755)
        self._write_manifest()
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "source one")
        self.git_directory = Path(shutil.which("git") or "").parent
        self.assertTrue((self.git_directory / "git").is_file())
        self._path_patch = patch.dict(
            os.environ,
            {
                "PATH": os.pathsep.join((
                    str(self.source / "scripts"),
                    str(self.git_directory),
                ))
            },
            clear=False,
        )
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.source,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _write_source(self, relative: str, content: str) -> None:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_manifest(self) -> None:
        paths = [
            relative
            for relative in (
                "schemas/v0/example.json",
                "scripts/floati",
                "scripts/floati-codex-wait",
                "floati/__init__.py",
            )
            if (self.source / relative).is_file()
        ]
        files = [
            {
                "path": relative,
                "sha256": hashlib.sha256((self.source / relative).read_bytes()).hexdigest(),
            }
            for relative in sorted(paths)
        ]
        (self.source / "bundle-manifest.v0.json").write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "protocol_version": "0",
                    "canonical_ref": "refs/heads/lane/hm0",
                    "files": files,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _writer(self, destination: Path, **kwargs: object) -> object:
        self.assertIsNotNone(DeploymentWriter, "deployment writer must exist")
        return DeploymentWriter(
            self.source,
            destination,
            "install",
            ref="HEAD",
            **kwargs,
        )

    def _installed_path(self, destination: Path) -> str:
        return os.pathsep.join((
            str(self.source / "scripts"),
            str(destination / "scripts"),
            str(self.git_directory),
        ))

    @staticmethod
    def _tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        }

    def _advance_source_without_the_schema(self) -> None:
        (self.source / "schemas/v0/example.json").unlink()
        self._write_manifest()
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "source two")

    def test_currency_check_runs_before_any_destination_write(self) -> None:
        destination = self.base / "destination"
        (self.source / "floati/__init__.py").write_text("VERSION = 'dirty'\n", encoding="utf-8")

        with self.assertRaisesRegex(Exception, "deployment_currency_unavailable"):
            self._writer(destination).run()

        self.assertFalse(destination.exists())

    def test_legacy_destination_refuses_before_currency_metadata_or_staging(self) -> None:
        self.assertIsNotNone(
            getattr(deploy, "refuse_legacy_workspace_artifacts", None),
            "deployment must preflight legacy destination artifacts",
        )
        destination = self.base / "destination"
        destination.mkdir()
        legacy = destination / ".slipway-install"
        contents = b"deployment legacy sentinel\n"
        legacy.write_bytes(contents)
        metadata = legacy.lstat()

        with (
            patch.object(
                DeploymentWriter,
                "_check_currency",
                side_effect=AssertionError("legacy refusal must precede Git currency reads"),
            ),
            patch.object(
                deploy,
                "_manifest_entries",
                side_effect=AssertionError("legacy refusal must precede manifest reads"),
            ),
            patch.object(
                deploy,
                "_load_previous",
                side_effect=AssertionError("legacy refusal must precede install metadata reads"),
            ),
            patch.object(
                DeploymentWriter,
                "_write_metadata",
                side_effect=AssertionError("legacy refusal must precede install staging"),
            ),
            self.assertRaises(ProtocolRefusal) as raised,
        ):
            self._writer(destination, committed_tree=True).run()

        self.assertEqual("legacy_workspace_artifacts", raised.exception.code)
        self.assertEqual(
            "workspace refused: legacy artifact '.slipway-install' predates the Floati rename; nothing was read, migrated, or deleted; start a fresh root, or archive the legacy artifacts yourself and run again",
            raised.exception.detail,
        )
        current = legacy.lstat()
        self.assertTrue(os.path.lexists(legacy))
        self.assertEqual((metadata.st_dev, metadata.st_ino), (current.st_dev, current.st_ino))
        self.assertEqual(contents, legacy.read_bytes())
        self.assertFalse(os.path.lexists(destination / ".floati-install"))
        self.assertFalse(os.path.lexists(destination / "floati"))

    def test_committed_tree_mode_is_explicit_and_printed(self) -> None:
        result = self._writer(self.base / "destination", committed_tree=True).run()

        self.assertEqual("committed-tree-ci", result["currency_mode"])
        self.assertEqual("HEAD", result["ref"])
        self.assertEqual("installed", result["status"])

    def test_install_copies_exact_manifest_set(self) -> None:
        destination = self.base / "destination"
        result = self._writer(destination, committed_tree=True).run()

        self.assertEqual(
            [
                "floati/__init__.py",
                "schemas/v0/example.json",
                "scripts/floati",
                "scripts/floati-codex-wait",
            ],
            result["managed_paths"],
        )
        self.assertEqual(
            set(result["managed_paths"]),
            {
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file() and ".floati-install" not in path.parts
            },
        )
        self.assertTrue((destination / ".floati-install/manifest.v0.json").is_file())
        self.assertFalse(os.path.lexists(destination / ".slipway-install"))

    def test_install_dry_run_uninstall_round_trip_covers_the_owned_script_set(self) -> None:
        """Catches install accepting an owned script that uninstall later refuses."""
        destination = self.base / "destination"
        installed = self._writer(destination, committed_tree=True).run()
        ledger = destination / "puddle-fleet" / "registry" / "entries.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_bytes(b"durable fleet evidence\n")

        preview = UninstallWriter(destination, dry_run=True).run()

        self.assertEqual(0, preview["removed_count"])
        self.assertEqual(
            [*installed["managed_paths"], ".floati-install/manifest.v0.json"],
            [receipt["path"] for receipt in preview["removal_receipts"]],
        )
        self.assertTrue((destination / "scripts/floati-codex-wait").is_file())

        removed = UninstallWriter(destination).run()

        self.assertEqual(len(installed["managed_paths"]) + 1, removed["removed_count"])
        self.assertFalse((destination / "scripts/floati-codex-wait").exists())
        self.assertEqual(b"durable fleet evidence\n", ledger.read_bytes())
        self.assertIn("puddle-fleet/registry/entries.jsonl", removed["foreign_preserved"])

    def test_first_install_refuses_found_shadow_before_destination_creation(self) -> None:
        """A known preceding executable must block writes before the destination exists."""
        destination = self.base / "destination"
        shadow = self.base / "shadow"
        shadow.mkdir()
        shadow_floati = shadow / "floati"
        shadow_floati.write_bytes(b"shadow\n")
        path = os.pathsep.join((
            str(shadow),
            str(self.source / "scripts"),
            str(self.git_directory),
        ))

        with patch.dict(os.environ, {"PATH": path}, clear=False):
            with self.assertRaises(ProtocolRefusal) as raised:
                self._writer(destination, committed_tree=True).run()

        self.assertEqual("deployment_shadow_found", raised.exception.code)
        self.assertEqual(
            "A floati ahead of the installed copy answered first on PATH.",
            raised.exception.detail,
        )
        self.assertFalse(destination.exists())

    def test_first_install_refuses_unknown_path_before_destination_creation(self) -> None:
        """An unenumerable PATH entry must not become a clean preflight result."""
        destination = self.base / "destination"
        missing = self.base / "unreadable-path-entry"
        path = os.pathsep.join((
            str(self.source / "scripts"),
            str(missing),
            str(self.git_directory),
        ))

        with patch.dict(os.environ, {"PATH": path}, clear=False):
            with self.assertRaises(ProtocolRefusal) as raised:
                self._writer(destination, committed_tree=True).run()

        self.assertEqual("deployment_shadow_unknown", raised.exception.code)
        self.assertEqual(
            "Some PATH entries could not be read; shadow state unknown.",
            raised.exception.detail,
        )
        self.assertFalse(destination.exists())

    def test_first_install_writes_only_after_affirmative_none_preflight(self) -> None:
        """The source launcher is excluded and an empty complete scan permits the first copy."""
        destination = self.base / "destination"
        path = os.pathsep.join((
            str(self.source / "scripts"),
            str(self.git_directory),
        ))

        with patch.dict(os.environ, {"PATH": path}, clear=False):
            result = self._writer(destination, committed_tree=True).run()

        self.assertTrue((destination / "scripts" / "floati").is_file())
        self.assertFalse((destination / "scripts" / "slip").exists())
        self.assertEqual(
            {
                "outcome": "affirmative_none",
                "enumerated_roots": [
                    str((self.source / "scripts").resolve()),
                    str(self.git_directory.resolve()),
                ],
                "found": [],
                "reason": "Every PATH entry was checked; the installed floati answers first.",
            },
            result["installer_shadow"],
        )

    def test_update_refuses_found_shadow_before_mutating_an_existing_destination(self) -> None:
        """A prior installation must remain byte-identical when a shadow appears before it."""
        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        self._advance_source_without_the_schema()
        before = self._tree_bytes(destination)
        shadow = self.base / "shadow"
        shadow.mkdir()
        (shadow / "floati").write_text("shadow\n", encoding="utf-8")
        path = os.pathsep.join((
            str(shadow),
            str(self.source / "scripts"),
            str(destination / "scripts"),
            str(self.git_directory),
        ))

        with patch.dict(os.environ, {"PATH": path}, clear=False):
            with self.assertRaises(ProtocolRefusal) as raised:
                DeploymentWriter(
                    self.source, destination, "update", ref="HEAD", committed_tree=True
                ).run()

        self.assertEqual("deployment_shadow_found", raised.exception.code)
        self.assertEqual(before, self._tree_bytes(destination))

    def test_update_refuses_partial_path_before_mutating_an_existing_destination(self) -> None:
        """An unenumerable exact PATH entry must protect an existing installation too."""
        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        self._advance_source_without_the_schema()
        before = self._tree_bytes(destination)
        missing = self.base / "unreadable-path-entry"
        path = os.pathsep.join((
            str(self.source / "scripts"),
            str(missing),
            str(destination / "scripts"),
            str(self.git_directory),
        ))

        with patch.dict(os.environ, {"PATH": path}, clear=False):
            with self.assertRaises(ProtocolRefusal) as raised:
                DeploymentWriter(
                    self.source, destination, "update", ref="HEAD", committed_tree=True
                ).run()

        self.assertEqual("deployment_shadow_unknown", raised.exception.code)
        self.assertEqual(before, self._tree_bytes(destination))

    def test_update_removes_only_owned_unchanged_stale_files(self) -> None:
        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        stale = destination / "schemas/v0/example.json"
        self.assertTrue(stale.exists())
        self._advance_source_without_the_schema()

        with patch.dict(os.environ, {"PATH": self._installed_path(destination)}, clear=False):
            result = DeploymentWriter(
                self.source,
                destination,
                "update",
                ref="HEAD",
                committed_tree=True,
            ).run()

        self.assertFalse(stale.exists())
        self.assertNotIn("schemas/v0/example.json", result["managed_paths"])

    def test_update_preserves_foreign_and_modified_files(self) -> None:
        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        foreign = destination / "foreign.txt"
        foreign.write_text("other tenant\n", encoding="utf-8")
        stale = destination / "schemas/v0/example.json"
        stale.write_text("modified by foreign\n", encoding="utf-8")
        self._advance_source_without_the_schema()

        with patch.dict(os.environ, {"PATH": self._installed_path(destination)}, clear=False):
            result = DeploymentWriter(
                self.source,
                destination,
                "update",
                ref="HEAD",
                committed_tree=True,
            ).run()

        self.assertEqual("other tenant\n", foreign.read_text(encoding="utf-8"))
        self.assertEqual("modified by foreign\n", stale.read_text(encoding="utf-8"))
        self.assertIn("foreign.txt", result["foreign_preserved"])
        self.assertIn("schemas/v0/example.json", result["foreign_preserved"])

    def test_update_preserves_foreign_symlink_stale_file(self) -> None:
        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        stale = destination / "schemas/v0/example.json"
        outside = self.base / "foreign-target.txt"
        outside.write_text("foreign target\n", encoding="utf-8")
        stale.unlink()
        stale.symlink_to(outside)
        self._advance_source_without_the_schema()

        with patch.dict(os.environ, {"PATH": self._installed_path(destination)}, clear=False):
            result = DeploymentWriter(
                self.source,
                destination,
                "update",
                ref="HEAD",
                committed_tree=True,
            ).run()

        self.assertTrue(stale.is_symlink())
        self.assertEqual("foreign target\n", outside.read_text(encoding="utf-8"))
        self.assertIn("schemas/v0/example.json", result["foreign_preserved"])

    def test_source_and_destination_symlink_entry_points_refuse(self) -> None:
        source_link = self.base / "source-link"
        source_link.symlink_to(self.source, target_is_directory=True)
        destination = self.base / "destination"
        with self.assertRaisesRegex(Exception, "deployment_symlinked_entry"):
            DeploymentWriter(source_link, destination, "install", committed_tree=True).run()

        real_destination = self.base / "real-destination"
        real_destination.mkdir()
        destination_link = self.base / "destination-link"
        destination_link.symlink_to(real_destination, target_is_directory=True)
        with self.assertRaisesRegex(Exception, "deployment_symlinked_entry"):
            DeploymentWriter(self.source, destination_link, "install", committed_tree=True).run()

    def test_foreign_collision_refuses_without_mutation(self) -> None:
        destination = self.base / "destination"
        collision = destination / "floati/__init__.py"
        collision.parent.mkdir(parents=True)
        collision.write_text("foreign\n", encoding="utf-8")
        before = collision.read_bytes()

        with self.assertRaisesRegex(Exception, "deployment_foreign_collision"):
            self._writer(destination, committed_tree=True).run()

        self.assertEqual(before, collision.read_bytes())
        self.assertFalse((destination / ".floati-install").exists())
        self.assertFalse(os.path.lexists(destination / ".slipway-install"))


if __name__ == "__main__":
    unittest.main()
