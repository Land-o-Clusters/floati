from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
COORDINATOR_AUTHORITY = (
    "dispatch_bounded_work",
    "gate_results_before_merge",
    "decide_non_owner_tier_questions",
)
OWNER_TIER = ("publishing", "credentials", "key_custody")


def tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    return {
        path.relative_to(root).as_posix(): (
            "symlink"
            if path.is_symlink()
            else "directory"
            if path.is_dir()
            else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.rglob("*")
    }


class WorkspaceSeatDeclarationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def invoke(
        self, *arguments: str, cwd: Path = REPOSITORY_ROOT
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        environment = dict(os.environ)
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (str(REPOSITORY_ROOT), existing_pythonpath)
            if value
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-m", "floati", *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        stream = completed.stdout if completed.returncode == 0 else completed.stderr
        other_stream = completed.stderr if completed.returncode == 0 else completed.stdout
        self.assertEqual("", other_stream)
        self.assertTrue(stream.endswith("\n"))
        self.assertEqual(1, len(stream.splitlines()))
        artifact = json.loads(stream)
        self.assertEqual(
            json.dumps(
                artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            stream,
        )
        return completed, artifact

    @staticmethod
    def governance_arguments(topology: str = "star") -> tuple[str, ...]:
        return (
            "--topology",
            topology,
            "--coordinator",
            "architect-a",
            "--coordinator-authority",
            COORDINATOR_AUTHORITY[0],
            "--coordinator-authority",
            COORDINATOR_AUTHORITY[1],
            "--coordinator-authority",
            COORDINATOR_AUTHORITY[2],
            "--owner-tier",
            OWNER_TIER[0],
            "--owner-tier",
            OWNER_TIER[1],
            "--owner-tier",
            OWNER_TIER[2],
        )

    def initialize_root(self, root: Path, *nodes: str) -> None:
        initialized, _artifact = self.invoke("init", "--root", str(root))
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        for node in nodes:
            added, _artifact = self.invoke(
                "node",
                "add",
                "--root",
                str(root),
                "--node",
                node,
                "--harness",
                "Codex",
                "--lifetime",
                "permanent",
            )
            self.assertEqual(0, added.returncode, added.stderr)

    def send_pending_mail(
        self, root: Path, recipient: str
    ) -> dict[str, object]:
        sent, artifact = self.invoke(
            "send",
            "--root",
            str(root),
            "--from",
            "sender-a",
            "--to",
            recipient,
            "--repo",
            "floati",
            "--sha",
            SHA,
            "--doc",
            "docs/evidence/task-2-red.md",
            "--note",
            "workspace identity guard fixture",
        )
        self.assertEqual(0, sent.returncode, sent.stderr)
        return artifact

    def write_seat_fixture(self, workspace: Path, root: Path, node: str) -> None:
        (workspace / "SEAT.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "tenant_id": root.name,
                    "root": str(root.resolve()),
                    "node_id": node,
                    "topology": "star",
                    "coordinator": "architect-a",
                    "coordinator_authority": list(COORDINATOR_AUTHORITY),
                    "owner_tier": list(OWNER_TIER),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def test_governance_init_and_node_add_write_versioned_seat_marker(self) -> None:
        """Catches onboarding that loses fleet governance or leaves a seat undeclared."""
        root = self.base / "governed-fleet"
        initialized, init_artifact = self.invoke(
            "init", "--root", str(root), *self.governance_arguments()
        )

        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.assertEqual("ok", init_artifact["status"])
        added, add_artifact = self.invoke(
            "node",
            "add",
            "--root",
            str(root),
            "--node",
            public_ids.builder("a"),
            "--harness",
            "Codex",
            "--lifetime",
            "permanent",
        )

        self.assertEqual(0, added.returncode, added.stderr)
        self.assertEqual("ok", add_artifact["status"])
        workspace = root / "nodes" / public_ids.builder("a")
        marker = workspace / "SEAT.json"
        self.assertTrue(workspace.is_dir())
        self.assertTrue(marker.is_file())
        declaration = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(1, declaration["schema_version"])
        self.assertEqual(root.name, declaration["tenant_id"])
        self.assertEqual(str(root.resolve()), declaration["root"])
        self.assertEqual(public_ids.builder("a"), declaration["node_id"])
        self.assertEqual("star", declaration["topology"])
        self.assertEqual("architect-a", declaration["coordinator"])
        self.assertEqual(
            list(COORDINATOR_AUTHORITY), declaration["coordinator_authority"]
        )
        self.assertEqual(list(OWNER_TIER), declaration["owner_tier"])

    def test_mesh_governance_init_and_node_add_write_mesh_seat_marker(self) -> None:
        """Catches onboarding that accepts star but rejects or rewrites ruled mesh."""
        root = self.base / "mesh-fleet"
        initialized, init_artifact = self.invoke(
            "init", "--root", str(root), *self.governance_arguments("mesh")
        )

        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.assertEqual("ok", init_artifact["status"])
        added, add_artifact = self.invoke(
            "node",
            "add",
            "--root",
            str(root),
            "--node",
            public_ids.builder("mesh"),
            "--harness",
            "Codex",
            "--lifetime",
            "permanent",
        )

        self.assertEqual(0, added.returncode, added.stderr)
        self.assertEqual("ok", add_artifact["status"])
        marker = root / "nodes" / public_ids.builder("mesh") / "SEAT.json"
        self.assertTrue(marker.is_file())
        declaration = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual("mesh", declaration["topology"])
        self.assertEqual(str(root.resolve()), declaration["root"])
        self.assertEqual(public_ids.builder("mesh"), declaration["node_id"])

    def test_governance_init_options_are_all_or_nothing(self) -> None:
        """Catches a partial governance declaration creating an ambiguous fleet root."""
        cases = {
            "topology": (
                "--coordinator",
                "architect-a",
                "--coordinator-authority",
                COORDINATOR_AUTHORITY[0],
                "--owner-tier",
                OWNER_TIER[0],
            ),
            "coordinator": (
                "--topology",
                "star",
                "--coordinator-authority",
                COORDINATOR_AUTHORITY[0],
                "--owner-tier",
                OWNER_TIER[0],
            ),
            "coordinator-authority": (
                "--topology",
                "star",
                "--coordinator",
                "architect-a",
                "--owner-tier",
                OWNER_TIER[0],
            ),
            "owner-tier": (
                "--topology",
                "star",
                "--coordinator",
                "architect-a",
                "--coordinator-authority",
                COORDINATOR_AUTHORITY[0],
            ),
        }
        for missing, arguments in cases.items():
            with self.subTest(missing=missing):
                root = self.base / f"partial-{missing}"
                completed, artifact = self.invoke(
                    "init", "--root", str(root), *arguments
                )

                self.assertEqual(20, completed.returncode)
                self.assertEqual("refused", artifact["status"])
                self.assertEqual("arguments_invalid", artifact["evidence"]["code"])
                self.assertFalse(root.exists())

    def test_governance_init_rejects_unruled_values_before_creating_root(self) -> None:
        """Catches topology, authority, or owner-tier values escaping validation."""
        cases = {
            "topology": ("--topology", "ring"),
            "coordinator-authority": (
                "--coordinator-authority",
                "publish_without_owner",
            ),
            "owner-tier": ("--owner-tier", "merge_gate"),
        }
        full = list(self.governance_arguments())
        for field, replacement in cases.items():
            with self.subTest(field=field):
                root = self.base / f"invalid-{field}"
                arguments = list(full)
                option = replacement[0]
                value_index = arguments.index(option) + 1
                arguments[value_index] = replacement[1]
                completed, artifact = self.invoke(
                    "init", "--root", str(root), *arguments
                )

                self.assertEqual(20, completed.returncode)
                self.assertEqual("refused", artifact["status"])
                self.assertEqual("arguments_invalid", artifact["evidence"]["code"])
                self.assertFalse(root.exists())

    def test_workspace_seat_schema_is_closed_and_pins_ruled_fields(self) -> None:
        """Catches a missing, open, incomplete, or topology-loose marker schema."""
        schema_path = (
            REPOSITORY_ROOT
            / "schemas"
            / "v1"
            / "workspace-seat-declaration.schema.json"
        )
        self.assertTrue(
            schema_path.is_file(),
            f"required workspace seat declaration schema is absent: {schema_path}",
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertIsInstance(schema, dict)
        self.assertIs(schema.get("additionalProperties"), False)
        expected_fields = {
            "schema_version",
            "tenant_id",
            "root",
            "node_id",
            "topology",
            "coordinator",
            "coordinator_authority",
            "owner_tier",
        }
        required = schema.get("required")
        properties = schema.get("properties")
        self.assertIsInstance(required, list)
        self.assertEqual(expected_fields, set(required))
        self.assertIsInstance(properties, dict)
        self.assertEqual(expected_fields, set(properties))
        topology = properties.get("topology")
        self.assertIsInstance(topology, dict)
        vocabulary = topology.get("enum")
        self.assertIsInstance(vocabulary, list)
        self.assertCountEqual(("star", "mesh"), vocabulary)

    def test_declared_root_mismatch_refuses_before_drain_without_side_effect(self) -> None:
        """Catches a declared seat draining mail from a remembered foreign root."""
        declared_root = self.base / "declared-fleet"
        foreign_root = self.base / "foreign-fleet"
        self.initialize_root(declared_root, public_ids.builder("a"))
        self.initialize_root(foreign_root, "sender-a", public_ids.builder("a"))
        self.send_pending_mail(foreign_root, public_ids.builder("a"))
        workspace = declared_root / "nodes" / public_ids.builder("a")
        self.write_seat_fixture(workspace, declared_root, public_ids.builder("a"))
        before = tree_snapshot(foreign_root)

        completed, artifact = self.invoke(
            "inbox",
            "--root",
            str(foreign_root),
            "--as",
            public_ids.builder("a"),
            "--peek",
            cwd=workspace,
        )
        after = tree_snapshot(foreign_root)

        with self.subTest(contract="exit"):
            self.assertEqual(20, completed.returncode)
        with self.subTest(contract="typed-refusal"):
            self.assertEqual("refused", artifact["status"])
            self.assertEqual(
                "workspace_identity_mismatch", artifact["evidence"]["code"]
            )
        with self.subTest(contract="no-side-effect"):
            self.assertEqual(before, after)

    def test_declared_node_mismatch_refuses_before_drain_without_side_effect(self) -> None:
        """Catches a declared seat draining as another node at the declared root."""
        root = self.base / "node-mismatch-fleet"
        self.initialize_root(root, "sender-a", public_ids.builder("a"), "foreign-node")
        self.send_pending_mail(root, "foreign-node")
        workspace = root / "nodes" / public_ids.builder("a")
        self.write_seat_fixture(workspace, root, public_ids.builder("a"))
        before = tree_snapshot(root)

        completed, artifact = self.invoke(
            "inbox",
            "--root",
            str(root),
            "--as",
            "foreign-node",
            "--peek",
            cwd=workspace,
        )
        after = tree_snapshot(root)

        with self.subTest(contract="exit"):
            self.assertEqual(20, completed.returncode)
        with self.subTest(contract="typed-refusal"):
            self.assertEqual("refused", artifact["status"])
            self.assertEqual(
                "workspace_identity_mismatch", artifact["evidence"]["code"]
            )
        with self.subTest(contract="no-side-effect"):
            self.assertEqual(before, after)

    def test_matching_declaration_allows_inbox_and_names_identity(self) -> None:
        """Catches a present-marker guard that refuses even the exact coordinate."""
        root = self.base / "matching-fleet"
        self.initialize_root(root, "sender-a", public_ids.builder("a"))
        sent = self.send_pending_mail(root, public_ids.builder("a"))
        workspace = root / "nodes" / public_ids.builder("a")
        self.write_seat_fixture(workspace, root, public_ids.builder("a"))

        completed, artifact = self.invoke(
            "inbox", "--root", str(root), "--as", public_ids.builder("a"), "--peek", cwd=workspace
        )

        self.assertEqual(0, completed.returncode)
        self.assertEqual("ok", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual([sent["evidence"]["message"]], evidence["messages"])
        self.assertEqual(str(root.resolve()), evidence["scope"]["root"])
        self.assertEqual(root.name, evidence["scope"]["tenant"])
        self.assertEqual("explicit", evidence["scope"]["root_source"])
        self.assertEqual("declared", evidence.get("workspace_identity"))
        self.assertEqual(public_ids.builder("a"), evidence.get("used_node"))
        self.assertEqual(str(root.resolve()), evidence.get("used_root"))

    def test_absent_declaration_names_the_used_node_and_root(self) -> None:
        """Catches compatibility mode silently inheriting an undeclared identity."""
        root = self.base / "legacy-fleet"
        self.initialize_root(root, public_ids.builder("a"))
        workspace = root / "nodes" / public_ids.builder("a")
        self.assertFalse((workspace / "SEAT.json").exists())

        completed, artifact = self.invoke(
            "inbox", "--root", str(root), "--as", public_ids.builder("a"), "--peek", cwd=workspace
        )

        self.assertEqual(31, completed.returncode)
        self.assertEqual("intentional_silence", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("absent", evidence.get("workspace_identity"))
        self.assertEqual(public_ids.builder("a"), evidence.get("used_node"))
        self.assertEqual(str(root.resolve()), evidence.get("used_root"))


if __name__ == "__main__":
    unittest.main()
