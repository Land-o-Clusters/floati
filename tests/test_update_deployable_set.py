"""An installed updater trusts a path class, not its release's file names."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from floati.deploy import DeploymentWriter
from tests.git_fixture_cache import cached_tree
from tests.installed_host_seed import restore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class InstalledUpdateDeployableSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base, _ = restore("deployable-revision-a", self._install_revision_a)
        self.addCleanup(shutil.rmtree, self.base)
        self.source = self.base / "source"
        self.destination = self.base / "installed"
        self.git(self.source, "update-index", "-q", "--refresh")

    def _install_revision_a(self, base: Path) -> None:
        source = base / "source"
        destination = base / "installed"

        def build(root: Path) -> None:
            manifest = json.loads((REPOSITORY_ROOT / "bundle-manifest.v0.json").read_text())
            for entry in manifest["files"]:
                relative = entry["path"]
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPOSITORY_ROOT / relative, target)
            self.write_manifest(root, manifest)
            self.git(root, "init", "-q", "--initial-branch=lane/hm0")
            self.git(root, "add", ".")
            self.git(root, "commit", "-q", "-m", "Fixture revision A")

        cached_tree(source, ("update-deployable-set-revision-a",), build)
        DeploymentWriter(
            source, destination, "install", ref="HEAD",
            committed_tree=True, installer_path="/usr/bin:/bin",
        ).run()

    @staticmethod
    def git(root: Path, *arguments: str) -> str:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update({
            "GIT_AUTHOR_NAME": "Floati Test", "GIT_COMMITTER_NAME": "Floati Test",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        })
        return subprocess.run(
            ["/usr/bin/git", *arguments], cwd=root, env=environment,
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    @staticmethod
    def write_manifest(root: Path, manifest: dict) -> None:
        for entry in manifest["files"]:
            entry["sha256"] = hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest()
        manifest["files"].sort(key=lambda entry: entry["path"])
        (root / "bundle-manifest.v0.json").write_text(json.dumps(manifest, indent=2) + "\n")

    def revision_b(self, relative: str, *, tracked: bool = True) -> str:
        target = self.source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
        # The process must load revision A's verifier, even though B carries
        # a different verifier alongside its expanded manifest.
        verifier = self.source / "floati/manifest.py"
        verifier.write_text(verifier.read_text() + "\nTARGET_REVISION = 'B'\n")
        manifest = json.loads((self.source / "bundle-manifest.v0.json").read_text())
        manifest["files"].append({"path": relative, "sha256": ""})
        self.write_manifest(self.source, manifest)
        self.git(self.source, "add", "bundle-manifest.v0.json", "floati/manifest.py")
        if tracked:
            self.git(self.source, "add", relative)
        self.git(self.source, "commit", "-q", "-m", "Fixture revision B")
        return self.git(self.source, "rev-parse", "HEAD")

    def installed_update(self, ref: str) -> tuple[subprocess.CompletedProcess, dict]:
        environment = os.environ.copy()
        environment.update({"PYTHONPATH": str(self.destination), "PATH": "/usr/bin:/bin"})
        result = subprocess.run(
            ["/usr/bin/python3", "-B", "-c",
             "import pathlib, sys; import floati.manifest as verifier; "
             "assert pathlib.Path(verifier.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1])); "
             "assert not hasattr(verifier, 'TARGET_REVISION'); "
             "del sys.argv[1]; from floati.cli import main; raise SystemExit(main())",
             str(self.destination), "update", "--source", str(self.source),
             "--destination", str(self.destination), "--committed-tree", "--ref", ref, "--json"],
            cwd=self.base, env=environment, capture_output=True, text=True,
        )
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result, json.loads(result.stdout)

    def installed_bytes(self) -> dict[str, bytes]:
        return {path.relative_to(self.destination).as_posix(): path.read_bytes()
                for path in self.destination.rglob("*") if path.is_file()}

    def test_installed_revision_a_updates_to_b_with_a_new_trusted_script(self) -> None:
        relative = "scripts/floati-newtool"
        ref = self.revision_b(relative)
        result, artifact = self.installed_update(ref)
        self.assertEqual(0, result.returncode, artifact)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(ref, artifact["evidence"]["source_sha"])
        self.assertEqual((self.source / relative).read_bytes(), (self.destination / relative).read_bytes())
        metadata = json.loads((self.destination / ".floati-install/manifest.v0.json").read_text())
        self.assertIn(relative, {entry["path"] for entry in metadata["files"]})

    def test_target_cannot_declare_a_script_outside_the_installed_class(self) -> None:
        relative = "scripts/evil.sh"
        ref = self.revision_b(relative)
        before = self.installed_bytes()
        result, artifact = self.installed_update(ref)
        self.assertEqual(33, result.returncode, artifact)
        self.assertEqual("deployment_manifest_invalid", artifact["evidence"]["code"])
        self.assertIn("manifest_path_outside_deployable_class:" + relative, artifact["evidence"]["detail"])
        self.assertEqual(before, self.installed_bytes())

    def test_untracked_target_file_cannot_be_installed(self) -> None:
        ref = self.revision_b("scripts/floati-newtool", tracked=False)
        before = self.installed_bytes()
        result, artifact = self.installed_update(ref)
        self.assertEqual(20, result.returncode, artifact)
        self.assertEqual("deployment_currency_unavailable", artifact["evidence"]["code"])
        self.assertEqual(before, self.installed_bytes())


if __name__ == "__main__":
    unittest.main()
