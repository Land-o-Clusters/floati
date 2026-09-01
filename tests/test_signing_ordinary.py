from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from floati.signing import _directory_relative, _minisign, _ordinary, sign_minisign
from tests.temp_roots import REAL_TEMP_ROOT


class SigningOrdinaryPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private = Path(
            tempfile.mkdtemp(prefix="floati-ci-r2-1-", dir=REAL_TEMP_ROOT)
        )
        self.addCleanup(self._cleanup_private)

    def _cleanup_private(self) -> None:
        shutil.rmtree(self.private, ignore_errors=True)

    def test_undeclared_minisign_never_selects_a_path_decoy(self) -> None:
        """Catches signature verification trusting a PATH-selected binary."""

        decoy = self.private / "minisign"
        decoy.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        decoy.chmod(0o755)

        with (
            mock.patch.dict(os.environ, {"PATH": str(self.private)}),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            _minisign()

        self.assertEqual("signature_tool_absent", caught.exception.code)
        self.assertIn("--minisign-executable", caught.exception.remedy or "")

    def test_declared_minisign_uses_the_house_explicit_executable_fence(self) -> None:
        """Catches a declared binary bypassing Policy A or following a symlink."""

        tool = self.private / "minisign-real"
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
        link = self.private / "minisign-link"
        link.symlink_to(tool)

        self.assertEqual(tool, _minisign(tool))
        with self.assertRaises(ProtocolRefusal) as caught:
            _minisign(link)
        self.assertEqual("signature_tool_invalid", caught.exception.code)

    def test_directory_relative_refuses_dotdot_and_symlink_escape_without_ordinary(self) -> None:
        """Containment is independent of _ordinary's ancestor walk."""

        outside = Path(REAL_TEMP_ROOT) / f"floati-ci-r2-1-outside-{os.getpid()}"
        outside.write_bytes(b"secret\n")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        (self.private / "inside.bin").write_bytes(b"inside\n")
        (self.private / "escape").symlink_to(outside)

        contained = _directory_relative(self.private, Path("inside.bin"))
        self.assertEqual(self.private / "inside.bin", contained)

        with self.assertRaises(ProtocolRefusal) as dotdot:
            _directory_relative(self.private, Path("..") / outside.name)
        self.assertEqual("path_not_contained", dotdot.exception.code)

        with self.assertRaises(ProtocolRefusal) as escaped:
            _directory_relative(self.private, Path("escape"))
        self.assertEqual("path_not_contained", escaped.exception.code)

    def test_ordinary_refuses_a_leaf_symlink_even_when_the_target_is_a_regular_file(self) -> None:
        target = self.private / "real.bin"
        target.write_bytes(b"ok\n")
        link = self.private / "link.bin"
        link.symlink_to(target)
        with self.assertRaises(ProtocolRefusal) as caught:
            _ordinary(
                link,
                "signature_artifact_missing",
                "the selected artifact is not one ordinary file inside the root",
            )
        self.assertEqual("signature_artifact_missing", caught.exception.code)
        self.assertTrue(target.is_file())

    def test_ordinary_accepts_a_regular_file_under_private_tmp(self) -> None:
        artifact = self.private / "artifact.bin"
        artifact.write_bytes(b"ok\n")
        self.assertEqual(
            artifact,
            _ordinary(
                artifact,
                "signature_artifact_missing",
                "the selected artifact is not one ordinary file inside the root",
            ),
        )

    def test_ordinary_accepts_a_regular_file_under_host_tmpdir(self) -> None:
        """macOS default TMPDIR lives under a symlinked /var; the check is the leaf."""

        lexical_root = Path(tempfile.mkdtemp(prefix="floati-ci-r2-1-tmpdir-"))
        self.addCleanup(lambda: shutil.rmtree(lexical_root, ignore_errors=True))
        artifact = lexical_root / "artifact.bin"
        artifact.write_bytes(b"ok\n")
        if artifact.resolve() == artifact:
            self.skipTest(
                "this host's default TMPDIR has no symlinked ancestor; "
                "the Darwin mkdtemp refusal is unobservable here"
            )
        try:
            accepted = _ordinary(
                artifact,
                "signature_artifact_missing",
                "the selected artifact is not one ordinary file inside the root",
            )
        except ProtocolRefusal as exc:
            self.fail(f"ordinary file under host TMPDIR refused {exc.code}")
        self.assertEqual(artifact, accepted)


class SigningKeyLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(
            tempfile.mkdtemp(prefix="floati-ci-r2-1-key-", dir=REAL_TEMP_ROOT)
        )
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.home = self.base / "floatihome"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        artifact = self.root.resolve_relative(Path("checkpoints/checkpoint.json"))
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b'{"ok": true}\n')
        self.artifact = Path("checkpoints/checkpoint.json")
        self.signature = Path("checkpoints/checkpoint.json.minisig")
        keys = self.home / "keys"
        keys.mkdir()
        self.direct_key = keys / "release.key"
        self.direct_key.write_bytes(b"unusable-secret-key\n")
        sneak = self.base / "sneak"
        sneak.symlink_to(keys)
        self.sneak_key = sneak / "release.key"

    def _sign(self, secret_key: Path):
        return sign_minisign(
            self.root,
            self.artifact,
            self.signature,
            secret_key=secret_key,
            version="0.0.0-fixture",
        )

    def test_secret_key_reached_via_symlinked_ancestor_is_location_invalid(self) -> None:
        """The key lives inside the root; reaching it through sneak must not sign."""

        self.assertFalse(self.sneak_key.is_symlink())
        self.assertTrue(self.sneak_key.is_file())
        self.assertNotEqual(self.sneak_key.resolve(strict=True), self.sneak_key)
        self.assertEqual(self.sneak_key.resolve(strict=True), self.direct_key.resolve())
        with self.assertRaises(ProtocolRefusal) as caught:
            self._sign(self.sneak_key)
        self.assertEqual("signature_key_location_invalid", caught.exception.code)

    def test_secret_key_on_the_direct_path_inside_the_root_is_still_refused(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            self._sign(self.direct_key)
        self.assertEqual("signature_key_location_invalid", caught.exception.code)

    def test_secret_key_unavailable_detail_does_not_claim_canonical(self) -> None:
        missing = self.base / "absent.key"
        with self.assertRaises(ProtocolRefusal) as caught:
            self._sign(missing)
        self.assertEqual("signature_key_unavailable", caught.exception.code)
        self.assertNotIn("canonical", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
