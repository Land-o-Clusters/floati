from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from floati.workspace_layout import (
    inspect_workspace_layout,
    register_node,
    retire_node,
)


class NodeWorkspaceLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)

    def test_register_option_creates_the_node_folder_only_under_the_fleet_root(self) -> None:
        """Catches workspace creation beside the fleet root or in the user's home."""
        result = register_node(
            self.root, "alpha", "Codex", create_workspace=True
        )

        expected = self.root_path / "nodes" / "alpha"
        self.assertTrue(expected.is_dir())
        self.assertEqual(expected.resolve(), Path(result["workspace"]["path"]))
        self.assertEqual("created", result["workspace"]["state"])
        self.assertEqual("alpha", result["registry"]["node_id"])
        self.assertEqual({"fleet"}, {path.name for path in Path(self.temporary.name).iterdir()})

    def test_retire_reports_and_never_deletes_the_node_folder(self) -> None:
        """Catches retirement recursively deleting durable working files."""
        register_node(self.root, "alpha", "Codex", create_workspace=True)
        durable = self.root_path / "nodes" / "alpha" / "worktree-note.txt"
        durable.write_text("retain me\n", encoding="utf-8")

        result = retire_node(self.root, "alpha")

        self.assertEqual("retained", result["workspace"]["state"])
        self.assertEqual("retain me\n", durable.read_text(encoding="utf-8"))
        self.assertEqual(
            "Node workspace retained; retirement never deletes working folders.",
            result["workspace"]["notice"],
        )

    def test_invalid_node_refuses_before_creating_the_nodes_directory(self) -> None:
        """Catches lexical refusal happening after workspace mutation."""
        with self.assertRaises(ProtocolRefusal) as raised:
            register_node(self.root, "../escape", "Codex", create_workspace=True)

        self.assertEqual("node_invalid", raised.exception.code)
        self.assertFalse((self.root_path / "nodes").exists())

    def test_existing_non_directory_workspace_refuses_before_registry_append(self) -> None:
        """Catches registration succeeding against an unusable workspace coordinate."""
        collision = self.root_path / "nodes" / "alpha"
        collision.parent.mkdir()
        collision.write_text("foreign file\n", encoding="utf-8")

        with self.assertRaises(ProtocolRefusal) as raised:
            register_node(self.root, "alpha", "Codex", create_workspace=True)

        self.assertEqual("node_workspace_invalid", raised.exception.code)
        self.assertEqual((), self._active_nodes())
        self.assertEqual("foreign file\n", collision.read_text(encoding="utf-8"))

    def test_doctor_awareness_is_deterministic_and_read_only(self) -> None:
        """Catches workspace inspection creating missing folders or hiding orphans."""
        register_node(self.root, "alpha", "Codex", create_workspace=False)
        orphan = self.root_path / "nodes" / "orphan"
        orphan.mkdir(parents=True)
        before = sorted(path.relative_to(self.root_path) for path in self.root_path.rglob("*"))

        findings = inspect_workspace_layout(self.root)

        self.assertEqual(
            [
                {
                    "code": "node_workspace_missing",
                    "node_id": "alpha",
                    "path": str((self.root_path / "nodes" / "alpha").resolve()),
                    "severity": "warning",
                },
                {
                    "code": "node_workspace_orphan",
                    "node_id": "orphan",
                    "path": str(orphan.resolve()),
                    "severity": "notice",
                },
            ],
            findings,
        )
        self.assertEqual(
            before,
            sorted(path.relative_to(self.root_path) for path in self.root_path.rglob("*")),
        )

    def _active_nodes(self) -> tuple[str, ...]:
        from floati.registry import Registry

        return Registry(self.root).active_node_ids()


if __name__ == "__main__":
    unittest.main()
