from __future__ import annotations

from tests.test_cli import LAUNCHER

import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from tests import managed_test_tools

try:
    from floati.signing import sign_minisign, verify_minisign
except ModuleNotFoundError:
    sign_minisign = None
    verify_minisign = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "minisign"


class MinisignSigningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open(Path(self.temporary.name).resolve(), "alpha")
        self.artifact = Path("checkpoints/checkpoint.json")
        self.signature = Path("checkpoints/checkpoint.json.minisig")
        self.public_key = Path("trust/fixture.pub")
        self._copy_fixture("checkpoint.json", self.artifact)
        self._copy_fixture("checkpoint.json.minisig", self.signature)
        self._copy_fixture("fixture.pub", self.public_key)
        selected = managed_test_tools.executable(
            "FLOATI_TEST_MINISIGN_EXECUTABLE", "minisign"
        )
        self.assertIsNotNone(
            selected, "the S2 constants must execute against a real minisign binary"
        )
        self.minisign = Path(selected)

    def _copy_fixture(self, name: str, relative: Path) -> Path:
        target = self.root.resolve_relative(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(FIXTURES / name, target)
        return target

    def _require_api(self) -> None:
        self.assertIsNotNone(sign_minisign, "floati.signing must own S2 signing")
        self.assertIsNotNone(verify_minisign, "floati.signing must own S2 verification")

    def _verify(
        self,
        *,
        version: str = "2.0.0-fixture",
        minisign_executable: Path | None = None,
    ):
        self._require_api()
        if minisign_executable is None:
            minisign_executable = self.minisign
        return verify_minisign(
            self.root,
            self.artifact,
            self.signature,
            self.public_key,
            version=version,
            journal_id="fixture-journal",
            through_seq=7,
            minisign_executable=minisign_executable,
        )

    def test_s2_real_minisign_binary_verifies_fixture_and_signed_binding(self) -> None:
        result = self._verify()

        self.assertEqual("signature_verified", result["state"])
        self.assertEqual("minisign", result["tool"])
        self.assertEqual(
            {
                "filename": "checkpoint.json",
                "journal_id": "fixture-journal",
                "through_seq": 7,
                "version": "2.0.0-fixture",
            },
            result["binding"],
        )

    def test_s2_real_minisign_binary_signs_without_generating_a_key(self) -> None:
        self._require_api()
        output = Path("signed/checkpoint.json.minisig")

        signed = sign_minisign(
            self.root,
            self.artifact,
            output,
            secret_key=(FIXTURES / "fixture.key").resolve(),
            version="2.0.0-fixture",
            journal_id="fixture-journal",
            through_seq=7,
            minisign_executable=self.minisign,
        )
        verified = verify_minisign(
            self.root,
            self.artifact,
            output,
            self.public_key,
            version="2.0.0-fixture",
            journal_id="fixture-journal",
            through_seq=7,
            minisign_executable=self.minisign,
        )

        self.assertEqual("signature_signed", signed["state"])
        self.assertEqual("signature_verified", verified["state"])
        self.assertTrue(self.root.resolve_relative(output).is_file())
        self.assertFalse(any(self.root.tenant_home.rglob("*.key")))

    def test_s2_tampered_checkpoint_has_typed_invalid_signature_refusal(self) -> None:
        path = self.root.resolve_relative(self.artifact)
        path.write_bytes(path.read_bytes() + b" ")

        with self.assertRaises(ProtocolRefusal) as caught:
            self._verify()

        self.assertEqual("signature_invalid", caught.exception.code)

    def test_s2_signed_binding_mismatch_has_its_own_refusal(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            self._verify(version="2.0.1")

        self.assertEqual("signature_binding_mismatch", caught.exception.code)

    def test_s2_absent_declaration_is_a_typed_refusal_with_a_remedy(self) -> None:
        self._require_api()
        with self.assertRaises(ProtocolRefusal) as caught:
            verify_minisign(
                self.root,
                self.artifact,
                self.signature,
                self.public_key,
                version="2.0.0-fixture",
                journal_id="fixture-journal",
                through_seq=7,
            )

        self.assertEqual("signature_tool_absent", caught.exception.code)
        self.assertIn("operator-declared", caught.exception.detail)
        self.assertIn("--minisign-executable", caught.exception.remedy or "")

    def test_s2_cli_verify_emits_one_machine_artifact(self) -> None:
        completed = subprocess.run(
            [
                str(LAUNCHER), "signature", "verify",
                "--root", str(self.root.tenant_home),
                "--artifact", str(self.artifact),
                "--signature", str(self.signature),
                "--public-key", str(self.public_key),
                "--version", "2.0.0-fixture",
                "--journal-id", "fixture-journal",
                "--through-seq", "7",
                "--minisign-executable", str(self.minisign),
                "--json",
            ],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, len(completed.stdout.splitlines()))
        artifact = json.loads(completed.stdout)
        self.assertEqual("signature", artifact["command"])
        self.assertEqual("signature_verified", artifact["evidence"]["state"])

    def test_s2_cli_sign_emits_one_machine_artifact(self) -> None:
        output = Path("signed/checkpoint.json.minisig")
        completed = subprocess.run(
            [
                str(LAUNCHER), "signature", "sign",
                "--root", str(self.root.tenant_home),
                "--artifact", str(self.artifact),
                "--signature", str(output),
                "--secret-key", str((FIXTURES / "fixture.key").resolve()),
                "--version", "2.0.0-fixture",
                "--journal-id", "fixture-journal",
                "--through-seq", "7",
                "--minisign-executable", str(self.minisign),
                "--json",
            ],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, len(completed.stdout.splitlines()))
        artifact = json.loads(completed.stdout)
        self.assertEqual("signature_signed", artifact["evidence"]["state"])
        self.assertTrue(self.root.resolve_relative(output).is_file())

    def test_s2_cli_absent_declaration_is_a_named_refusal(self) -> None:
        environment = dict(os.environ)
        environment.update(
            {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
        )
        completed = subprocess.run(
            [
                str(LAUNCHER), "signature", "verify",
                "--root", str(self.root.tenant_home),
                "--artifact", str(self.artifact),
                "--signature", str(self.signature),
                "--public-key", str(self.public_key),
                "--version", "2.0.0-fixture",
                "--journal-id", "fixture-journal",
                "--through-seq", "7",
                "--json",
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(20, completed.returncode, completed.stdout)
        self.assertEqual("", completed.stderr)
        artifact = json.loads(completed.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("signature_tool_absent", artifact["evidence"]["code"])
        self.assertIn("--minisign-executable", artifact["evidence"]["remedy"])

    def test_s2_help_is_restamped_and_trust_scaffold_stays_draft_until_ceremony(self) -> None:
        for command in (
            ("signature",), ("signature", "sign"), ("signature", "verify"),
        ):
            with self.subTest(command=command):
                completed = subprocess.run(
                    [str(LAUNCHER), *command, "--help"],
                    cwd=REPOSITORY_ROOT,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertNotIn("DRAFT", completed.stdout)

        # Ceremony executed 2026-08-29: the scaffold pin flipped to the live-trust pin.
        # The live state is DERIVED, not quoted: keys.json's active key id must equal the
        # key id embedded in the .pub file's own base64 bytes, and the README must name it.
        trust_dir = REPOSITORY_ROOT / "trust"
        copy = (trust_dir / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("DRAFT", copy)
        keys = json.loads((trust_dir / "keys.json").read_text(encoding="utf-8"))
        active = [k for k in keys["keys"] if k["status"] == "active"]
        self.assertEqual(1, len(active))
        pub_lines = (trust_dir / active[0]["public_key_file"]).read_text(
            encoding="utf-8"
        ).splitlines()
        raw = base64.b64decode(pub_lines[1])
        self.assertEqual(b"Ed", raw[:2])
        embedded_key_id = raw[2:10][::-1].hex().upper()
        self.assertEqual(active[0]["key_id"], embedded_key_id)
        self.assertIn(embedded_key_id, copy)
        self.assertFalse(any(trust_dir.glob("*.key")))


if __name__ == "__main__":
    unittest.main()
