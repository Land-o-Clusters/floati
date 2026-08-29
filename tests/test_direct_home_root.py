from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot


class DirectHomeRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_create_uses_the_exact_home_and_derives_tenant(self) -> None:
        home = self.base / "puddle-fleet"

        root = FloatiRoot.open_direct_home(home, create=True)

        self.assertEqual(home.resolve(), root.path)
        self.assertEqual(home.resolve(), root.tenant_home)
        self.assertEqual("puddle-fleet", root.tenant_id)
        self.assertTrue(home.is_dir())

    def test_missing_or_relative_root_refuses(self) -> None:
        with self.assertRaises(ProtocolRefusal) as missing:
            FloatiRoot.open_direct_home(None)
        self.assertEqual("root_required", missing.exception.code)
        with self.assertRaises(ProtocolRefusal) as relative:
            FloatiRoot.open_direct_home(Path("relative"))
        self.assertEqual("root_not_absolute", relative.exception.code)

    def test_invalid_home_basename_refuses(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            FloatiRoot.open_direct_home(self.base / "Invalid")
        self.assertEqual("direct_home_tenant_invalid", caught.exception.code)

    def test_missing_home_without_create_refuses(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            FloatiRoot.open_direct_home(self.base / "puddle-fleet")
        self.assertEqual("direct_home_missing", caught.exception.code)

    def test_create_refuses_existing_file_without_mutation(self) -> None:
        home = self.base / "puddle-fleet"
        original = b"not a directory\n"
        home.write_bytes(original)

        with self.assertRaises(ProtocolRefusal) as caught:
            FloatiRoot.open_direct_home(home, create=True)

        self.assertEqual("direct_home_not_directory", caught.exception.code)
        self.assertEqual(original, home.read_bytes())

    def test_create_translates_expected_filesystem_failure(self) -> None:
        blocked_parent = self.base / "blocked"
        original = b"not a parent directory\n"
        blocked_parent.write_bytes(original)

        with self.assertRaises(ProtocolRefusal) as caught:
            FloatiRoot.open_direct_home(blocked_parent / "puddle-fleet", create=True)

        self.assertEqual("root_unavailable", caught.exception.code)
        self.assertEqual(original, blocked_parent.read_bytes())

    def test_namespace_layout_and_broken_tenants_symlink_refuse(self) -> None:
        namespace_root = self.base / "puddle-fleet"
        namespace_root.mkdir()
        (namespace_root / "tenants").mkdir()
        with self.assertRaisesRegex(ProtocolRefusal, "namespace_root_layout_present"):
            FloatiRoot.open_direct_home(namespace_root)

        direct_home = self.base / "puddle-fleet-link"
        direct_home.mkdir()
        (direct_home / "tenants").symlink_to(self.base / "missing-tenants")
        with self.assertRaisesRegex(ProtocolRefusal, "namespace_root_layout_present"):
            FloatiRoot.open_direct_home(direct_home)


if __name__ == "__main__":
    unittest.main()
