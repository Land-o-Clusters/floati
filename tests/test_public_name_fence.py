from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "public_name_fence.py"
HOME_PREFIX = bytes.fromhex("2f55736572732f").decode("ascii")
PRIVATE_TMP_PREFIX = bytes.fromhex("2f707269766174652f746d70").decode("ascii")
OWNER_USERNAME = bytes.fromhex("63687269736d656e656e64657a").decode("ascii")
VERIFICATION_SEAT = bytes.fromhex("67726f6b").decode("ascii")
EXPLICIT_VERIFICATION_SEAT = bytes.fromhex(
    "67726f6b2d7468652d73656174"
).decode("ascii")
ARCHITECT_SEAT = bytes.fromhex("6661626c65").decode("ascii")
BUILD_SEAT_PREFIX = bytes.fromhex("6c616e652d").decode("ascii")
BUILD_SEAT = bytes.fromhex("616c69636537").decode("ascii")
SHORT_BUILD_SEAT = bytes.fromhex("736f6c").decode("ascii")


class PublicNameFenceTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.find_spec("scripts.public_name_fence")
        self.assertIsNotNone(spec, "scripts.public_name_fence is missing")
        return importlib.import_module("scripts.public_name_fence")

    def test_clean_binary_tree_has_no_findings(self) -> None:
        """Returning a default finding for arbitrary binary bytes is rejected."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "clean.bin").write_bytes(bytes(range(256)))

            self.assertEqual([], module.scan_tree(root))

    def test_public_product_source_has_no_private_seat_vocabulary(self) -> None:
        """A private seat identifier reaching product code or tests is rejected."""

        module = self.module()
        policy = json.loads(
            (REPOSITORY_ROOT / ".github" / "public-export-policy.v0.json").read_text(
                encoding="utf-8"
            )
        )
        private_only = set(policy["private_only_paths"])
        private_prefixes = tuple(policy["class3_prefixes"])
        public_prefixes = (
            ".github/",
            "bundle/",
            "floati/",
            "roles/",
            "schemas/",
            "scripts/",
            "tests/",
            "trust/",
        )
        findings = [
            finding
            for finding in module.scan_tree(REPOSITORY_ROOT)
            if finding["code"] in {"seat_name", "seat_name_path"}
            and finding["path"] not in private_only
            and not str(finding["path"]).startswith(private_prefixes)
            and (
                finding["path"] == "README.md"
                or str(finding["path"]).startswith(public_prefixes)
            )
        ]

        self.assertEqual([], findings)

    def test_home_prefix_is_detected_in_each_supported_encoding(self) -> None:
        """Dropping an encoding from the byte fence leaves that encoded path publishable."""

        module = self.module()
        encodings = ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, encoding in enumerate(encodings):
                (root / f"home-{index}.bin").write_bytes(
                    f"{HOME_PREFIX}example/project".encode(encoding)
                )

            self.assertEqual(
                [
                    {"code": "operator_home_path", "path": f"home-{index}.bin"}
                    for index in range(len(encodings))
                ],
                module.scan_tree(root),
            )

    def test_private_tmp_prefix_is_detected_in_utf32_big_endian(self) -> None:
        """Treating a non-UTF-8 file as unscannable is rejected."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "capture.bin").write_bytes(
                f"{PRIVATE_TMP_PREFIX}/capture".encode("utf-32-be")
            )

            self.assertEqual(
                [{"code": "private_tmp_path", "path": "capture.bin"}],
                module.scan_tree(root),
            )

    def test_owner_username_is_detected_in_each_supported_encoding(self) -> None:
        """Publishing the owner username in any file or encoding is rejected."""

        module = self.module()
        encodings = ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, encoding in enumerate(encodings):
                (root / f"owner-{index}.bin").write_bytes(
                    f"prefix-{OWNER_USERNAME}-suffix".encode(encoding)
                )

            self.assertEqual(
                [
                    {"code": "owner_username", "path": f"owner-{index}.bin"}
                    for index in range(len(encodings))
                ],
                module.scan_tree(root),
            )

    def test_seat_names_are_case_insensitive_word_bounded_and_encoding_agnostic(self) -> None:
        """Publishing an attribution while sparing product and embedded words is rejected."""

        module = self.module()
        product_hyphen = VERIFICATION_SEAT + "-build"
        product_underscore = VERIFICATION_SEAT + "_build"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                "architect.bin": ARCHITECT_SEAT.upper().encode("utf-8"),
                "build-prefix.bin": f"{BUILD_SEAT_PREFIX}floati".encode("utf-16-le"),
                "build-wildcard.bin": BUILD_SEAT.encode("utf-32-be"),
                "short-build.bin": SHORT_BUILD_SEAT.encode("utf-16-be"),
                "verification.bin": VERIFICATION_SEAT.encode("utf-32-le"),
                "verification-explicit.bin": EXPLICIT_VERIFICATION_SEAT.encode("utf-8"),
            }
            for name, data in fixtures.items():
                (root / name).write_bytes(data)
            (root / "allowed.txt").write_text(
                (
                    f"{product_hyphen} {product_underscore} console "
                    "verification lane-shaped build lane-shaped"
                ),
                encoding="utf-8",
            )
            (root / "binary-fragments.bin").write_bytes(b"S\xffO\xffL")
            (root / "binary-coincidence.bin").write_bytes(
                b"\xff" + SHORT_BUILD_SEAT.upper().encode("ascii") + b"\xff"
            )

            self.assertEqual(
                [
                    {"code": "seat_name", "line": 1, "path": name}
                    for name in sorted(fixtures)
                ],
                module.scan_tree(root),
            )

    def test_seat_names_are_exempt_only_on_declared_vendor_measurement_paths(self) -> None:
        """Guessing vendor meaning from token shape instead of the governed path is rejected."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                "docs/capability-matrix.v0.json": f'{{"harness":"{VERIFICATION_SEAT}"}}\n',
                "docs/capability-matrix.md": f"| harness | {VERIFICATION_SEAT} |\n",
                "docs/evidence/conformance/C1.md": f"measured {VERIFICATION_SEAT}\n",
                "docs/evidence/captures/demo.txt": f"captured {ARCHITECT_SEAT}\n",
            }
            for relative, content in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            (root / "docs" / "unsafe.md").write_text(
                f"assigned to {ARCHITECT_SEAT}\n", encoding="utf-8"
            )

            self.assertEqual(
                [{"code": "seat_name", "line": 1, "path": "docs/unsafe.md"}],
                module.scan_tree(root),
            )

    def test_vendor_product_sites_are_allowlisted_once_and_only_at_recorded_files(self) -> None:
        """A global vendor-token exemption or an unbounded site allowance is rejected."""

        module = self.module()
        product = bytes.fromhex("67726f6b").decode("ascii").title()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "docs/COPY-LEDGER.md": f"exact {product} session binding recorded.\n",
                "docs/assets/floati-multifleet-dark.svg": f"<text>{product}</text>\n",
                "docs/assets/floati-multifleet-light.svg": f"<text>{product}</text>\n",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            self.assertEqual(3, len(getattr(module, "SEAT_NAME_SITE_ALLOWLIST", ())))
            self.assertEqual([], module.scan_tree(root))

            copy_ledger = root / "docs" / "COPY-LEDGER.md"
            copy_ledger.write_text(files["docs/COPY-LEDGER.md"] * 2, encoding="utf-8")
            (root / "docs" / "unsafe.md").write_text(product + "\n", encoding="utf-8")
            self.assertEqual(
                [
                    {"code": "seat_name", "line": 2, "path": "docs/COPY-LEDGER.md"},
                    {"code": "seat_name", "line": 1, "path": "docs/unsafe.md"},
                ],
                module.scan_tree(root),
            )

    def test_vendor_research_allowance_is_pinned_to_exactly_twenty_one_lines(self) -> None:
        """A new bare vendor-token line cannot ride the governed research-file ticket."""

        module = self.module()
        product = VERIFICATION_SEAT.title()
        relative = "docs/research/regatta/grok-build-tui-mechanisms-dr.md"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            governed = "".join(f"{product} mechanism {index}\n" for index in range(21))
            path.write_text(governed, encoding="utf-8")

            self.assertEqual([], module.scan_tree(root))

            path.write_text(governed + f"new {product} claim\n", encoding="utf-8")
            self.assertEqual(
                [
                    {"code": "seat_name", "line": line, "path": relative}
                    for line in range(1, 23)
                ],
                module.scan_tree(root),
            )

    def test_signed_historical_records_are_exempt_by_path_rule_only(self) -> None:
        """The historical class is derived from its directory and dated basename."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                "docs/rulings/owner-record.md": f"signed by {ARCHITECT_SEAT}\n",
                "docs/design/brief-2026-08-30.md": f"assigned to {BUILD_SEAT}\n",
                "docs/design/nested/brief-2026-08-30-amended.md": (
                    f"assigned to {SHORT_BUILD_SEAT}\n"
                ),
                "docs/design/brief-2026-08.md": f"assigned to {BUILD_SEAT}\n",
                "docs/other/brief-2026-08-30.md": f"assigned to {BUILD_SEAT}\n",
            }
            for relative, content in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            self.assertEqual(
                [
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/design/brief-2026-08.md",
                    },
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/other/brief-2026-08-30.md",
                    },
                ],
                module.scan_tree(root),
            )

    def test_readme_vendor_product_allowances_pin_each_generated_line_count(self) -> None:
        """README product cells allow their measured counts, not arbitrary vendor prose."""

        module = self.module()
        self.assertEqual(
            [1, 1],
            [row[3] for row in module.SEAT_NAME_CONTENT_LINE_COUNT_ALLOWLIST],
        )
        product = VERIFICATION_SEAT
        relative = "README.md"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lines = ["\n"] * 83
            lines[66] = f"| {product} | {product} cli | installed {product} binary |\n"
            lines[82] = f"| {product} / desktop | n/a |\n"
            path = root / relative
            path.write_text("".join(lines), encoding="utf-8")

            self.assertEqual([], module.scan_tree(root))
            self.assertEqual(
                {
                    "readme_desktop_product_lines": 1,
                    "readme_harness_product_lines": 1,
                    "research_product_lines": 0,
                    "vendor_product_sites": 0,
                },
                module.allowlist_measurements(root),
            )

            path.write_text("new generated row\n" + "".join(lines), encoding="utf-8")
            self.assertEqual([], module.scan_tree(root))

            lines[67] = lines[66]
            path.write_text("".join(lines), encoding="utf-8")
            self.assertEqual(
                [
                    {"code": "seat_name", "line": 67, "path": relative},
                    {"code": "seat_name", "line": 68, "path": relative},
                ],
                module.scan_tree(root),
            )

    def test_seat_name_in_path_is_refused_without_renaming(self) -> None:
        """Letting a projection invent a role-substituted filename is rejected."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "floati" / f"{ARCHITECT_SEAT}-fixture.py"
            path.parent.mkdir(parents=True)
            path.write_text("VALUE = 1\n", encoding="utf-8")

            self.assertEqual(
                [{"code": "seat_name_path", "path": path.relative_to(root).as_posix()}],
                module.scan_tree(root),
            )

    def test_existing_private_project_fence_is_included(self) -> None:
        """Removing the existing source scrub from the public fence is rejected."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "foreign.bin").write_bytes(
                bytes.fromhex("5369676e616c4372616674")
            )

            self.assertEqual(
                [{"code": "private_project_name", "path": "foreign.bin"}],
                module.scan_tree(root),
            )

    def test_symlink_is_a_finding_and_is_not_followed(self) -> None:
        """Following or silently skipping a projected symlink is rejected."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root.parent / f"{root.name}-outside"
            outside.write_text(f"{HOME_PREFIX}example/outside\n", encoding="utf-8")
            try:
                (root / "linked").symlink_to(outside)

                self.assertEqual(
                    [{"code": "symlink_path", "path": "linked"}],
                    module.scan_tree(root),
                )
            finally:
                outside.unlink(missing_ok=True)

    def test_findings_are_sorted_by_path_then_code(self) -> None:
        """Filesystem traversal order leaking into evidence is rejected."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "z.bin").write_bytes(
                f"{HOME_PREFIX}example {PRIVATE_TMP_PREFIX}/value".encode("ascii")
            )
            (root / "a.bin").write_bytes(f"{PRIVATE_TMP_PREFIX}/value".encode("ascii"))

            self.assertEqual(
                [
                    {"code": "private_tmp_path", "path": "a.bin"},
                    {"code": "operator_home_path", "path": "z.bin"},
                    {"code": "private_tmp_path", "path": "z.bin"},
                ],
                module.scan_tree(root),
            )

    def test_cli_emits_one_success_artifact(self) -> None:
        """Printing diagnostics around the machine artifact is rejected."""

        self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "clean.txt").write_text("public\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(0, completed.returncode)
        self.assertEqual("", completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(1, len(lines))
        artifact = json.loads(lines[0])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual([], artifact["evidence"]["findings"])

    def test_cli_refuses_a_finding_with_one_artifact(self) -> None:
        """Reporting a hit while returning success is rejected."""

        self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "private.txt").write_bytes(f"{HOME_PREFIX}example".encode("ascii"))
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), str(root)],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(20, completed.returncode)
        self.assertEqual("", completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(1, len(lines))
        artifact = json.loads(lines[0])
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("public_name_fence_failed", artifact["evidence"]["code"])
        self.assertEqual(
            [{"code": "operator_home_path", "path": "private.txt"}],
            artifact["evidence"]["findings"],
        )

    def test_git_checkout_scans_only_the_tracked_public_inventory(self) -> None:
        """Letting unrelated untracked workspace bytes enter release evidence is rejected."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["/usr/bin/git", "init", "-q"], cwd=root, check=True)
            (root / "tracked.txt").write_text("public\n", encoding="utf-8")
            subprocess.run(["/usr/bin/git", "add", "tracked.txt"], cwd=root, check=True)
            (root / "untracked.txt").write_text(
                f"{HOME_PREFIX}example/private\n", encoding="utf-8"
            )

            self.assertEqual([], module.scan_tree(root))

    def test_fence_sources_do_not_carry_the_bytes_they_forbid(self) -> None:
        """Embedding a forbidden path in the hunter makes every public run fail."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scanner.py").write_bytes(SCRIPT.read_bytes())
            (root / "scanner_test.py").write_bytes(Path(__file__).read_bytes())

            self.assertEqual([], module.scan_tree(root))


if __name__ == "__main__":
    unittest.main()
