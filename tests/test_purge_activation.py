from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from floati import purge
from floati.cli import _parser, main
from floati.helptext import help_for
from floati.manifest import verify_manifest


REPOSITORY_ROOT = Path(__file__).parents[1]


class PurgeActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "preserved-root"
        self.root.mkdir()
        (self.root / "receipt.json").write_text("{}\n", encoding="utf-8")
        self.trash = self.base / "Trash"
        self.trash.mkdir()

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, object], str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(list(arguments))
        rendered = stdout.getvalue() or stderr.getvalue()
        return status, json.loads(rendered), stdout.getvalue(), stderr.getvalue()

    def test_purge_is_registered_once_at_the_canonical_cli_seam(self) -> None:
        """Catches activation omission or duplicate shared-seam registration."""

        with patch("floati.purge.register_cli", wraps=purge.register_cli) as register:
            parser = _parser()

        parsed = parser.parse_args(
            ["purge", "--root", str(self.root), "--dry-run"]
        )
        self.assertEqual(1, register.call_count)
        self.assertEqual("purge", parsed.command)
        self.assertEqual([str(self.root)], parsed.roots)
        self.assertTrue(parsed.dry_run)
        self.assertTrue(callable(parsed.handler))

    def test_real_cli_dry_run_projects_without_moving_the_root(self) -> None:
        """Catches a parser-only activation that does not reach the ruled writer."""

        before = (self.root / "receipt.json").read_bytes()
        with patch("floati.purge._trash_dir", return_value=self.trash):
            status, artifact, stdout, stderr = self.run_cli(
                "purge", "--root", str(self.root), "--dry-run"
            )

        self.assertEqual(0, status, artifact)
        self.assertEqual("ok", artifact["status"])
        self.assertTrue(artifact["evidence"]["dry_run"])
        self.assertTrue(artifact["evidence"]["trash_only"])
        self.assertEqual(before, (self.root / "receipt.json").read_bytes())
        self.assertTrue(self.root.is_dir())
        self.assertEqual([], list(self.trash.iterdir()))
        self.assertNotEqual("", stdout)
        self.assertEqual("", stderr)

    def test_static_help_uses_the_restamped_trash_only_contract(self) -> None:
        """Catches argparse fallback, stale DRAFT copy, or root-help omission."""

        page = help_for(("purge", "--help"))
        self.assertIsNotNone(page)
        assert page is not None
        self.assertTrue(page.startswith("NAME\n"))
        self.assertIn("floati purge", page)
        self.assertIn("account Trash; never deletes", page)
        self.assertIn("one absolute preserved root to move; repeat for more", page)
        self.assertIn("list every file that would move; move nothing", page)
        self.assertNotIn("DRAFT -", page)

        root_help = help_for(("--help",))
        self.assertIsNotNone(root_help)
        assert root_help is not None
        self.assertIn("purge", root_help)

    def test_bundle_manifest_remains_exact_after_activation(self) -> None:
        """Catches wiring or static copy omitted from the shipped bundle receipt."""

        self.assertEqual([], verify_manifest(REPOSITORY_ROOT))


if __name__ == "__main__":
    unittest.main()
