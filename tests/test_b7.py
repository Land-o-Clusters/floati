"""B7: a secret-shape scan on a scratch projection must refuse.

The export rehearsal's first stage is scripts/export_public.py, which already
runs the name fence and the workflow-runner fence on the projected tree.
B7 adds a sibling step that refuses on four token shapes. RED-first: these
tests fail until that detector exists and is wired into the projection path.

Planted bytes are assembled at runtime so this file never carries a consecutive
secret shape of its own.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.private_artifacts import require_private_artifact


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "export_secret_fence.py"
SHAPES = (
    "github_pat",
    "aws_access_key",
    "private_key_block",
    "token_adjacent_hex40",
)
GITHUB_PAT = "ghp_" + ("A" * 36)
AWS_KEY = "AKIA" + ("B" * 16)
HEX40 = "ab" * 20


def private_key_block() -> str:
    begin = "-----BEGIN " + "RSA PRIVATE KEY-----"
    end = "-----END " + "RSA PRIVATE KEY-----"
    return f"{begin}\nMIIBOgIBAAJBAKfake\n{end}\n"


def plant(root: Path, shape: str) -> Path:
    path = root / f"{shape}.txt"
    if shape == "github_pat":
        path.write_text(f"auth {GITHUB_PAT}\n", encoding="utf-8")
    elif shape == "aws_access_key":
        path.write_text(f"id={AWS_KEY}\n", encoding="utf-8")
    elif shape == "private_key_block":
        path.write_text(private_key_block(), encoding="utf-8")
    elif shape == "token_adjacent_hex40":
        path.write_text(f"token {HEX40}\n", encoding="utf-8")
    else:
        raise AssertionError(f"unknown shape {shape}")
    return path


class B7SecretFenceTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.find_spec("scripts.export_secret_fence")
        self.assertIsNotNone(spec, "scripts.export_secret_fence is missing")
        return importlib.import_module("scripts.export_secret_fence")

    def exporter(self):
        spec = importlib.util.find_spec("scripts.export_public")
        self.assertIsNotNone(spec, "scripts.export_public is missing")
        return importlib.import_module("scripts.export_public")

    def test_each_planted_shape_is_refused_on_a_scratch_projection(self) -> None:
        """RED: a planted token of each named shape passes the rehearsal today."""

        module = self.module()
        for shape in SHAPES:
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                plant(root, shape)
                findings = module.scan_tree(root)
                codes = [finding["code"] for finding in findings]
                self.assertIn(shape, codes, findings)

    def test_export_projection_wires_the_secret_fence(self) -> None:
        """The detector is a rehearsal step, not merely an importable helper."""

        require_private_artifact(self, "scripts/export_public.py")
        exporter = self.exporter()
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            plant(target, "github_pat")
            with self.assertRaises(exporter.ExportRefusal) as caught:
                exporter.refuse_secret_shapes(target)
            self.assertEqual("public_export_secret_fence_failed", caught.exception.code)
            codes = [row["code"] for row in caught.exception.evidence["findings"]]
            self.assertIn("github_pat", codes)

    def test_rehearsal_stages_name_the_secret_fence(self) -> None:
        module = self.module()
        self.assertIn("secret_fence", module.REHEARSAL_STAGES)

    def test_clean_sha_without_the_word_token_is_not_a_finding(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "commit.txt").write_text(f"landed {HEX40}\n", encoding="utf-8")
            self.assertEqual([], module.scan_tree(root))

    def test_fragmented_pat_constructor_is_not_a_finding(self) -> None:
        """The intake fixture style (prefix plus 36 letters) is source, not a token."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "construct.py").write_text(
                'classic = "ghp_" + "a" * 36\n', encoding="utf-8"
            )
            self.assertEqual([], module.scan_tree(root))

    def test_harbor_tree_is_the_false_positive_control(self) -> None:
        module = self.module()
        census: dict[str, int] = {}
        findings = module.scan_tree(REPOSITORY_ROOT, census=census)
        self.assertEqual([], findings)
        self.assertGreater(census["files_examined"], 0)
        self.assertGreater(census["candidates_examined"], 0)

    def test_undeclared_gitleaks_is_a_typed_absence_not_a_skip(self) -> None:
        module = self.module()
        observation = module.observe_gitleaks(None)
        self.assertEqual("undeclared", observation["state"])
        self.assertEqual("operator_executable_absent", observation["reason"])
        self.assertNotEqual("skip", observation.get("state"))

    def test_declared_missing_gitleaks_is_a_typed_absence(self) -> None:
        module = self.module()
        observation = module.observe_gitleaks("/var/empty/gitleaks-not-installed")
        self.assertEqual("absent", observation["state"])
        self.assertEqual("operator_executable_absent", observation["reason"])

    def test_observe_gitleaks_uses_the_house_explicit_executable_predicate(self) -> None:
        """The gitleaks wrap imports the house predicate; a copy would ignore this patch."""

        module = self.module()
        sentinel = "/declared/gitleaks-from-predicate"
        with mock.patch(
            "floati.fleet_update._explicit_executable",
            return_value=sentinel,
        ):
            observation = module.observe_gitleaks(
                "/var/empty/gitleaks-not-installed"
            )
        self.assertEqual("declared", observation["state"])
        self.assertEqual(sentinel, observation["path"])

    def test_cli_refuses_a_planted_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plant(root, "aws_access_key")
            completed = subprocess.run(
                [sys.executable, "-E", str(SCRIPT), str(root)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(20, completed.returncode)
            artifact = json.loads(completed.stdout)
            self.assertEqual("refused", artifact["status"])
            self.assertEqual(
                "public_export_secret_fence_failed", artifact["evidence"]["code"]
            )
            codes = [row["code"] for row in artifact["evidence"]["findings"]]
            self.assertIn("aws_access_key", codes)
            gitleaks = artifact["evidence"]["gitleaks"]
            self.assertEqual("undeclared", gitleaks["state"])
            self.assertEqual("operator_executable_absent", gitleaks["reason"])

    def test_source_never_discovers_a_host_gitleaks(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("shutil.which", source)
        self.assertNotIn("os.environ[\"PATH\"]", source)
