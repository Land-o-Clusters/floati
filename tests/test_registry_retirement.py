from __future__ import annotations

from tests.test_cli import LAUNCHER

from floati import fixture_ids as public_ids

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def root_entries(root: FloatiRoot) -> dict[Path, tuple[str, bytes]]:
    return {
        path.relative_to(root.tenant_home): (
            "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.tenant_home.rglob("*")
    }


class RegistryRetirementWriterTests(unittest.TestCase):
    """The public retirement writer TD-5301's projection floor already reads."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.registry = Registry(self.root)

    def test_retire_appends_a_retired_row_without_rewriting_the_active_row(self) -> None:
        """Catches a retirement that mutates history instead of appending to it."""
        active = self.registry.register(public_ids.builder('a'), "worker")
        before = self.registry.path.read_bytes()

        retired = self.registry.retire(public_ids.builder('a'))

        self.assertEqual("retired", retired["state"])
        self.assertEqual(public_ids.builder('a'), retired["node_id"])
        self.assertEqual("registry_entry", retired["kind"])
        self.assertNotEqual(active["id"], retired["id"])
        self.assertTrue(str(retired["id"]).startswith("registry-"))
        after = self.registry.path.read_bytes()
        self.assertTrue(after.startswith(before), "retirement must append, never rewrite")
        self.assertEqual(2, len(after.splitlines()))

    def test_retire_preserves_the_registered_role_rather_than_inventing_one(self) -> None:
        """Catches a retirement row that guesses a role the node never held."""
        self.registry.register(public_ids.builder('a'), "Codex")

        self.assertEqual("Codex", self.registry.retire(public_ids.builder('a'))["role"])

    def test_retired_node_leaves_the_active_projection_and_fails_lookup(self) -> None:
        """The TD-5301 projection floor must see the public writer's row."""
        for node in (public_ids.builder('a'), public_ids.builder('b')):
            self.registry.register(node, "worker")

        self.registry.retire(public_ids.builder('a'))

        self.assertEqual((public_ids.builder('b'),), self.registry.active_node_ids())
        with self.assertRaises(ProtocolRefusal) as caught:
            self.registry.require_active(public_ids.builder('a'))
        self.assertEqual("unknown_node", caught.exception.code)

    def test_retiring_an_unregistered_node_refuses_without_mutation(self) -> None:
        """Catches a retirement that tombstones a node that never registered."""
        self.registry.register(public_ids.builder('a'), "worker")
        before = root_entries(self.root)

        with self.assertRaises(ProtocolRefusal) as caught:
            self.registry.retire("stranger")

        self.assertEqual("unknown_node", caught.exception.code)
        self.assertEqual(before, root_entries(self.root))

    def test_retiring_an_already_retired_node_refuses_without_mutation(self) -> None:
        """Catches a second tombstone stacking on a node already retired."""
        self.registry.register(public_ids.builder('a'), "worker")
        self.registry.retire(public_ids.builder('a'))
        before = root_entries(self.root)

        with self.assertRaises(ProtocolRefusal) as caught:
            self.registry.retire(public_ids.builder('a'))

        self.assertEqual("registry_already_retired", caught.exception.code)
        self.assertEqual(before, root_entries(self.root))

    def test_unsafe_node_identifier_refuses_before_the_ledger_exists(self) -> None:
        """Retirement shares register's lexical preflight; no lock is created first."""
        for node in ("bad\x1bnode", "bad‮node", ""):
            with self.subTest(node=repr(node)):
                self.assertFalse(self.registry.path.parent.exists())
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.registry.retire(node)
                self.assertEqual("node_invalid", caught.exception.code)
                self.assertFalse(self.registry.path.parent.exists())

    def test_retirement_is_not_reachable_on_an_absent_ledger(self) -> None:
        """Catches a retirement that creates a registry out of nothing."""
        self.assertFalse(self.registry.path.exists())

        with self.assertRaises(ProtocolRefusal) as caught:
            self.registry.retire(public_ids.builder('a'))

        self.assertEqual("unknown_node", caught.exception.code)


class RegistryRetirementCliTests(unittest.TestCase):
    """The `floati retire` surface. Self-retirement only; no actor override exists."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "demo-fleet"
        result = self.run_cli("init", "--root", str(self.home))
        self.assertEqual(0, result.returncode, result.stderr)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *args],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def artifact(self, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        return json.loads(result.stdout)

    def register(self, node: str) -> None:
        result = self.run_cli(
            "register", "--root", str(self.home), node, "--harness", "Codex"
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_retire_emits_one_ok_artifact_carrying_the_retired_row(self) -> None:
        self.register(public_ids.builder('a'))

        result = self.run_cli("retire", "--root", str(self.home), public_ids.builder('a'))

        self.assertEqual(0, result.returncode, result.stderr)
        artifact = self.artifact(result)
        self.assertEqual("retire", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(public_ids.builder('a'), artifact["evidence"]["node_id"])
        self.assertEqual("retired", artifact["evidence"]["state"])

    def test_retire_refuses_an_unregistered_node_with_the_refusal_status(self) -> None:
        self.register(public_ids.builder('a'))

        result = self.run_cli("retire", "--root", str(self.home), "stranger")

        self.assertEqual(20, result.returncode)
        artifact = self.artifact(result)
        self.assertEqual("retire", artifact["command"])
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("unknown_node", artifact["evidence"]["code"])

    def test_retire_refuses_a_node_already_retired(self) -> None:
        self.register(public_ids.builder('a'))
        self.assertEqual(
            0, self.run_cli("retire", "--root", str(self.home), public_ids.builder('a')).returncode
        )

        result = self.run_cli("retire", "--root", str(self.home), public_ids.builder('a'))

        self.assertEqual(20, result.returncode)
        self.assertEqual(
            "registry_already_retired", self.artifact(result)["evidence"]["code"]
        )

    def test_retire_offers_no_actor_override_pending_the_controller_ruling(self) -> None:
        """Self-retirement only: retiring another node is not yet ruled lawful."""
        self.register(public_ids.builder('a'))
        self.register(public_ids.builder('b'))

        for override in ("--as", "--on-behalf-of", "--actor"):
            with self.subTest(override=override):
                result = self.run_cli(
                    "retire", "--root", str(self.home), public_ids.builder('b'), override, public_ids.builder('a')
                )
                self.assertNotEqual(0, result.returncode)

    def test_retire_has_a_static_man_page(self) -> None:
        result = self.run_cli("retire", "--help")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        for section in ("NAME\n", "SYNOPSIS\n", "DESCRIPTION\n", "EXIT STATUS\n", "EXAMPLES\n"):
            self.assertIn(section, result.stdout)


class RegistryRetirementCopyOwnershipTests(unittest.TestCase):
    'Every visible retirement string stays an unfilled reviewer placeholder.'

    def test_retirement_copy_is_registered_as_placeholders_for_the_architect(self) -> None:
        from floati.copy import copy_ledger_markdown

        ledger = copy_ledger_markdown()
        for key in (
            "help.retire",
            "registry.retire.unknown_node",
            "registry.retire.already_retired",
        ):
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", ledger)

    def test_retirement_prose_is_the_architects_strings(self) -> None:
        """Catches the architect's strings regressing to placeholder keys."""
        from floati import copy as copy_module
        from floati import helptext  # noqa: F401 - registers the static help bank

        for key in (
            "help.retire",
            "registry.retire.unknown_node",
            "registry.retire.already_retired",
        ):
            with self.subTest(key=key):
                value = copy_module._ENTRIES[key][0]
                self.assertNotIn("[[", value, "retirement copy must stay written, never a placeholder")


if __name__ == "__main__":
    unittest.main()
