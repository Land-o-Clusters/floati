"""WHICH-1: product code never locates an executable with shutil.which."""

from __future__ import annotations

import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_NEEDLE = "shutil.which"


def _product_hits() -> list[str]:
    hits: list[str] = []
    for root_name in ("floati", "scripts"):
        for path in sorted((REPOSITORY_ROOT / root_name).rglob("*")):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if _NEEDLE in text:
                hits.append(path.relative_to(REPOSITORY_ROOT).as_posix())
    return hits


class Which1ExecutableProvenanceTests(unittest.TestCase):
    def test_floati_and_scripts_contain_zero_shutil_which(self) -> None:
        self.assertEqual([], _product_hits())


if __name__ == "__main__":
    unittest.main()
