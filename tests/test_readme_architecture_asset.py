from __future__ import annotations

import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"
ARCHITECTURE_ASSETS = (
    "docs/assets/floati-architecture-dark.svg",
    "docs/assets/floati-architecture-light.svg",
)


class ReadmeArchitectureAssetTests(unittest.TestCase):
    def test_readme_architecture_picture_resolves_to_accessible_safe_svg_twins(
        self,
    ) -> None:
        """Catches a missing, unsafe, or inaccessible README architecture image."""

        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        for relative in ARCHITECTURE_ASSETS:
            self.assertIn(relative, readme)

        architecture_image = re.search(
            r'<img\s+src="docs/assets/floati-architecture-light\.svg"\s+'
            r'alt="([^"]+)"\s+width="1400">',
            readme,
        )
        self.assertIsNotNone(architecture_image)
        self.assertTrue(architecture_image.group(1).strip())

        forbidden_tags = {
            f"{SVG_NAMESPACE}animate",
            f"{SVG_NAMESPACE}animateMotion",
            f"{SVG_NAMESPACE}animateTransform",
            f"{SVG_NAMESPACE}image",
            f"{SVG_NAMESPACE}script",
        }
        for relative in ARCHITECTURE_ASSETS:
            with self.subTest(relative=relative):
                root = ET.parse(REPOSITORY_ROOT / relative).getroot()
                self.assertEqual(f"{SVG_NAMESPACE}svg", root.tag)
                self.assertEqual("0 0 1400 640", root.attrib.get("viewBox"))
                self.assertTrue(root.findtext(f"{SVG_NAMESPACE}title", "").strip())
                self.assertTrue(root.findtext(f"{SVG_NAMESPACE}desc", "").strip())
                self.assertTrue(forbidden_tags.isdisjoint(element.tag for element in root.iter()))
                for element in root.iter():
                    for attribute, value in element.attrib.items():
                        if attribute.endswith("href"):
                            self.assertFalse(value.startswith(("http:", "https:", "//")))


if __name__ == "__main__":
    unittest.main()
