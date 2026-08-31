from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.deploy import DeploymentWriter
from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot

try:
    from floati.signing import verify_minisign_paths
except ImportError:
    verify_minisign_paths = None

try:
    from floati.update_ownership import (
        observe_install_ownership,
        require_standalone_ownership,
    )
except ImportError:
    observe_install_ownership = None
    require_standalone_ownership = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MINISIGN_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "minisign"
LIVE_KEY_ID = "A9FBAB3833B4D4EF"


class AU1S0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.staging = self.base / "staging"
        self.staging.mkdir()
        self._copy_minisign_fixture(
            "checkpoint.json", self.staging / "release" / "checkpoint.json"
        )
        self._copy_minisign_fixture(
            "checkpoint.json.minisig",
            self.staging / "release" / "checkpoint.json.minisig",
        )
        self._copy_minisign_fixture(
            "fixture.pub", self.staging / "trust" / "fixture.pub"
        )

        self.root = FloatiRoot.open(self.base / "fleet", "alpha")
        self._copy_minisign_fixture(
            "checkpoint.json",
            self.root.resolve_relative("release/checkpoint.json"),
        )
        self._copy_minisign_fixture(
            "checkpoint.json.minisig",
            self.root.resolve_relative("release/checkpoint.json.minisig"),
        )
        self._copy_minisign_fixture(
            "fixture.pub", self.root.resolve_relative("trust/fixture.pub")
        )

    @staticmethod
    def _copy_minisign_fixture(name: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MINISIGN_FIXTURES / name, target)

    def _source_checkout(self) -> Path:
        source = self.base / "source"
        source.mkdir()
        files = {
            "floati/__init__.py": b"VERSION = 'fixture'\n",
            "schemas/v0/example.json": b'{"schema_version":0}\n',
            "scripts/floati": b"#!/bin/sh\nexit 0\n",
            "scripts/floati-codex-wait": b"#!/bin/sh\nexit 0\n",
        }
        for relative, payload in files.items():
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            if relative.startswith("scripts/"):
                path.chmod(0o755)
        manifest = {
            "schema_version": 0,
            "protocol_version": "0",
            "canonical_ref": "refs/heads/lane/hm0",
            "files": [
                {
                    "path": relative,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for relative, payload in sorted(files.items())
            ],
        }
        (source / "bundle-manifest.v0.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        for arguments in (
            ("init", "--quiet", "--initial-branch=lane/hm0"),
            ("config", "user.name", "Floati Test"),
            ("config", "user.email", "floati-test@example.invalid"),
            ("add", "."),
            ("commit", "--quiet", "-m", "fixture source"),
        ):
            subprocess.run(
                ["git", *arguments],
                cwd=source,
                check=True,
                capture_output=True,
                text=True,
            )
        return source

    @staticmethod
    def _metadata(
        destination: Path,
        *,
        schema_version: int,
        ownership: dict[str, object] | None = None,
    ) -> Path:
        entrypoint = destination / "scripts" / "floati"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_bytes(b"#!/bin/sh\nexit 0\n")
        entrypoint.chmod(0o755)
        payload: dict[str, object] = {
            "schema_version": schema_version,
            "source_ref": "refs/heads/main",
            "source_sha": "a" * 40,
            "files": [
                {
                    "path": "scripts/floati",
                    "sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
                }
            ],
        }
        if ownership is not None:
            payload["ownership"] = ownership
        metadata = destination / ".floati-install" / "manifest.v0.json"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return entrypoint

    def test_s0_path_root_verifier_accepts_the_real_fixture(self) -> None:
        """Catches update staging being forced to masquerade as a fleet root."""

        self.assertIsNotNone(
            verify_minisign_paths,
            "S0 must expose a directory-rooted Minisign verifier",
        )

        fact = verify_minisign_paths(
            self.staging,
            Path("release/checkpoint.json"),
            Path("release/checkpoint.json.minisig"),
            Path("trust/fixture.pub"),
            version="2.0.0-fixture",
            journal_id="fixture-journal",
            through_seq=7,
        )

        self.assertEqual("signature_verified", fact["state"])
        self.assertEqual("minisign", fact["tool"])

    def test_s0_path_root_verifier_refuses_escape(self) -> None:
        """Catches a release filename escaping the explicit staging directory."""

        self.assertIsNotNone(
            verify_minisign_paths,
            "S0 must expose a directory-rooted Minisign verifier",
        )

        with self.assertRaises(ProtocolRefusal) as caught:
            verify_minisign_paths(
                self.staging,
                Path("../checkpoint.json"),
                Path("release/checkpoint.json.minisig"),
                Path("trust/fixture.pub"),
                version="2.0.0-fixture",
            )

        self.assertEqual("path_not_contained", caught.exception.code)

    def test_s0_existing_signature_cli_twins_remain_byte_identical(self) -> None:
        """Catches the internal bridge changing the shipped signature artifact."""

        arguments = [
            "python3",
            "-m",
            "floati",
            "signature",
            "verify",
            "--root",
            str(self.root.tenant_home),
            "--artifact",
            "release/checkpoint.json",
            "--signature",
            "release/checkpoint.json.minisig",
            "--public-key",
            "trust/fixture.pub",
            "--version",
            "2.0.0-fixture",
            "--journal-id",
            "fixture-journal",
            "--through-seq",
            "7",
        ]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        plain = subprocess.run(
            arguments,
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
        )
        machine = subprocess.run(
            [*arguments, "--json"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
        )

        self.assertEqual(0, plain.returncode, plain.stderr)
        self.assertEqual(plain.returncode, machine.returncode)
        self.assertEqual(plain.stdout, machine.stdout)
        self.assertEqual(plain.stderr, machine.stderr)

    def test_s0_new_install_writes_standalone_ownership(self) -> None:
        """Catches a new install remaining indistinguishable from manager ownership."""

        source = self._source_checkout()
        destination = self.base / "installed"
        git_directory = Path(shutil.which("git") or "").resolve().parent
        path = os.pathsep.join((str(source / "scripts"), str(git_directory)))
        with mock.patch.dict(os.environ, {"PATH": path}, clear=False):
            DeploymentWriter(
                source,
                destination,
                "install",
                ref="HEAD",
                committed_tree=True,
            ).run()

        metadata = json.loads(
            (destination / ".floati-install/manifest.v0.json").read_text(
                encoding="utf-8"
            )
        )
        entrypoint = destination / "scripts" / "floati"
        self.assertEqual(1, metadata["schema_version"])
        self.assertEqual(
            {
                "kind": "floati_standalone",
                "destination": str(destination.resolve()),
                "entrypoint": "scripts/floati",
                "entrypoint_sha256": hashlib.sha256(
                    entrypoint.read_bytes()
                ).hexdigest(),
                "manager": None,
                "remedy": None,
            },
            metadata["ownership"],
        )

    def test_s0_legacy_receipt_is_unknown(self) -> None:
        """Catches a v0 receipt being silently promoted to standalone authority."""

        destination = self.base / "legacy-install"
        entrypoint = self._metadata(destination, schema_version=0)
        self.assertIsNotNone(
            observe_install_ownership,
            "S0 must project install ownership without network",
        )

        fact = observe_install_ownership(destination, entrypoint=entrypoint)

        self.assertEqual("unknown", fact["state"])
        self.assertEqual("legacy_receipt", fact["reason"])
        self.assertEqual("a" * 40, fact["source_sha"])

    def test_s0_package_manager_refuses_with_draft_remedy(self) -> None:
        """Catches a manager-owned install reaching any future transport call."""

        destination = self.base / "manager-install"
        entrypoint = destination / "scripts" / "floati"
        payload = b"#!/bin/sh\nexit 0\n"
        ownership = {
            "kind": "package_manager",
            "destination": str(destination.resolve()),
            "entrypoint": "scripts/floati",
            "entrypoint_sha256": hashlib.sha256(payload).hexdigest(),
            "manager": "homebrew",
            "remedy": "DRAFT - run brew upgrade floati",
        }
        entrypoint = self._metadata(
            destination, schema_version=1, ownership=ownership
        )
        self.assertIsNotNone(
            require_standalone_ownership,
            "S0 must refuse package-manager ownership before transport",
        )

        with (
            mock.patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("ownership refusal must precede transport"),
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            require_standalone_ownership(destination, entrypoint=entrypoint)

        self.assertEqual("update_package_manager_owned", caught.exception.code)
        self.assertEqual("DRAFT - run brew upgrade floati", caught.exception.remedy)

    def test_s0_entrypoint_digest_mismatch_is_typed(self) -> None:
        """Catches changed installed bytes retaining standalone authority."""

        destination = self.base / "changed-install"
        entrypoint = destination / "scripts" / "floati"
        original = b"#!/bin/sh\nexit 0\n"
        ownership = {
            "kind": "floati_standalone",
            "destination": str(destination.resolve()),
            "entrypoint": "scripts/floati",
            "entrypoint_sha256": hashlib.sha256(original).hexdigest(),
            "manager": None,
            "remedy": None,
        }
        entrypoint = self._metadata(
            destination, schema_version=1, ownership=ownership
        )
        entrypoint.write_bytes(b"#!/bin/sh\nexit 9\n")
        self.assertIsNotNone(
            observe_install_ownership,
            "S0 must project changed standalone entrypoint bytes",
        )

        fact = observe_install_ownership(destination, entrypoint=entrypoint)

        self.assertEqual("mismatch", fact["state"])
        self.assertEqual("entrypoint_digest_mismatch", fact["reason"])

    def test_s0_base_derives_live_public_key_and_backup_gate(self) -> None:
        """Catches S0 using the pre-ceremony trust snapshot from its old base."""

        trust = REPOSITORY_ROOT / "trust"
        keys = json.loads((trust / "keys.json").read_text(encoding="utf-8"))
        active = [item for item in keys["keys"] if item["status"] == "active"]
        self.assertEqual(1, len(active))
        public = (trust / active[0]["public_key_file"]).read_text(
            encoding="utf-8"
        ).splitlines()
        raw = base64.b64decode(public[1])
        embedded_key_id = raw[2:10][::-1].hex().upper()
        copy = (trust / "README.md").read_text(encoding="utf-8")

        self.assertEqual(LIVE_KEY_ID, active[0]["key_id"])
        self.assertEqual(LIVE_KEY_ID, embedded_key_id)
        self.assertIn("Ceremony status:** COMPLETE", copy)
        self.assertIn("off-machine backup made and byte-verified", copy)
        self.assertFalse(any(trust.glob("*.key")))


if __name__ == "__main__":
    unittest.main()
