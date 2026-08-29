from __future__ import annotations

import builtins
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal

try:
    from floati.storage_identity import refuse_legacy_workspace_artifacts
except (ImportError, ModuleNotFoundError):
    refuse_legacy_workspace_artifacts = None


class StorageIdentityTests(unittest.TestCase):
    @staticmethod
    def _identity(path: Path) -> tuple[int, int]:
        metadata = path.lstat()
        return metadata.st_dev, metadata.st_ino

    def _require_preflight(self):
        self.assertIsNotNone(
            refuse_legacy_workspace_artifacts,
            "floati.storage_identity must provide the legacy preflight",
        )
        return refuse_legacy_workspace_artifacts

    def _refuse_without_opening(
        self, workspace: Path, *forbidden_paths: Path,
    ) -> ProtocolRefusal:
        preflight = self._require_preflight()
        forbidden = set(forbidden_paths)
        opened: list[Path] = []

        def candidate(value: object) -> Path | None:
            if not isinstance(value, (str, bytes, os.PathLike)):
                return None
            try:
                return Path(os.fsdecode(value))
            except (TypeError, ValueError):
                return None

        def forbid(original):
            def guarded(path, *args, **kwargs):
                selected = candidate(path)
                if selected in forbidden:
                    opened.append(selected)
                    raise AssertionError(
                        "legacy storage must be refused without being opened or followed"
                    )
                return original(path, *args, **kwargs)

            return guarded

        with (
            mock.patch("builtins.open", side_effect=forbid(builtins.open)),
            mock.patch("io.open", side_effect=forbid(io.open)),
            mock.patch("os.open", side_effect=forbid(os.open)),
            mock.patch("os.stat", side_effect=forbid(os.stat)),
            self.assertRaises(ProtocolRefusal) as raised,
        ):
            preflight(workspace)

        self.assertEqual([], opened)
        return raised.exception

    def _assert_regular_legacy_preserved(
        self,
        path: Path,
        *,
        identity: tuple[int, int],
        contents: bytes,
    ) -> None:
        self.assertTrue(os.path.lexists(path))
        self.assertEqual(identity, self._identity(path))
        self.assertEqual(contents, path.read_bytes())

    def test_one_legacy_artifact_refuses_with_fable_copy_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            legacy = workspace / ".slipway"
            contents = b"legacy Floati migration sentinel\n"
            legacy.write_bytes(contents)
            identity = self._identity(legacy)

            raised = self._refuse_without_opening(workspace, legacy)

            self.assertEqual("legacy_workspace_artifacts", raised.code)
            self.assertEqual(
                "workspace refused: legacy artifact '.slipway' predates the Floati rename; nothing was read, migrated, or deleted; start a fresh root, or archive the legacy artifacts yourself and run again",
                raised.detail,
            )
            self._assert_regular_legacy_preserved(
                legacy, identity=identity, contents=contents,
            )
            self.assertFalse(os.path.lexists(workspace / ".floati"))
            self.assertEqual([".slipway"], sorted(entry.name for entry in workspace.iterdir()))

    def test_multiple_legacy_artifacts_sort_count_and_preserve_fable_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            first = workspace / ".slipway"
            other = workspace / ".slipway-snapshots"
            first_contents = b"first legacy sentinel\n"
            other_contents = b"second legacy sentinel\n"
            first.write_bytes(first_contents)
            other.write_bytes(other_contents)
            first_identity = self._identity(first)
            other_identity = self._identity(other)

            raised = self._refuse_without_opening(workspace, first, other)

            self.assertEqual("legacy_workspace_artifacts", raised.code)
            self.assertEqual(
                "workspace refused: legacy artifact '.slipway' and 1 more predate the Floati rename; nothing was read, migrated, or deleted; start a fresh root, or archive the legacy artifacts yourself and run again",
                raised.detail,
            )
            self._assert_regular_legacy_preserved(
                first, identity=first_identity, contents=first_contents,
            )
            self._assert_regular_legacy_preserved(
                other, identity=other_identity, contents=other_contents,
            )
            self.assertFalse(os.path.lexists(workspace / ".floati"))
            self.assertEqual(
                [".slipway", ".slipway-snapshots"],
                sorted(entry.name for entry in workspace.iterdir()),
            )

    def test_legacy_symlink_refuses_without_following_or_mutating_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            target = workspace / "legacy-target.txt"
            target_contents = b"symlink target sentinel\n"
            target.write_bytes(target_contents)
            legacy = workspace / ".slipway-link"
            legacy.symlink_to(target.name)
            legacy_identity = self._identity(legacy)
            target_identity = self._identity(target)

            raised = self._refuse_without_opening(workspace, legacy, target)

            self.assertEqual("legacy_workspace_artifacts", raised.code)
            self.assertEqual(
                "workspace refused: legacy artifact '.slipway-link' predates the Floati rename; nothing was read, migrated, or deleted; start a fresh root, or archive the legacy artifacts yourself and run again",
                raised.detail,
            )
            self.assertTrue(os.path.lexists(legacy))
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(legacy_identity, self._identity(legacy))
            self.assertEqual(target.name, os.readlink(legacy))
            self.assertEqual(target_identity, self._identity(target))
            self.assertEqual(target_contents, target.read_bytes())
            self.assertFalse(os.path.lexists(workspace / ".floati"))


if __name__ == "__main__":
    unittest.main()
