"""HARNESS-VER-1 (public issue #18): harness binaries installed twice.

The measured incident: a harness existed as two copies - the PATH copy
and a user-local copy - disagreeing on version, and nothing reported
it, so a terminal ran one harness while floati ran the other. Under the
executable-provenance policy (IN-4) PATH is consulted ONLY as an
operator inventory, never to choose: per declared harness, the
inventory reports the declared executable's measured version AND every
other copy of the same binary name earlier on PATH with its measured
version. A differing (or unmeasurable) earlier copy is the typed
warning harness_binary_shadowed naming the declared path; identical
copies are a note; PATH entries that cannot be read are named, never a
crash (the INS-1 shape).
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class HarnessInventoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)

    def write_harness(self, directory: Path, version: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / "zcode"
        binary.write_text(
            "#!/bin/sh\necho 'zcode " + version + "'\n", encoding="utf-8"
        )
        binary.chmod(0o755)
        return binary

    def bind(self, executable: Path) -> None:
        from floati.registry import Registry
        from floati.wake_daemon_adapters import adapter_contract_digest
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate

        Registry(self.root).register("seat-a", "worker")
        AdapterBindingStore(self.root).write(
            DaemonCoordinate(self.root, "seat-a", "zcode"),
            session_id="seat-a-hook-session",
            workspace=self.base,
            executable=executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("zcode"),
            binding_epoch=1,
        )


class HarnessVersionInventoryTests(HarnessInventoryFixture):
    def load_module(self):
        from floati import harness_versions

        return harness_versions

    def test_an_earlier_differing_copy_shadows_the_declared_binary(self) -> None:
        """RED: nothing reports the version disagreement today."""

        module = self.load_module()
        earlier = self.write_harness(self.base / "path-a" / "bin", "1.0")
        declared = self.write_harness(self.base / "local" / "bin", "2.0")
        self.bind(declared)
        environ = {
            "PATH": os.pathsep.join([
                str(earlier.parent), str(declared.parent),
            ])
        }

        artifact = module.harness_version_artifact(self.root, environ=environ)

        rows = artifact["declared"]
        self.assertEqual(1, len(rows), rows)
        row = rows[0]
        self.assertEqual("seat-a", row["node"])
        self.assertEqual("zcode", row["harness"])
        self.assertEqual(str(declared), row["executable"])
        self.assertEqual("zcode 2.0", row["version"])
        shadows = row["shadow_inventory"]["shadows"]
        self.assertEqual(
            [{"path": str(earlier), "version": "zcode 1.0"}], shadows
        )
        self.assertEqual("shadowed", row["status"])

    def test_identical_copies_are_a_note(self) -> None:
        module = self.load_module()
        earlier = self.write_harness(self.base / "path-a" / "bin", "1.0")
        declared = self.write_harness(self.base / "local" / "bin", "1.0")
        self.bind(declared)
        environ = {"PATH": os.pathsep.join([
            str(earlier.parent), str(declared.parent),
        ])}

        artifact = module.harness_version_artifact(self.root, environ=environ)

        row = artifact["declared"][0]
        self.assertEqual("zcode 1.0", row["version"])
        self.assertEqual("identical", row["status"])
        self.assertEqual(str(earlier), row["shadow_inventory"]["shadows"][0]["path"])

    def test_unreadable_path_entries_are_named_never_a_crash(self) -> None:
        """The INS-1 shape: an entry that cannot be read is named."""

        module = self.load_module()
        declared = self.write_harness(self.base / "local" / "bin", "2.0")
        self.bind(declared)
        missing = self.base / "does-not-exist"
        not_a_dir = self.base / "not-a-dir"
        not_a_dir.write_bytes(b"")
        environ = {"PATH": os.pathsep.join([
            str(missing), str(not_a_dir), str(declared.parent),
        ])}

        artifact = module.harness_version_artifact(self.root, environ=environ)

        row = artifact["declared"][0]
        self.assertEqual(
            [str(missing), str(not_a_dir)],
            row["shadow_inventory"]["unreadable_entries"],
        )
        self.assertEqual([], row["shadow_inventory"]["shadows"])

    def test_an_off_path_declared_binary_is_shadowed_by_the_first_copy(
        self,
    ) -> None:
        module = self.load_module()
        earlier = self.write_harness(self.base / "path-a" / "bin", "9.9")
        declared = self.write_harness(self.base / "elsewhere", "2.0")
        self.bind(declared)
        environ = {"PATH": str(earlier.parent)}

        artifact = module.harness_version_artifact(self.root, environ=environ)

        row = artifact["declared"][0]
        self.assertEqual("shadowed", row["status"])
        self.assertEqual(
            [{"path": str(earlier), "version": "zcode 9.9"}],
            row["shadow_inventory"]["shadows"],
        )

    def test_an_unmeasurable_copy_is_named_never_guessed(self) -> None:
        module = self.load_module()
        earlier_dir = self.base / "path-a" / "bin"
        earlier_dir.mkdir(parents=True)
        broken = earlier_dir / "zcode"
        broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        broken.chmod(0o755)
        declared = self.write_harness(self.base / "local" / "bin", "2.0")
        self.bind(declared)
        environ = {"PATH": os.pathsep.join([
            str(earlier_dir), str(declared.parent),
        ])}

        artifact = module.harness_version_artifact(self.root, environ=environ)

        row = artifact["declared"][0]
        self.assertIsNone(row["shadow_inventory"]["shadows"][0]["version"])
        self.assertEqual("unmeasurable", row["status"])


class DoctorHarnessShadowFindingTests(HarnessInventoryFixture):
    def test_doctor_warns_harness_binary_shadowed(self) -> None:
        """The doctor finding: typed warning, remedy names the declared
        path; identical copies are a note, not a warning."""

        from floati.doctor import Doctor

        earlier = self.write_harness(self.base / "path-a" / "bin", "1.0")
        declared = self.write_harness(self.base / "local" / "bin", "2.0")
        self.bind(declared)
        self.addCleanup(os.environ.pop, "HARNESS_VER_1_TEST_PATH", None)
        os.environ["HARNESS_VER_1_TEST_PATH"] = os.pathsep.join([
            str(earlier.parent), str(declared.parent),
        ])

        patched_path = os.environ.copy()
        patched_path["PATH"] = os.environ["HARNESS_VER_1_TEST_PATH"]
        import unittest.mock

        with unittest.mock.patch.dict(os.environ, patched_path, clear=True):
            artifact, _rc = Doctor(
                Path.cwd(), str(self.root.path), ref="HEAD"
            ).artifact()

        shadowed = [
            row for row in artifact["findings"]
            if row["code"] == "harness_binary_shadowed"
        ]
        self.assertEqual(1, len(shadowed), artifact["findings"])
        self.assertEqual("warning", shadowed[0]["severity"])
        self.assertIn(str(declared), shadowed[0]["remediation"])
        self.assertIn("zcode 1.0", shadowed[0]["detail"])
        self.assertIn("zcode 2.0", shadowed[0]["detail"])


if __name__ == "__main__":
    unittest.main()
