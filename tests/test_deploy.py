from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.identity_fence import (
    RETIRED_PRODUCT_NAME,
    RETIRED_PRODUCT_SHORT_NAME,
)
from floati.uninstall import UninstallWriter

# The dot-prefixed workspace name the pre-rename product wrote, built from
# the fence's own governed token rather than spelled: these fixtures drive a
# refusal (or assert an absence) whose whole mechanism is these exact bytes.
LEGACY_PREFIX = "." + RETIRED_PRODUCT_NAME

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

    def test_deployment_git_defaults_are_fixed_absolute_vectors(self) -> None:
        self.assertEqual(
            "/usr/bin/git",
            inspect.signature(deploy._manifest_entries)
            .parameters["git_executable"]
            .default,
        )
        self.assertEqual(
            "/usr/bin/git",
            inspect.signature(DeploymentWriter.__init__)
            .parameters["git_executable"]
            .default,
        )

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
        legacy = destination / f"{LEGACY_PREFIX}-install"
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
            f"workspace refused: legacy artifact '{LEGACY_PREFIX}-install' predates the Floati rename; nothing was read, migrated, or deleted; start a fresh root, or archive the legacy artifacts yourself and run again",
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

    def test_deterministic_join_id_stamps_every_writer_journal_entry(self) -> None:
        """Catches governed shared installs whose journal cannot be joined to one run."""

        destination = self.base / "destination"
        join_id = "a" * 64

        result = self._writer(
            destination, committed_tree=True, join_id=join_id
        ).run()

        entries = deploy.wiring_journal.read_entries(
            deploy.wiring_journal.journal_path(destination)
        )
        self.assertEqual(join_id, result["wiring_join_id"])
        self.assertEqual(
            {join_id}, {entry.payload.get("join_id") for entry in entries}
        )

    def test_joined_prefix_before_file_write_resumes_without_duplicate_entry(self) -> None:
        """Catches a writer retry duplicating a valid journal-before-file prefix."""

        destination = self.base / "destination"
        join_id = "b" * 64
        destination.mkdir()
        manifest = json.loads((self.source / "bundle-manifest.v0.json").read_text(encoding="utf-8"))
        first = sorted(manifest["files"], key=lambda row: row["path"])[0]
        deploy.wiring_journal.append_entry(destination, {
            "v": 1, "ts": "2026-08-30T00:00:00Z",
            "actor": {"command": "install", "floatiVersion": "fixture"},
            "action": "install", "kind": "file",
            "path": str(destination / first["path"]), "op": "create",
            "sha256": first["sha256"], "join_id": join_id,
        })
        result = self._writer(destination, committed_tree=True, join_id=join_id).run()
        joined = [entry for entry in deploy.wiring_journal.read_entries(
            deploy.wiring_journal.journal_path(destination)
        ) if entry.payload.get("join_id") == join_id]
        self.assertEqual(len(manifest["files"]) + 1, len(joined))
        self.assertEqual(first["sha256"], hashlib.sha256((destination / first["path"]).read_bytes()).hexdigest())
        self.assertEqual(join_id, result["wiring_join_id"])

    def test_joined_prefix_refuses_an_unjournaled_future_post_state(self) -> None:
        """Catches recovery treating an unjoined future target byte as completed."""

        destination = self.base / "destination"
        join_id = "c" * 64
        destination.mkdir()
        manifest = json.loads((self.source / "bundle-manifest.v0.json").read_text(encoding="utf-8"))
        rows = sorted(manifest["files"], key=lambda row: row["path"])
        deploy.wiring_journal.append_entry(destination, {
            "v": 1, "ts": "2026-08-30T00:00:00Z",
            "actor": {"command": "install", "floatiVersion": "fixture"},
            "action": "install", "kind": "file",
            "path": str(destination / rows[0]["path"]), "op": "create",
            "sha256": rows[0]["sha256"], "join_id": join_id,
        })
        future = destination / rows[1]["path"]
        future.parent.mkdir(parents=True)
        shutil.copy2(self.source / rows[1]["path"], future)

        with self.assertRaises(ProtocolRefusal) as caught:
            self._writer(destination, committed_tree=True, join_id=join_id).run()

        self.assertEqual("deployment_join_invalid", caught.exception.code)
        self.assertEqual(1, len(deploy.wiring_journal.read_entries(
            deploy.wiring_journal.journal_path(destination)
        )))

    def test_joined_metadata_post_state_appends_only_missing_metadata_row(self) -> None:
        """Catches retry reclassifying target metadata as its original pre-state."""

        destination = self.base / "destination"
        join_id = "d" * 64
        destination.mkdir()
        manifest = json.loads((self.source / "bundle-manifest.v0.json").read_text(encoding="utf-8"))
        rows = sorted(manifest["files"], key=lambda row: row["path"])
        for row in rows:
            target = destination / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            deploy.wiring_journal.append_entry(destination, {
                "v": 1, "ts": "2026-08-30T00:00:00Z",
                "actor": {"command": "install", "floatiVersion": "fixture"},
                "action": "install", "kind": "file", "path": str(target),
                "op": "create", "sha256": row["sha256"], "join_id": join_id,
            })
            shutil.copy2(self.source / row["path"], target)
        metadata = deploy.render_install_metadata(
            destination=destination.resolve(strict=True), source_ref="HEAD", source_sha=self._git("rev-parse", "HEAD"),
            entries=rows,
            entrypoint_sha256=next(row["sha256"] for row in rows if row["path"] == "scripts/floati"),
            previous_ownership=self._writer(destination, committed_tree=True)._ownership(destination, None),
        )
        metadata_path = destination / ".floati-install" / "manifest.v0.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_bytes(metadata)
        before = metadata_path.read_bytes()

        with patch.dict(os.environ, {"PATH": self._installed_path(destination)}, clear=False):
            self._writer(destination, committed_tree=True, join_id=join_id).run()

        joined = [entry for entry in deploy.wiring_journal.read_entries(
            deploy.wiring_journal.journal_path(destination)
        ) if entry.payload.get("join_id") == join_id]
        self.assertEqual(len(rows) + 1, len(joined))
        self.assertEqual(before, metadata_path.read_bytes())

    def test_journal_before_atomic_file_write_retries_the_terminal_pre_state(self) -> None:
        """Catches a post-journal crash either duplicating evidence or skipping its file."""

        destination = self.base / "destination"
        join_id = "e" * 64
        calls: list[str] = []

        def fail_after_first_journal(event: str) -> None:
            calls.append(event)
            if event == "after_file_journal":
                raise OSError("fixture after journal before atomic file write")

        with self.assertRaisesRegex(OSError, "after journal"):
            DeploymentWriter(
                self.source, destination, "install", ref="HEAD", committed_tree=True,
                join_id=join_id, fault_hook=fail_after_first_journal,
            ).run()
        first = deploy.wiring_journal.read_entries(deploy.wiring_journal.journal_path(destination))
        self.assertEqual(1, len(first))
        self.assertFalse((destination / "floati/__init__.py").exists())

        DeploymentWriter(
            self.source, destination, "install", ref="HEAD", committed_tree=True,
            join_id=join_id,
        ).run()
        joined = [entry for entry in deploy.wiring_journal.read_entries(
            deploy.wiring_journal.journal_path(destination)
        ) if entry.payload.get("join_id") == join_id]
        self.assertEqual(5, len(joined))
        self.assertEqual(1, calls.count("after_file_journal"))

    def test_joined_replace_terminal_pre_state_retries_without_second_row(self) -> None:
        """Catches replace recovery treating the old owned byte as foreign drift."""

        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        self._write_source("floati/__init__.py", "VERSION = 'two'\n")
        self._write_manifest()
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "source two")
        join_id = "f" * 64
        faulted = False

        def fault_after_replace_journal(event: str) -> None:
            nonlocal faulted
            if event == "after_file_journal" and not faulted:
                faulted = True
                raise OSError("fixture replace journal interruption")

        with patch.dict(os.environ, {"PATH": self._installed_path(destination)}, clear=False):
            with self.assertRaisesRegex(OSError, "replace journal"):
                DeploymentWriter(
                    self.source, destination, "update", ref="HEAD", committed_tree=True,
                    join_id=join_id, fault_hook=fault_after_replace_journal,
                ).run()
            self.assertEqual(b"VERSION = 'one'\n", (destination / "floati/__init__.py").read_bytes())
            DeploymentWriter(
                self.source, destination, "update", ref="HEAD", committed_tree=True,
                join_id=join_id,
            ).run()

        joined = [entry for entry in deploy.wiring_journal.read_entries(
            deploy.wiring_journal.journal_path(destination)
        ) if entry.payload.get("join_id") == join_id]
        self.assertEqual(5, len(joined))
        self.assertEqual(b"VERSION = 'two'\n", (destination / "floati/__init__.py").read_bytes())

    def test_post_replace_retry_fsyncs_and_reads_back_without_a_second_replace(self) -> None:
        """Catches a retry receipting bytes after replace without re-establishing durability."""

        destination = self.base / "destination"
        join_id = "1" * 64
        replaced = destination / "floati/__init__.py"
        original_replace = deploy.os.replace
        replaces: list[str] = []

        def count_replace(source: object, target: object) -> None:
            replaces.append(str(target))
            original_replace(source, target)

        def crash_after_replace(event: str) -> None:
            if event == "after_file_replace":
                raise OSError("fixture after replace before directory fsync")

        with patch.object(deploy.os, "replace", new=count_replace):
            with self.assertRaisesRegex(OSError, "after replace"):
                DeploymentWriter(
                    self.source, destination, "install", ref="HEAD", committed_tree=True,
                    join_id=join_id, fault_hook=crash_after_replace,
                ).run()
            retry_events: list[str] = []
            DeploymentWriter(
                self.source, destination, "install", ref="HEAD", committed_tree=True,
                join_id=join_id, fault_hook=retry_events.append,
            ).run()

        self.assertEqual(1, replaces.count(str(replaced)))
        self.assertIn("after_file_directory_fsync", retry_events)
        self.assertIn("after_file_readback", retry_events)

    def test_new_managed_parent_chain_is_durably_linked(self) -> None:
        """Every newly linked ancestor is fsynced with its containing directory."""

        destination = self.base / "destination"
        destination.mkdir()
        observed: list[int] = []
        real_fsync = deploy.os.fsync

        def observe(descriptor: int) -> None:
            observed.append(os.fstat(descriptor).st_ino)
            real_fsync(descriptor)

        with patch.object(deploy.os, "fsync", side_effect=observe):
            target = deploy._ensure_parent(destination, "new/deep/managed.py")

        new = destination / "new"
        deep = new / "deep"
        self.assertEqual(destination / "new/deep/managed.py", target)
        self.assertEqual(
            [new.stat().st_ino, destination.stat().st_ino,
             deep.stat().st_ino, new.stat().st_ino],
            observed,
        )

        # Visibility does not prove that the containing-directory fsync from
        # a predecessor returned.  An exact retry must replay the same links.
        observed.clear()
        with patch.object(deploy.os, "fsync", side_effect=observe):
            self.assertEqual(
                target,
                deploy._ensure_parent(destination, "new/deep/managed.py"),
            )
        self.assertEqual(
            [new.stat().st_ino, destination.stat().st_ino,
             deep.stat().st_ino, new.stat().st_ino],
            observed,
        )

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
        self.assertFalse(os.path.lexists(destination / f"{LEGACY_PREFIX}-install"))

    def test_install_metadata_preserves_explicit_writer_source_ref(self) -> None:
        """Catches bundle canonical identity replacing the writer's selected Git ref."""

        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        bundle = json.loads(
            (self.source / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        metadata = json.loads(
            (destination / ".floati-install/manifest.v0.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual("refs/heads/lane/hm0", bundle["canonical_ref"])
        self.assertEqual("HEAD", metadata["source_ref"])

    def test_package_recipe_writes_explicit_manager_ownership(self) -> None:
        """Catches an explicit package owner being replaced by standalone authority."""

        destination = self.base / "destination"
        owner = {
            "kind": "package_manager",
            "manager": "homebrew",
            "remedy": "DRAFT - run brew upgrade floati",
        }
        try:
            writer = DeploymentWriter(
                self.source,
                destination,
                "install",
                ref="HEAD",
                committed_tree=True,
                installation_owner=owner,
            )
        except TypeError as exc:
            self.fail(f"deployment writer must accept explicit ownership: {exc}")

        writer.run()

        metadata = json.loads(
            (destination / ".floati-install/manifest.v0.json").read_text(
                encoding="utf-8"
            )
        )
        entrypoint = destination / "scripts" / "floati"
        self.assertEqual(1, metadata["schema_version"])
        self.assertEqual(
            {
                "kind": "package_manager",
                "destination": str(destination.resolve()),
                "entrypoint": "scripts/floati",
                "entrypoint_sha256": hashlib.sha256(
                    entrypoint.read_bytes()
                ).hexdigest(),
                "manager": "homebrew",
                "remedy": "DRAFT - run brew upgrade floati",
            },
            metadata.get("ownership"),
        )

    def test_invalid_explicit_owner_refuses_before_destination_mutation(self) -> None:
        """Catches malformed package ownership being discovered after file copies."""

        destination = self.base / "destination"
        with self.assertRaises(ProtocolRefusal) as raised:
            DeploymentWriter(
                self.source,
                destination,
                "install",
                ref="HEAD",
                committed_tree=True,
                installation_owner={
                    "kind": "package_manager",
                    "manager": "homebrew",
                    "remedy": None,
                },
            ).run()

        self.assertEqual("deployment_owner_invalid", raised.exception.code)
        self.assertFalse(destination.exists())

    def test_legacy_update_preserves_unknown_ownership(self) -> None:
        """Catches an old receipt being silently promoted during an update."""

        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        metadata_path = destination / ".floati-install/manifest.v0.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["schema_version"] = 0
        metadata.pop("ownership", None)
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._advance_source_without_the_schema()

        with patch.dict(
            os.environ, {"PATH": self._installed_path(destination)}, clear=False
        ):
            DeploymentWriter(
                self.source,
                destination,
                "update",
                ref="HEAD",
                committed_tree=True,
            ).run()

        updated = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(1, updated["schema_version"])
        self.assertEqual("unknown", updated.get("ownership", {}).get("kind"))
        self.assertEqual(
            "reinstall with the governed standalone installer",
            updated.get("ownership", {}).get("remedy"),
        )

    def test_install_dry_run_uninstall_round_trip_covers_the_owned_script_set(self) -> None:
        """Catches install accepting an owned script that uninstall later refuses."""
        destination = self.base / "destination"
        installed = self._writer(destination, committed_tree=True).run()
        ledger = destination / "demo-fleet" / "registry" / "entries.jsonl"
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
        self.assertIn("demo-fleet/registry/entries.jsonl", removed["foreign_preserved"])

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

    def test_first_install_skips_and_reports_nonexistent_path_entry(self) -> None:
        """A stale shell coordinate must not block a first install after the readable scan clears."""
        destination = self.base / "destination"
        missing = self.base / "stale-path-entry"
        path = os.pathsep.join((
            str(self.source / "scripts"),
            str(missing),
            str(self.git_directory),
        ))

        with patch.dict(os.environ, {"PATH": path}, clear=False):
            result = self._writer(destination, committed_tree=True).run()

        self.assertTrue((destination / "scripts" / "floati").is_file())
        self.assertEqual([str(missing)], result["installer_shadow"]["skipped_entries"])

    def test_first_install_refuses_existing_unreadable_path_with_coordinate_and_remedy(self) -> None:
        """The product caller must preserve the exact blocked entry and operator remedy."""
        destination = self.base / "destination"
        unreadable = self.base / "unreadable-path-entry"
        unreadable.mkdir()
        path = os.pathsep.join((
            str(self.source / "scripts"),
            str(unreadable),
            str(self.git_directory),
        ))
        original_lstat = Path.lstat

        def refuse_exact_entry(candidate: Path) -> os.stat_result:
            if candidate == unreadable:
                raise PermissionError("fixture denies this existing PATH entry")
            return original_lstat(candidate)

        with patch.dict(os.environ, {"PATH": path}, clear=False):
            with patch.object(Path, "lstat", autospec=True, side_effect=refuse_exact_entry):
                with self.assertRaises(ProtocolRefusal) as raised:
                    self._writer(destination, committed_tree=True).run()

        self.assertEqual("deployment_shadow_unknown", raised.exception.code)
        self.assertIn(str(unreadable), raised.exception.detail)
        self.assertEqual(
            f"Fix or drop PATH entry {unreadable}, or pass a clean PATH.",
            raised.exception.remedy,
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
        self.assertFalse((destination / "scripts" / RETIRED_PRODUCT_SHORT_NAME).exists())
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

    def test_update_skips_nonexistent_path_without_mutating_unowned_files(self) -> None:
        """A stale PATH entry does not block update or broaden its owned-file mutation set."""
        destination = self.base / "destination"
        self._writer(destination, committed_tree=True).run()
        self._advance_source_without_the_schema()
        foreign = destination / "foreign.txt"
        foreign.write_bytes(b"foreign\n")
        missing = self.base / "stale-path-entry"
        path = os.pathsep.join((
            str(self.source / "scripts"),
            str(missing),
            str(destination / "scripts"),
            str(self.git_directory),
        ))

        with patch.dict(os.environ, {"PATH": path}, clear=False):
            result = DeploymentWriter(
                self.source, destination, "update", ref="HEAD", committed_tree=True
            ).run()

        self.assertEqual([str(missing)], result["installer_shadow"]["skipped_entries"])
        self.assertEqual(b"foreign\n", foreign.read_bytes())

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
        self.assertFalse(os.path.lexists(destination / f"{LEGACY_PREFIX}-install"))


if __name__ == "__main__":
    unittest.main()
