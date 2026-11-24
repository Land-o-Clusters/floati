from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from floati.adapters import codex_live, pi
from floati.workers import WorkerAdapterFailure
from tests.temp_roots import REAL_TEMP_ROOT


class WorkerParentCreationTests(unittest.TestCase):
    """A creator winning mkdir must still pass the real parent boundary."""

    def exercise_competing_creator(self, adapter_name: str, kind: str) -> None:
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            parent = root / "floati-work"
            workspace = parent / "work-parent-race"
            fixtures = Path(__file__).parent / "fixtures"
            if adapter_name == "codex":
                adapter = codex_live.CodexAppServerAdapter((
                    str(Path(sys.executable).resolve()),
                    str(fixtures / "codex-app-server" / "reference_harness.py"),
                    "--mode", "complete",
                ))
                proof = "PROOF.txt"
            else:
                adapter = pi.PiRpcAdapter((
                    str(Path(sys.executable).resolve()),
                    str(fixtures / "pi-rpc" / "reference_harness.py"),
                    "complete",
                ))
                proof = "PI-PROOF.txt"
            item = {
                "id": "work-parent-race",
                "title": "Create fixture proof",
                "workspace": str(workspace),
            }
            real_mkdir = Path.mkdir
            real_lstat = Path.lstat
            competing_created = False

            def competing_mkdir(path: Path, *args, **kwargs) -> None:
                nonlocal competing_created
                if path == parent and not competing_created:
                    competing_created = True
                    if kind == "symlink":
                        target = root / "foreign-target"
                        real_mkdir(target, mode=0o700)
                        parent.symlink_to(target, target_is_directory=True)
                    elif kind == "file":
                        parent.write_text("foreign file", encoding="utf-8")
                    else:
                        real_mkdir(parent, mode=0o700)
                        if kind == "wrong-mode":
                            parent.chmod(0o755)
                        elif kind == "existing-child":
                            real_mkdir(workspace, mode=0o700)
                            (workspace / "sentinel").write_text("keep", encoding="utf-8")
                return real_mkdir(path, *args, **kwargs)

            def parent_metadata(path: Path, *args, **kwargs):
                metadata = real_lstat(path, *args, **kwargs)
                if path == parent and kind == "wrong-owner":
                    fields = list(metadata)
                    fields[4] = os.getuid() + 1
                    return os.stat_result(fields)
                return metadata

            with ExitStack() as patches:
                patches.enter_context(mock.patch.object(codex_live, "_WORKSPACE_PARENT", parent))
                patches.enter_context(mock.patch.object(pi, "_WORKSPACE_PARENT", parent))
                patches.enter_context(mock.patch.object(Path, "mkdir", competing_mkdir))
                patches.enter_context(mock.patch.object(Path, "lstat", parent_metadata))
                if kind in {"symlink", "file", "wrong-owner", "existing-child"}:
                    with self.assertRaises(WorkerAdapterFailure) as caught:
                        adapter.spawn(item, deadline_seconds=5)
                    self.assertEqual("workspace_invalid", caught.exception.code)
                    if kind == "existing-child":
                        self.assertEqual(["sentinel"], [path.name for path in workspace.iterdir()])
                        self.assertEqual("keep", (workspace / "sentinel").read_text(encoding="utf-8"))
                    else:
                        self.assertFalse(workspace.exists())
                    if kind == "symlink":
                        self.assertEqual([], list((root / "foreign-target").iterdir()))
                    elif kind == "file":
                        self.assertEqual("foreign file", parent.read_text(encoding="utf-8"))
                else:
                    try:
                        handle = adapter.spawn(item, deadline_seconds=5)
                    except WorkerAdapterFailure as exc:
                        self.fail(f"valid competing parent rejected: {exc.code}")
                    try:
                        bindings = adapter.drive(handle, item, deadline_seconds=5)
                    finally:
                        adapter.cancel()
                    self.assertEqual([proof], [binding["doc"] for binding in bindings])
                    self.assertTrue((workspace / proof).is_file())
                    self.assertEqual(0o700, stat.S_IMODE(parent.stat().st_mode))
                    self.assertEqual(os.getuid(), parent.stat().st_uid)
                    self.assertFalse(parent.is_symlink())
            self.assertTrue(competing_created, "the test must force the mkdir race")

    def test_competing_private_parent_reaches_normal_spawn(self) -> None:
        for adapter in ("codex", "pi"):
            with self.subTest(adapter=adapter):
                self.exercise_competing_creator(adapter, "private")

    def test_competing_symlink_parent_is_refused_without_child_writes(self) -> None:
        for adapter in ("codex", "pi"):
            with self.subTest(adapter=adapter):
                self.exercise_competing_creator(adapter, "symlink")

    def test_competing_file_parent_is_refused_without_child_writes(self) -> None:
        for adapter in ("codex", "pi"):
            with self.subTest(adapter=adapter):
                self.exercise_competing_creator(adapter, "file")

    def test_competing_foreign_owned_parent_is_refused_without_child_writes(self) -> None:
        for adapter in ("codex", "pi"):
            with self.subTest(adapter=adapter):
                self.exercise_competing_creator(adapter, "wrong-owner")

    def test_competing_owned_parent_permissions_are_still_normalized(self) -> None:
        for adapter in ("codex", "pi"):
            with self.subTest(adapter=adapter):
                self.exercise_competing_creator(adapter, "wrong-mode")

    def test_competing_child_workspace_is_still_refused_without_overwrite(self) -> None:
        for adapter in ("codex", "pi"):
            with self.subTest(adapter=adapter):
                self.exercise_competing_creator(adapter, "existing-child")


if __name__ == "__main__":
    unittest.main()
