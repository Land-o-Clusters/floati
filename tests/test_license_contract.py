from __future__ import annotations

import hashlib
import unittest
from pathlib import Path


AGPL_3_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
APACHE_2_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


class LicenseContractTests(unittest.TestCase):
    def test_root_is_agpl_3(self) -> None:
        body = Path("LICENSE").read_bytes()
        text = body.decode("utf-8")

        self.assertEqual(AGPL_3_SHA256, hashlib.sha256(body).hexdigest())
        self.assertTrue(text.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE\n"))
        self.assertIn("Version 3, 19 November 2007", text)
        self.assertIn("GNU Affero General Public License", text)

    def test_nested_spec_surfaces_are_apache_2(self) -> None:
        for relative in (
            "schemas/LICENSE",
            "bundle/c7.1/LICENSE",
            "bundle/c7.2/LICENSE",
        ):
            with self.subTest(relative=relative):
                body = Path(relative).read_bytes()
                text = body.decode("utf-8")
                self.assertEqual(APACHE_2_SHA256, hashlib.sha256(body).hexdigest())
                self.assertTrue(text.lstrip().startswith("Apache License\n"))
                self.assertIn("Version 2.0, January 2004", text)
                self.assertIn("http://www.apache.org/licenses/", text)

    def test_pending_license_is_removed(self) -> None:
        self.assertFalse(Path("LICENSE-PENDING.md").exists())

    def test_contributing_requires_dco_signoff(self) -> None:
        text = Path("CONTRIBUTING.md").read_text(encoding="utf-8")

        self.assertIn("Signed-off-by:", text)
        self.assertIn("https://developercertificate.org/", text)

    def test_readme_states_exact_split(self) -> None:
        text = Path("README.md").read_text(encoding="utf-8")

        self.assertIn(
            "Product code is AGPL-3.0; the interchange schemas and bundle\n"
            "specifications are Apache-2.0.",
            text,
        )


if __name__ == "__main__":
    unittest.main()
