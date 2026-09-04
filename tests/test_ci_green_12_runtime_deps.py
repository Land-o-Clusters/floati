"""Fence: selftest runtime deps stay unpinned names installed before the suite.

CI-GREEN-12 reads the orphan ``codex/ci-runtime-dependencies`` ``f57381ee``.
That branch added pinned ``requirements-selftest.txt`` (jsonschema==4.25.1,
Pillow==11.3.0) and rewired both public workflows to
``pip install --requirement requirements-selftest.txt``. Main already
installs unpinned ``pillow jsonschema`` plus minisign before
``python3 -m floati.selftest``. A pin file would fight that step.

This module lands the orphan's *property* against main's install step:
the two public workflow files install unpinned pillow and jsonschema
before the suite, and the pinned requirements file does not exist.
``.github/workflows`` is not edited here.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UNPINNED_INSTALL = (
    "python3 -m pip install --disable-pip-version-check --quiet pillow jsonschema"
)
SUITE = "python3 -m floati.selftest"
PUBLIC_WORKFLOWS = (
    ".github/workflows/selftest.yml",
    ".github/workflows/release-gate.yml",
)
PINNED_REQUIREMENTS = "requirements-selftest.txt"
ORPHAN_INSTALL = "python3 -m pip install --requirement requirements-selftest.txt"


def pinned_requirements_findings(root: Path) -> list[str]:
    """Return paths that still carry the orphan's pin file or install line."""

    findings: list[str] = []
    pin = root / PINNED_REQUIREMENTS
    if pin.is_file():
        findings.append(PINNED_REQUIREMENTS)
    for relative in PUBLIC_WORKFLOWS:
        path = root / relative
        if not path.is_file():
            findings.append(f"{relative}::<missing>")
            continue
        text = path.read_text(encoding="utf-8")
        if ORPHAN_INSTALL in text:
            findings.append(f"{relative}::requirement-install")
        if PINNED_REQUIREMENTS in text:
            findings.append(f"{relative}::pin-file-named")
    return findings


class RuntimeDependencyFenceTests(unittest.TestCase):
    def test_orphan_pin_file_is_absent(self) -> None:
        self.assertEqual([], pinned_requirements_findings(REPOSITORY_ROOT))
        self.assertFalse((REPOSITORY_ROOT / PINNED_REQUIREMENTS).exists())

    def test_public_workflows_install_unpinned_pillow_jsonschema_before_selftest(
        self,
    ) -> None:
        for relative in PUBLIC_WORKFLOWS:
            with self.subTest(workflow=relative):
                text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(UNPINNED_INSTALL, text)
                self.assertIn(SUITE, text)
                self.assertLess(text.index(UNPINNED_INSTALL), text.index(SUITE))
                self.assertNotIn("pillow==", text.lower())
                self.assertNotIn("jsonschema==", text.lower())

    def test_control_orphan_pin_file_and_requirement_install_are_findings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / PINNED_REQUIREMENTS).write_text(
                "jsonschema==4.25.1\nPillow==11.3.0\n",
                encoding="utf-8",
            )
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "selftest.yml").write_text(
                f"run: {ORPHAN_INSTALL}\nrun: {SUITE}\n",
                encoding="utf-8",
            )
            (workflows / "release-gate.yml").write_text(
                f"{UNPINNED_INSTALL}\n{SUITE}\n",
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    PINNED_REQUIREMENTS,
                    ".github/workflows/selftest.yml::requirement-install",
                    ".github/workflows/selftest.yml::pin-file-named",
                ],
                pinned_requirements_findings(root),
            )

    def test_control_unpinned_install_before_suite_is_not_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            body = f"{UNPINNED_INSTALL}\n{SUITE}\n"
            (workflows / "selftest.yml").write_text(body, encoding="utf-8")
            (workflows / "release-gate.yml").write_text(body, encoding="utf-8")
            self.assertEqual([], pinned_requirements_findings(root))


if __name__ == "__main__":
    unittest.main()
