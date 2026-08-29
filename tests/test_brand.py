from __future__ import annotations

import io
import json
import hashlib
import re
import unittest
from unittest.mock import patch
from pathlib import Path


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", value)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class FloatiBrandTests(unittest.TestCase):
    def test_buoy_mark_is_the_exact_six_line_never_unlit_ruling(self) -> None:
        from floati.brand import BUOY_MARK

        self.assertEqual(
            "      ⊙\n"
            "      │\n"
            "     ╱ ╲\n"
            "    ╱───╲\n"
            "   ╱     ╲\n"
            " ~~~~~~~~~~~",
            BUOY_MARK,
        )
        self.assertEqual(6, len(BUOY_MARK.splitlines()))
        self.assertIn("⊙", BUOY_MARK)

    def test_buoy_color_is_advisory_and_keeps_the_band_dim(self) -> None:
        from floati.brand import BUOY_MARK, BUOY_ORANGE, HARBOR_SLATE, render_buoy_mark

        rendered = render_buoy_mark(color=True)

        self.assertEqual(BUOY_MARK, strip_ansi(rendered))
        self.assertIn(BUOY_ORANGE + "      ⊙", rendered)
        self.assertIn(HARBOR_SLATE + "───", rendered)
        self.assertIn(HARBOR_SLATE + " ~~~~~~~~~~~", rendered)

    def test_install_marks_only_the_interactive_success_receipt(self) -> None:
        from floati.cli import _emit

        tty = _Tty()
        with patch("sys.stdout", tty):
            _emit("install", "ok", {"status": "installed"}, 0)
        plain = io.StringIO()
        with patch("sys.stdout", plain):
            _emit("install", "ok", {"status": "installed"}, 0)

        self.assertIn("⊙", tty.getvalue())
        self.assertLess(tty.getvalue().index("⊙"), tty.getvalue().index('{"artifact_version"'))
        self.assertNotIn("⊙", plain.getvalue())
        self.assertEqual("installed", json.loads(plain.getvalue())["evidence"]["status"])

    def test_selftest_marks_only_the_interactive_bundle_verified_line(self) -> None:
        from floati.selftest import emit_verified

        tty = _Tty()
        emit_verified(tty)
        plain = io.StringIO()
        emit_verified(plain)

        self.assertIn("⊙", tty.getvalue())
        self.assertIn('"status":"bundle_verified"', tty.getvalue())
        self.assertNotIn("⊙", plain.getvalue())
        self.assertEqual("bundle_verified", json.loads(plain.getvalue())["status"])

    def test_readme_uses_the_pinned_byte_identical_buoy_master(self) -> None:
        asset = Path("docs/assets/floati-icon.svg")
        readme = Path("README.md").read_text(encoding="utf-8")

        self.assertTrue(asset.is_file(), "the accepted buoy master must be committed")
        self.assertEqual(
            "99d89c3e252e6970979f902a5abe8790ff57ca91266bfe1a28a8cc6cbf13adeb",
            hashlib.sha256(asset.read_bytes()).hexdigest(),
        )
        self.assertIn("docs/assets/floati-icon.svg", readme)
