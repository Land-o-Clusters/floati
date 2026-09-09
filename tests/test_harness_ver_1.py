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
    def doctor_with_path(self, *directories: Path):
        from floati.doctor import Doctor
        import unittest.mock

        path = os.pathsep.join(str(directory) for directory in directories)
        patched = os.environ.copy()
        patched["PATH"] = path
        with unittest.mock.patch.dict(os.environ, patched, clear=True):
            return Doctor(Path.cwd(), str(self.root.path), ref="HEAD").artifact()

    def write_claude(self, directory: Path, version: str) -> Path:
        """Fake claude binary that prints the measured Claude Code line."""

        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / "claude"
        binary.write_text(
            "#!/bin/sh\necho '" + version + "'\n", encoding="utf-8"
        )
        binary.chmod(0o755)
        return binary

    def test_doctor_warns_harness_binary_shadowed(self) -> None:
        """The doctor finding: typed warning, remedy names the declared
        path; identical copies are a note, not a warning."""

        earlier = self.write_harness(self.base / "path-a" / "bin", "1.0")
        declared = self.write_harness(self.base / "local" / "bin", "2.0")
        self.bind(declared)

        artifact, _rc = self.doctor_with_path(earlier.parent, declared.parent)

        shadowed = [
            row for row in artifact["findings"]
            if row["code"] == "harness_binary_shadowed"
        ]
        self.assertEqual(1, len(shadowed), artifact["findings"])
        self.assertEqual("warning", shadowed[0]["severity"])
        self.assertIn(str(declared), shadowed[0]["remediation"])
        self.assertIn("zcode 1.0", shadowed[0]["detail"])
        self.assertIn("zcode 2.0", shadowed[0]["detail"])

    def test_fq4_claude_dual_version_names_bound_path_rule_and_shadow(
        self,
    ) -> None:
        """FQ-4 / #18: reproduce 2.1.231 vs 2.1.238 on a scratch PATH.

        Doctor USER-FACING copy must name: which absolute path floati
        bound, by what rule, its measured version, and the earlier PATH
        copy at the other version.
        """

        path_copy = self.write_claude(
            self.base / "homebrew" / "bin", "2.1.231 (Claude Code)"
        )
        bound = self.write_claude(
            self.base / "user-local" / "bin", "2.1.238 (Claude Code)"
        )
        self.bind(bound)

        artifact, _rc = self.doctor_with_path(path_copy.parent, bound.parent)

        shadowed = [
            row for row in artifact["findings"]
            if row["code"] == "harness_binary_shadowed"
        ]
        self.assertEqual(1, len(shadowed), artifact["findings"])
        detail = shadowed[0]["detail"]
        self.assertTrue(
            detail.startswith(
                "floati runs {0} — the wake-daemon adapter binds it by "
                "absolute path".format(bound)
            ),
            detail,
        )
        self.assertIn("and it measures 2.1.238 (Claude Code)", detail)
        self.assertIn(
            "an earlier copy on your PATH is a different version: {0} "
            "(2.1.231 (Claude Code))".format(path_copy),
            detail,
        )
        self.assertLess(
            detail.index("an earlier copy on your PATH"),
            detail.index("and it measures"),
            "the shadow answer leads; the bound version follows",
        )
        self.assertIn("wake-daemon adapter binds it by absolute path", detail)
        remediation = shadowed[0]["remediation"]
        self.assertIn("nothing here is broken for floati", remediation)
        self.assertIn(str(bound), remediation)
        self.assertIn("PATH is an inventory to floati", remediation)

    def test_shadow_detail_leads_with_copies_and_summarizes_unreadable(
        self,
    ) -> None:
        """SHADOW-DETAIL-1: the answer first; unreadable PATH as a count."""

        earlier = self.write_harness(self.base / "path-a" / "bin", "1.0")
        declared = self.write_harness(self.base / "local" / "bin", "2.0")
        self.bind(declared)
        unreadable = [
            str(self.base / "unreadable-{0}".format(index))
            for index in range(20)
        ]
        path_dirs = [Path(entry) for entry in unreadable] + [
            earlier.parent, declared.parent,
        ]

        artifact, _rc = self.doctor_with_path(*path_dirs)

        shadowed = [
            row for row in artifact["findings"]
            if row["code"] == "harness_binary_shadowed"
        ]
        self.assertEqual(1, len(shadowed), artifact["findings"])
        detail = shadowed[0]["detail"]
        self.assertIn("an earlier copy on your PATH", detail)
        self.assertIn("floati could not read 20 PATH entries", detail)
        self.assertNotIn(unreadable[0], detail)
        self.assertNotIn(unreadable[-1], detail)
        self.assertLess(
            detail.index("an earlier copy on your PATH"),
            detail.index("floati could not read"),
            detail,
        )
        self.assertEqual(
            unreadable, shadowed[0].get("path_entries_unreadable"),
        )


if __name__ == "__main__":
    unittest.main()
