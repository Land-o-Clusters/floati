"""A second installed-host client sees pristine bytes at the same path."""

import os
import shutil
import unittest

from tests.installed_host_seed import restore


class InstalledHostSeedTests(unittest.TestCase):
    def test_restore_removes_predecessor_writes_at_the_original_path(self):
        key = object()

        def build(base):
            registry = base / "registry"
            registry.write_bytes(b"pristine\n")
            registry.chmod(0o600)
            (base / "registered").symlink_to(registry)
            return registry

        base, registry = restore(key, build)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        registry.write_bytes(b"changed by predecessor\n")
        registry.chmod(0o644)
        (base / "registered").unlink()
        (base / "registered").symlink_to(base)
        (base / "directory-alias").symlink_to(base, target_is_directory=True)
        os.mkfifo(base / "receipt")

        def unexpected_build(_base):
            self.fail("the same key rebuilt the installation")

        restored, restored_registry = restore(key, unexpected_build)
        self.assertEqual((base, registry), (restored, restored_registry))
        self.assertEqual(b"pristine\n", registry.read_bytes())
        self.assertEqual(0o600, registry.stat().st_mode & 0o777)
        self.assertEqual(str(registry), os.readlink(base / "registered"))
        self.assertEqual({"registry", "registered"}, {path.name for path in base.iterdir()})

        shutil.rmtree(base)
        self.assertEqual((base, registry), restore(key, unexpected_build))
        self.assertEqual(b"pristine\n", registry.read_bytes())
