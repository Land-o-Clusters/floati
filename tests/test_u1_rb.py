from __future__ import annotations

from tests.test_cli import LAUNCHER

import importlib
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
import tests.test_au1_s2 as s2_fixture

try:
    from floati.update_apply import rollback_update
except (ImportError, ModuleNotFoundError):
    rollback_update = None

try:
    from floati.update_apply import rollback_update
except (ImportError, ModuleNotFoundError):
    rollback_update = None


class U1RBTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = s2_fixture.AU1S2Tests(
            "test_s2_09_a_to_b_to_a_rollback_uses_the_same_verified_apply_path"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _require_rollback(self):
        self.assertIsNotNone(
            rollback_update,
            "U1-RB must expose rollback through the existing apply mechanism",
        )
        return rollback_update

    def _run_rollback(
        self,
        bundle: bytes | object,
        *,
        to_sha: str,
        idempotency_key: str = "u1-rb-one",
    ) -> dict[str, object]:
        operation = self._require_rollback()
        transport = importlib.import_module("floati.update_transport")
        fetch = mock.patch.object(
            transport,
            "fetch_one_https",
            side_effect=bundle if callable(bundle) else None,
            return_value=mock.DEFAULT if callable(bundle) else bundle,
        )
        path = os.pathsep.join(
            (
                str(self.fixture.source / "scripts"),
                str(self.fixture.destination / "scripts"),
                str(self.fixture.git_directory),
            )
        )
        with (
            fetch,
            mock.patch.dict(os.environ, {"PATH": path}, clear=False),
        ):
            return operation(
                destination=self.fixture.destination,
                channel=self.fixture.channel,
                entrypoint=self.fixture.entrypoint,
                to_sha=to_sha,
                idempotency_key=idempotency_key,
            )

    def test_missing_to_is_arguments_invalid_and_names_the_flag(self) -> None:
        """Catches rollback existing as apply-with-previous rather than a --to verb."""

        completed = subprocess.run(
            [
                str(LAUNCHER),
                "update",
                "rollback",
                "--destination",
                str(Path("/var/empty")),
                "--channel",
                "https://updates.example.invalid/release-index.v0",
                "--idempotency-key",
                "u1-rb-missing-to",
            ],
            cwd=s2_fixture.REPOSITORY_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(20, completed.returncode, completed.stderr)
        artifact = json.loads(completed.stdout)
        self.assertEqual("", completed.stderr)
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])
        self.assertIn("--to", artifact["evidence"]["detail"])

    def test_invalid_to_sha_refuses_before_apply(self) -> None:
        """Catches a rollback that treats --to as a free-form version alias."""

        self.fixture._observe()
        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_rollback(self.fixture.bundle_a, to_sha="not-a-source-sha")
        self.assertEqual("update_rollback_to_invalid", caught.exception.code)

    def test_no_prior_apply_refuses(self) -> None:
        """Catches rollback inventing a previous state from the install SHA."""

        self.fixture._observe()
        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_rollback(self.fixture.bundle_a, to_sha=self.fixture.sha_a)
        self.assertEqual("update_rollback_prior_missing", caught.exception.code)
        self.assertEqual(
            self.fixture.bytes_a, self.fixture._destination_managed_bytes()
        )

    def test_to_must_name_the_previous_source_sha(self) -> None:
        """Catches rollback accepting the current SHA or an unrelated catalog SHA."""

        self.fixture._observe()
        self.fixture._run_apply(self.fixture.bundle_b)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._run_rollback(self.fixture.bundle_b, to_sha=self.fixture.sha_b)
        self.assertEqual("update_rollback_target_mismatch", caught.exception.code)
        self.assertEqual(
            self.fixture.bytes_b, self.fixture._destination_managed_bytes()
        )

    def test_rollback_reapplies_the_previous_signed_bundle(self) -> None:
        """Catches a second writer or a rollback that bypasses signed apply."""

        self.fixture._observe()
        applied_b = self.fixture._run_apply(
            self.fixture.bundle_b,
            version="2.0.0",
            idempotency_key="u1-rb-apply-b",
        )
        self.assertEqual(self.fixture.sha_b, applied_b["source_sha"])

        rolled = self._run_rollback(
            self.fixture.bundle_a,
            to_sha=self.fixture.sha_a,
            idempotency_key="u1-rb-apply-a",
        )
        self.assertEqual("update_application", rolled["kind"])
        self.assertEqual(self.fixture.sha_a, rolled["source_sha"])
        self.assertEqual(self.fixture.sha_b, rolled["previous_source_sha"])
        self.assertEqual(
            self.fixture.bytes_a, self.fixture._destination_managed_bytes()
        )

    def test_no_update_refusal_carries_unnamed_remedy(self) -> None:
        """Am.1: the public-#8 class — named update refusals must bind a remedy."""

        import ast

        from floati.errors import UNNAMED_REMEDY

        owned = {
            "update_rollback_to_invalid",
            "update_rollback_target_mismatch",
            "update_version_unavailable",
        }
        path = s2_fixture.REPOSITORY_ROOT / "floati" / "update_apply.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        missing: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            if name != "ProtocolRefusal" or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or first.value not in owned:
                continue
            if not any(keyword.arg == "remedy" for keyword in node.keywords):
                missing.append(f"{first.value}:{node.lineno}")
        self.assertEqual([], missing)

        self.fixture._observe()
        with self.assertRaises(ProtocolRefusal) as invalid:
            self._run_rollback(self.fixture.bundle_a, to_sha="not-a-source-sha")
        self.assertNotEqual(UNNAMED_REMEDY, invalid.exception.remedy)
        self.assertNotEqual(
            {"kind": "none", "why": "no action was named for this refusal"},
            invalid.exception.remedy,
        )

        with self.assertRaises(ProtocolRefusal) as missing_prior:
            self._run_rollback(self.fixture.bundle_a, to_sha=self.fixture.sha_a)
        self.assertNotEqual(UNNAMED_REMEDY, missing_prior.exception.remedy)

        self.fixture._run_apply(self.fixture.bundle_b)
        with self.assertRaises(ProtocolRefusal) as mismatch:
            self._run_rollback(self.fixture.bundle_b, to_sha=self.fixture.sha_b)
        self.assertNotEqual(UNNAMED_REMEDY, mismatch.exception.remedy)

    def test_cli_rollback_uses_the_ruled_to_sha_shape(self) -> None:
        """Catches the domain operation existing without the governed CLI route."""

        completed = subprocess.run(
            [
                str(LAUNCHER),
                "update",
                "rollback",
                "--destination",
                str(self.fixture.destination),
                "--channel",
                self.fixture.channel,
                "--to",
                self.fixture.sha_a,
                "--idempotency-key",
                "u1-rb-cli",
            ],
            cwd=s2_fixture.REPOSITORY_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(20, completed.returncode, completed.stderr)
        artifact = json.loads(completed.stdout)
        self.assertEqual("", completed.stderr)
        self.assertEqual("update_consent_missing", artifact["evidence"]["code"])


if __name__ == "__main__":
    unittest.main()
