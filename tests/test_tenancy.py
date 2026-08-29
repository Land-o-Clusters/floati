from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.jsonl import read_records
from floati.registry import Registry
from floati.root import FloatiRoot


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class TenancyBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_namespace_open_refuses_symlinked_invoked_root(self) -> None:
        namespace = self.base / "namespace"
        namespace.mkdir()
        link = self.base / "namespace-link"
        link.symlink_to(namespace, target_is_directory=True)

        with self.assertRaises(ProtocolRefusal) as caught:
            FloatiRoot.open(link, "alpha")

        self.assertEqual("root_symlinked_entry", caught.exception.code)

    def test_direct_home_refuses_symlinked_invoked_home(self) -> None:
        home = self.base / "alpha"
        home.mkdir()
        link = self.base / "alpha-link"
        link.symlink_to(home, target_is_directory=True)

        with self.assertRaises(ProtocolRefusal) as caught:
            FloatiRoot.open_direct_home(link)

        self.assertEqual("direct_home_symlinked_entry", caught.exception.code)

    def test_namespace_open_refuses_symlinked_tenant_home(self) -> None:
        namespace = self.base / "namespace"
        namespace.mkdir()
        tenants = namespace / "tenants"
        tenants.mkdir()
        target = self.base / "foreign"
        target.mkdir()
        (tenants / "alpha").symlink_to(target, target_is_directory=True)

        with self.assertRaises(ProtocolRefusal) as caught:
            FloatiRoot.open(namespace, "alpha")

        self.assertEqual("tenant_symlinked_entry", caught.exception.code)

    def test_two_tenants_cannot_read_or_write_each_other_by_default(self) -> None:
        namespace = self.base / "namespace"
        alpha = FloatiRoot.open(namespace, "alpha")
        bravo = FloatiRoot.open(namespace, "bravo")
        Registry(alpha).register("alpha-node", "worker")
        Registry(bravo).register("bravo-node", "worker")

        alpha_records = read_records(
            alpha, "registry/entries.jsonl", allowed_kinds={"registry_entry"}
        )
        bravo_records = read_records(
            bravo, "registry/entries.jsonl", allowed_kinds={"registry_entry"}
        )
        self.assertEqual(["alpha-node"], [row["node_id"] for row in alpha_records])
        self.assertEqual(["bravo-node"], [row["node_id"] for row in bravo_records])

        for root, foreign in ((alpha, bravo), (bravo, alpha)):
            before = sorted(
                path.relative_to(foreign.tenant_home)
                for path in foreign.tenant_home.rglob("*")
            )
            with self.assertRaises(ProtocolRefusal) as escaped:
                root.resolve_relative(
                    Path("..") / foreign.tenant_id / "registry" / "entries.jsonl"
                )
            self.assertEqual("path_not_contained", escaped.exception.code)
            after = sorted(
                path.relative_to(foreign.tenant_home)
                for path in foreign.tenant_home.rglob("*")
            )
            self.assertEqual(before, after)

        alpha_link = alpha.tenant_home / "bravo-link"
        alpha_link.symlink_to(bravo.tenant_home, target_is_directory=True)
        before = sorted(
            path.relative_to(bravo.tenant_home) for path in bravo.tenant_home.rglob("*")
        )
        with self.assertRaises(ProtocolRefusal) as escaped:
            alpha.resolve_relative("bravo-link/registry/entries.jsonl")
        self.assertEqual("path_not_contained", escaped.exception.code)
        after = sorted(
            path.relative_to(bravo.tenant_home) for path in bravo.tenant_home.rglob("*")
        )
        self.assertEqual(before, after)

    def test_launcher_refuses_a_symlinked_invoked_entry_point(self) -> None:
        link = self.base / "slip-link"
        link.symlink_to(REPOSITORY_ROOT / "scripts" / "floati")

        result = subprocess.run(
            [str(link), "--help"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(20, result.returncode)
        self.assertIn("symlink", result.stderr.lower())
        self.assertNotIn("NAME", result.stdout)


if __name__ == "__main__":
    unittest.main()
