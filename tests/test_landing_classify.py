from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "export_public.py"
POLICY_PATH = REPOSITORY_ROOT / ".github" / "public-export-policy.v0.json"
GATEWAY_PATHS = (
    "tools/codex/codex-fleet-bus.py",
    "tools/codex/codex-fleet-bus.sha256.json",
    "tools/codex/install_codex_gateway.py",
)
UNCLASSIFIED_ADDED = "tools/codex/future-gateway.py"


def _git(root: Path, *arguments: str) -> str:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    completed = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


class LandingClassifyTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.find_spec("scripts.export_public")
        self.assertIsNotNone(spec, "scripts.export_public is missing")
        return importlib.import_module("scripts.export_public")

    def policy(self):
        return self.module().ExportPolicy.load(POLICY_PATH)

    def test_unclassified_added_path_is_refused_at_landing(self) -> None:
        """A landing that only runs tests and the manifest cannot see this class."""

        module = self.module()
        policy = self.policy()

        module.refuse_unclassified_added_paths(GATEWAY_PATHS, policy)

        with self.assertRaises(module.ExportRefusal) as caught:
            module.refuse_unclassified_added_paths((UNCLASSIFIED_ADDED,), policy)

        self.assertEqual("public_export_unclassified", caught.exception.code)
        self.assertEqual(
            [UNCLASSIFIED_ADDED], caught.exception.evidence["unresolved_paths"]
        )
        self.assertEqual(
            module.Classification("unresolved", "unclassified_new_path"),
            module.classify_path(UNCLASSIFIED_ADDED, set(), policy),
        )

    def test_empty_added_set_is_a_clean_landing(self) -> None:
        module = self.module()
        self.assertEqual(
            (),
            module.refuse_unclassified_added_paths((), self.policy()),
        )

    def test_added_paths_from_diff_are_adds_only(self) -> None:
        """Modifications are already in the tree; the hole is a path that did not exist."""

        module = self.module()
        with tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            _git(root, "init", "-q", "--initial-branch=main")
            _git(root, "config", "user.name", "fixture")
            _git(root, "config", "user.email", "fixture@example.invalid")
            (root / "kept.py").write_text("one\n", encoding="utf-8")
            _git(root, "add", "kept.py")
            _git(root, "commit", "-q", "-m", "base")
            base = _git(root, "rev-parse", "HEAD")
            (root / "kept.py").write_text("two\n", encoding="utf-8")
            (root / "added.py").write_text("new\n", encoding="utf-8")
            _git(root, "add", "kept.py", "added.py")
            _git(root, "commit", "-q", "-m", "change")

            self.assertEqual(
                ("added.py",),
                module.added_paths_from_diff(root, base),
            )

    def test_classify_added_cli_refuses_without_a_public_tree(self) -> None:
        """Milliseconds, no staged export tree: the classifier alone is the landing gate."""

        module = self.module()
        with tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp") as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            _git(root, "init", "-q", "--initial-branch=main")
            _git(root, "config", "user.name", "fixture")
            _git(root, "config", "user.email", "fixture@example.invalid")
            (root / "floati").mkdir()
            (root / "floati" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            _git(root, "add", "floati/core.py")
            _git(root, "commit", "-q", "-m", "base")
            base = _git(root, "rev-parse", "HEAD")
            (root / "tools").mkdir()
            (root / "tools" / "codex").mkdir()
            (root / "tools" / "codex" / "future-gateway.py").write_text(
                "pass\n", encoding="utf-8"
            )
            _git(root, "add", "tools/codex/future-gateway.py")
            _git(root, "commit", "-q", "-m", "unclassified add")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "classify-added",
                    "--source-root",
                    str(root),
                    "--base",
                    base,
                    "--policy",
                    str(POLICY_PATH),
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(20, completed.returncode)
            artifact = json.loads(completed.stdout)
            self.assertEqual("refused", artifact["status"])
            self.assertEqual("export-public", artifact["command"])
            self.assertEqual(
                "public_export_unclassified", artifact["evidence"]["code"]
            )
            self.assertEqual(
                [UNCLASSIFIED_ADDED], artifact["evidence"]["unresolved_paths"]
            )

    def test_this_tree_added_vs_origin_main_has_no_unclassified_paths(self) -> None:
        """Landing rehearsal on this checkout: every added path must already classify."""

        module = self.module()
        added = module.added_paths_from_diff(REPOSITORY_ROOT, "origin/main")
        classified = module.refuse_unclassified_added_paths(added, self.policy())
        self.assertEqual(len(added), len(classified))
        self.assertTrue(
            all(row.disposition in {"include", "exclude"} for row in classified)
        )


if __name__ == "__main__":
    unittest.main()
