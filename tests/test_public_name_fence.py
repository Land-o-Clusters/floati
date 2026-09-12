from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.export_inventory import export_include_set, materialise_exposed_tree
from tests.private_artifacts import require_private_artifact


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "public_name_fence.py"
POLICY_RELATIVE = ".github/public-export-policy.v0.json"
MEASURED_ARCHITECT_RESIDUE_PATHS = (
    "docs/evidence/DEMO-UAT-CAPTURE-GIF-CANDIDATES.md",
    "docs/evidence/DEMO-UAT-CAPTURE-GIF-SET.md",
    "docs/evidence/DEMO-UAT-CORPUS-FOUNDATIONS.md",
    "docs/evidence/FLEET-OPS-ISSUE-1-REGISTRY-RETIREMENT-VERB.md",
    "docs/evidence/FLEET-OPS-ISSUE-2-DOCTOR-BUS-ONLY-PROFILE.md",
    "docs/evidence/WEEKEND-TRAIN-CAR-3-DELIVERY-HEALTH-DOCTOR-PROBE.md",
    "docs/evidence/wave2-r1-headless-invocations-2026-08-27.md",
    "docs/evidence/wave2-r2-pi-deadline-classification-2026-08-27.md",
    "docs/evidence/wave2-r3-herdr-loopback-client-2026-08-27.md",
)
HOME_PREFIX = bytes.fromhex("2f55736572732f").decode("ascii")
PRIVATE_TMP_PREFIX = bytes.fromhex("2f707269766174652f746d70").decode("ascii")
PRIVATE_VAR_TMP_PREFIX = bytes.fromhex("2f707269766174652f7661722f746d70").decode("ascii")
VAR_FOLDERS_PREFIX = bytes.fromhex("2f7661722f666f6c64657273").decode("ascii")
TMP_PREFIX = bytes.fromhex("2f746d70").decode("ascii")
OWNER_USERNAME = bytes.fromhex("63687269736d656e656e64657a").decode("ascii")
VERIFICATION_SEAT = bytes.fromhex("67726f6b").decode("ascii")
EXPLICIT_VERIFICATION_SEAT = bytes.fromhex(
    "67726f6b2d7468652d73656174"
).decode("ascii")
ARCHITECT_SEAT = bytes.fromhex("6661626c65").decode("ascii")
BUILD_SEAT_PREFIX = bytes.fromhex("6c616e652d").decode("ascii")
BUILD_SEAT = bytes.fromhex("616c69636537").decode("ascii")
SHORT_BUILD_SEAT = bytes.fromhex("736f6c").decode("ascii")
CITY_SEAT = bytes.fromhex("616c696365").decode("ascii")
PUDDLE_SEAT = bytes.fromhex("707564646c65").decode("ascii")
# FL-4: the derived seat population — every seat id the fleet bus
# registry holds (nodes/ basenames, acks and deliveries basenames, the
# codex-wait workspaces file), derived 2026-09-12, hex-carried like the
# rest of this vocabulary. The fence module derives its projections from
# the same reviewed map this population must stay inside.
DERIVED_SEAT_POPULATION_HEX = (
    "616c696365",
    "616c6963652d63697479",
    "616c6963652d6e6563726f",
    "666c6f6174692d6f62736572766572",
    "666c6f6174692d7769746e657373",
    "6671362d636f646578",
    "6671372d6f70656e636f6465",
    "6671372d73656174",
    "6671372d7365617432",
    "67726f6b",
    "6c616e652d617070",
    "6c616e652d666c6f617469",
    "6c616e652d707564646c65",
    "6c616e652d707564646c652d63726f7373636f6e6e656374696f6e",
    "6c616e652d707564646c652d66726f6e74696572",
    "6c616e652d707564646c652d6d656e75626172",
    "6c616e652d707564646c652d72656c696566",
    "6c616e652d736c6970776179",
    "6c616e652d736f6c",
    "6c616e652d7a636f6465",
    "6c616e652d7a636f64652d32",
    "6c616e652d7a636f64652d33",
    "6c616e652d7a636f64652d34",
    "707564646c652d666c6f6174692d617263686974656374",
)


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
        """An exact seat id reaching an export-exposed byte surface is rejected."""

        require_private_artifact(self, POLICY_RELATIVE)
        module = self.module()
        population = set(export_include_set())
        with tempfile.TemporaryDirectory() as temporary:
            exposed = Path(temporary) / "exposed"
            materialise_exposed_tree(exposed)
            findings = [
                finding
                for finding in module.scan_tree(exposed)
                if finding["code"] in {"seat_name", "seat_name_path"}
                and finding["path"] in population
            ]

        self.assertEqual([], findings)

    def test_measured_architect_residue_paths_are_not_export_exposed(self) -> None:
        """The nine measured docs/evidence paths must not carry architect ids after adaptation."""

        require_private_artifact(self, POLICY_RELATIVE)
        module = self.module()
        population = set(MEASURED_ARCHITECT_RESIDUE_PATHS)
        self.assertLessEqual(population, set(export_include_set()))
        with tempfile.TemporaryDirectory() as temporary:
            exposed = Path(temporary) / "exposed"
            materialise_exposed_tree(exposed)
            findings = sorted(
                {
                    finding["path"]
                    for finding in module.scan_tree(exposed)
                    if finding["code"] in {"seat_name", "seat_name_path"}
                    and finding["path"] in population
                }
            )

        self.assertEqual([], findings)

    def test_planted_seat_id_in_export_included_evidence_is_refused(self) -> None:
        """A seat id planted on an export-included evidence path must refuse the fence."""

        require_private_artifact(self, POLICY_RELATIVE)
        module = self.module()
        relative = "docs/evidence/conformance/name-fence-2-control-fixture.md"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"assigned to {ARCHITECT_SEAT}\n", encoding="utf-8")

            self.assertEqual(
                [{"code": "seat_name", "line": 1, "path": relative}],
                module.scan_tree(root),
            )

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

    def test_every_governed_temp_prefix_is_detected_in_utf32_big_endian(self) -> None:
        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = (
                ("private-tmp.bin", "private_tmp_path", PRIVATE_TMP_PREFIX),
                ("private-var-tmp.bin", "private_var_tmp_path", PRIVATE_VAR_TMP_PREFIX),
                ("var-folders.bin", "var_folders_path", VAR_FOLDERS_PREFIX),
                ("tmp.bin", "tmp_path", TMP_PREFIX),
            )
            for name, _code, prefix in fixtures:
                (root / name).write_bytes(f"{prefix}/capture".encode("utf-32-be"))

            self.assertEqual(
                sorted(
                    ({"code": code, "path": name} for name, code, _prefix in fixtures),
                    key=lambda finding: (finding["path"], finding["code"]),
                ),
                module.scan_tree(root),
            )

    def test_identity_fence_source_does_not_embed_governed_temp_prefixes(self) -> None:
        source = (REPOSITORY_ROOT / "floati" / "identity_fence.py").read_bytes()
        for prefix in (
            PRIVATE_TMP_PREFIX,
            PRIVATE_VAR_TMP_PREFIX,
            VAR_FOLDERS_PREFIX,
            TMP_PREFIX,
        ):
            with self.subTest(prefix=prefix):
                self.assertNotIn(prefix.encode("ascii"), source)

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
        """Exact ruled ids are forbidden while products and prefix fixtures remain safe."""

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
                    for name in (
                        "architect.bin",
                        "build-prefix.bin",
                        "verification-explicit.bin",
                    )
                ],
                module.scan_tree(root),
            )

    def test_suffixed_seat_ids_are_caught_while_word_continuations_stay_safe(self) -> None:
        """NAME-FENCE-2 Am.1: a numeric suffix cannot launder a seat id.

        The trailing guard refused any continuation, so ``<seat>-2`` — a
        real numbered lane seat — was invisible while the vocabulary held
        only the base id: tests/test_wake_notice_1.py carried one three
        times and the fence returned []. Am.2 then widened the guards to
        treat joins as components, so the seat's word continuation is
        caught too; the product compound of the verification seat stays
        exempt (covered beside the encoding-agnostic fixtures).
        """

        module = self.module()
        numbered_seat = ARCHITECT_SEAT + "-2"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "numbered.bin").write_text(numbered_seat, encoding="utf-8")
            (root / "word-continuation.bin").write_text(
                ARCHITECT_SEAT + "-build", encoding="utf-8"
            )

            self.assertEqual(
                [
                    {"code": "seat_name", "line": 1, "path": "numbered.bin"},
                    {"code": "seat_name", "line": 1, "path": "word-continuation.bin"},
                ],
                module.scan_tree(root),
            )

    def test_seat_ids_are_caught_as_compound_components(self) -> None:
        """NAME-FENCE-2 Am.2: joining cannot launder a seat id.

        Hyphen- and underscore-joined compounds carried seat vocabulary
        past the boundary: a relief-lane compound, a fleet-city compound,
        an underscore join, and a matrix-audit label were all no-match at
        Am.1's tip and all present in the live projection. A seat id is
        now caught as any joined component of a longer token, while the
        product compound of the verification seat stays exempt.
        """

        module = self.module()
        compounds = {
            "compound-middle.bin": f"{PUDDLE_SEAT}-{CITY_SEAT}-city",
            "compound-prefix.bin": BUILD_SEAT_PREFIX + PUDDLE_SEAT + "-relief",
            "compound-suffix.bin": f"matrix-audit-{ARCHITECT_SEAT}-x",
            "compound-underscore.bin": f"{CITY_SEAT}_city",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, text in compounds.items():
                (root / name).write_text(text, encoding="utf-8")
            (root / "product-compound.bin").write_text(
                VERIFICATION_SEAT + "-build", encoding="utf-8"
            )

            self.assertEqual(
                [
                    {"code": "seat_name", "line": 1, "path": name}
                    for name in sorted(compounds)
                ],
                module.scan_tree(root),
            )

    def test_record_class_paths_are_checked_for_exact_ids_while_grok_stays_product_prose(self) -> None:
        """Historical paths cannot exempt exact ids, and ambiguous product prose stays safe."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exact_build_seat = f"{BUILD_SEAT_PREFIX}floati"
            fixtures = {
                "docs/capability-matrix.v0.json": f'{{"seat":"{exact_build_seat}"}}\n',
                "docs/capability-matrix.md": f"| architect | {ARCHITECT_SEAT} |\n",
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
                [
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/capability-matrix.md",
                    },
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/capability-matrix.v0.json",
                    },
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/evidence/captures/demo.txt",
                    },
                    {"code": "seat_name", "line": 1, "path": "docs/unsafe.md"},
                ],
                module.scan_tree(root),
            )

    def test_escaped_json_seat_names_are_checked_semantically(self) -> None:
        """JSON escapes cannot hide exact ids from the public-name fence."""

        module = self.module()
        escaped = "".join(f"\\u{ord(character):04x}" for character in ARCHITECT_SEAT)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "fixture.json").write_text(
                f'{{"{escaped}":"{escaped}"}}\n', encoding="utf-8"
            )

            self.assertEqual(
                [{"code": "seat_name", "line": 1, "path": "fixture.json"}],
                module.scan_tree(root),
            )

    def test_capability_matrix_grok_attribution_is_fenced_semantically(self) -> None:
        """Bare product prose stays safe while seat-valued matrix fields are refused."""

        module = self.module()
        escaped = "".join(
            f"\\u{ord(character):04x}" for character in VERIFICATION_SEAT
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            matrix = root / "docs" / "capability-matrix.v0.json"
            matrix.parent.mkdir(parents=True)
            matrix.write_text(
                json.dumps(
                    {
                        "seeded_by": VERIFICATION_SEAT,
                        "harness": VERIFICATION_SEAT,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/capability-matrix.v0.json",
                    }
                ],
                module.scan_tree(root),
            )

            matrix.write_text(
                f'{{"seeded_by":"{escaped}","harness":"{escaped}"}}',
                encoding="utf-8",
            )
            self.assertEqual(
                [
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/capability-matrix.v0.json",
                    }
                ],
                module.scan_tree(root),
            )

    def test_bom_json_seat_names_are_checked_semantically(self) -> None:
        """A BOM cannot hide an escaped exact id from the semantic fence."""

        module = self.module()
        escaped = "".join(f"\\u{ord(character):04x}" for character in ARCHITECT_SEAT)
        source = f'{{"reviewer":"{escaped}"}}'
        fixtures = {
            "utf16le.json": b"\xff\xfe" + source.encode("utf-16-le"),
            "utf16be.json": b"\xfe\xff" + source.encode("utf-16-be"),
            "utf32le.json": b"\xff\xfe\x00\x00" + source.encode("utf-32-le"),
            "utf32be.json": b"\x00\x00\xfe\xff" + source.encode("utf-32-be"),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, data in fixtures.items():
                (root / name).write_bytes(data)

            self.assertEqual(
                [
                    {"code": "seat_name", "line": 1, "path": name}
                    for name in sorted(fixtures)
                ],
                module.scan_tree(root),
            )

    def test_ambiguous_grok_product_token_is_not_fenced_by_lexical_position(self) -> None:
        """R4 protects product prose even when the same token was once a seat name."""

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

            self.assertEqual([], module.scan_tree(root))

            copy_ledger = root / "docs" / "COPY-LEDGER.md"
            copy_ledger.write_text(files["docs/COPY-LEDGER.md"] * 2, encoding="utf-8")
            (root / "docs" / "unsafe.md").write_text(product + "\n", encoding="utf-8")
            self.assertEqual([], module.scan_tree(root))

    def test_ambiguous_grok_research_prose_is_not_count_gated(self) -> None:
        """Product prose remains protected without a fragile line-count lexical allowance."""

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
            self.assertEqual([], module.scan_tree(root))

    def test_signed_historical_records_are_checked_by_the_postcondition(self) -> None:
        """Record authenticity forbids rewriting harbor bytes, not checking projected bytes."""

        module = self.module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exact_build_seat = f"{BUILD_SEAT_PREFIX}floati"
            fixtures = {
                "docs/rulings/owner-record.md": f"signed by {ARCHITECT_SEAT}\n",
                "docs/design/brief-2026-08-30.md": f"assigned to {exact_build_seat}\n",
                "docs/design/nested/brief-2026-08-30-amended.md": (
                    f"assigned to {exact_build_seat}\n"
                ),
                "docs/design/brief-2026-08.md": f"assigned to {exact_build_seat}\n",
                "docs/other/brief-2026-08-30.md": f"assigned to {exact_build_seat}\n",
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
                        "path": "docs/design/brief-2026-08-30.md",
                    },
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/design/brief-2026-08.md",
                    },
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/design/nested/brief-2026-08-30-amended.md",
                    },
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/other/brief-2026-08-30.md",
                    },
                    {
                        "code": "seat_name",
                        "line": 1,
                        "path": "docs/rulings/owner-record.md",
                    },
                ],
                module.scan_tree(root),
            )

    def test_readme_grok_product_lines_are_protected_without_lexical_allowances(self) -> None:
        """Product rows stay unchanged and no longer depend on line-count exemptions."""

        module = self.module()
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
                    "readme_desktop_product_lines": 0,
                    "readme_harness_product_lines": 0,
                    "research_product_lines": 0,
                    "vendor_product_sites": 0,
                },
                module.allowlist_measurements(root),
            )

            path.write_text("new generated row\n" + "".join(lines), encoding="utf-8")
            self.assertEqual([], module.scan_tree(root))

            lines[67] = lines[66]
            path.write_text("".join(lines), encoding="utf-8")
            self.assertEqual([], module.scan_tree(root))

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


def _fl4_planted_world(seats: list[str]) -> list[dict[str, str]]:
    """One plant per ruled separator shape, per derived seat, at runtime.

    FL-4's shape family, as positions: the seat id hyphen-joined after
    the lane prefix, slash-joined after the lane segment, either with a
    row suffix hanging off it, the possessive, and — where the seat is a
    numbered harness lane — the bare harness-ordinal form. The
    verification seat is product-first: only its lane-prefixed bare and
    numbered coordinate spellings are in scope.
    """

    lane_prefix = BUILD_SEAT_PREFIX
    harness = bytes.fromhex("7a636f6465").decode("ascii")
    verify = VERIFICATION_SEAT
    plants: list[dict[str, str]] = []
    for seat in seats:
        row = [
            ("lane-<seat>", "lane-" + seat),
            ("lane/<seat>", "lane/" + seat),
            ("lane/<seat>-<suffix>", "lane/" + seat + "-wd-4"),
            ("<seat>'s", seat + "'s note"),
        ]
        if seat == verify:
            row = [
                item
                for item in row
                if item[0] not in ("<seat>'s", "lane/<seat>-<suffix>")
            ]
            row.append(("lane-<verify>-<n>", "lane-" + verify + "-3"))
            row.append(("lane/<verify>-<n>", "lane/" + verify + "-3"))
        if seat.startswith(lane_prefix):
            tail = seat[len(lane_prefix) :]
            row.append(("lane/<tail>", "lane/" + tail))
            row.append(("lane/<tail>-<suffix>", "lane/" + tail + "-wd-4"))
            body, separator, digits = tail.rpartition("-")
            if separator and body and digits.isdigit():
                row.append(("<harness>-<n>", body + separator + digits))
        for shape, token in row:
            clean = (
                shape.replace("/", "-over-")
                .replace("<", "")
                .replace(">", "")
                .replace("'", "-pos")
            )
            plants.append(
                {
                    "seat_hex": seat.encode("utf-8").hex(),
                    "shape": shape,
                    "token_hex": token.encode("utf-8").hex(),
                    "file": seat.encode("utf-8").hex() + "-" + clean + ".md",
                }
            )
    plants.sort(key=lambda plant: plant["file"])
    return plants


class SeparatorShapeProjectionTests(unittest.TestCase):
    """FL-4: fence and redactor agree on a seat id in every separator shape."""

    def module(self):
        spec = importlib.util.find_spec("scripts.public_name_fence")
        self.assertIsNotNone(spec, "scripts.public_name_fence is missing")
        return importlib.import_module("scripts.public_name_fence")

    def exporter(self):
        # FL-4 Am.1: the exporter is private to the harbor by export policy, so in
        # the public projection this is a stated skip, never an error (the
        # projected suite at the 0.1.2 cut tip red'd 1 of 4534 on exactly this).
        require_private_artifact(self, "scripts/export_public.py")
        spec = importlib.util.find_spec("scripts.export_public")
        self.assertIsNotNone(spec, "scripts.export_public is missing")
        return importlib.import_module("scripts.export_public")

    def _world(self) -> list[dict[str, str]]:
        seats = [
            bytes.fromhex(value).decode("ascii")
            for value in DERIVED_SEAT_POPULATION_HEX
        ]
        return _fl4_planted_world(seats)

    def test_every_planted_separator_shape_is_a_finding(self) -> None:
        """FL-4 RED: the shapes the cut's export delta sweep found escape no more.

        Pre-fix (cut tip e8d349c7a298ed901dc2d7cd800dea356869ed1b) this
        world measured 126 plants with 56 escapes: both lane-segment
        spellings of all thirteen lane seats, the bare harness-ordinal
        forms of the three numbered seats, the cut's own test-docstring
        pair, and the shapes of the six map-lacking seats. The planted
        set is written to disk and read back so the count pin counts
        the world, never an in-test literal compared to itself.
        """

        module = self.module()
        plants = self._world()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "plant-manifest.json"
            manifest.write_text(json.dumps(plants, indent=1), encoding="utf-8")
            world = json.loads(manifest.read_text(encoding="utf-8"))
            tree = root / "planted"
            tree.mkdir()
            for plant in world:
                (tree / plant["file"]).write_text(
                    "plant: "
                    + bytes.fromhex(plant["token_hex"]).decode("utf-8")
                    + "\n",
                    encoding="utf-8",
                )
            # the cut's instance, as a public test docstring: the rejected
            # arm — the redactor never rewrites executable source, so the
            # fence is what refuses it
            numbered = bytes.fromhex("7a636f6465").decode("ascii") + "-2"
            docstring_token = (
                "lane/" + numbered + "-wd-4 and a " + numbered + "'s reference"
            )
            docstring = '"""' + docstring_token + '"""\nVALUE = 1\n'
            (tree / "tests").mkdir()
            (tree / "tests" / "planted_surface.py").write_text(
                docstring, encoding="utf-8"
            )
            world.append(
                {
                    "seat_hex": (BUILD_SEAT_PREFIX + numbered).encode("utf-8").hex(),
                    "shape": "py-docstring lane/<short>-<suffix> + <short>'s",
                    "token_hex": docstring_token.encode("utf-8").hex(),
                    "file": "tests/planted_surface.py",
                }
            )

            findings = module.scan_tree(tree)

        seat_findings = [
            finding
            for finding in findings
            if finding["code"] in ("seat_name", "seat_name_path")
        ]
        caught_files = {finding["path"] for finding in seat_findings}
        escaped = [plant["file"] for plant in world if plant["file"] not in caught_files]
        self.assertEqual(
            len(world),
            len(seat_findings),
            msg=(
                f"planted world is {len(world)} shapes across "
                f"{len(DERIVED_SEAT_POPULATION_HEX)} derived seats; every one "
                f"must be a finding; escaped: {escaped}"
            ),
        )
        self.assertEqual(
            sorted(plant["file"] for plant in world),
            sorted(finding["path"] for finding in seat_findings),
        )

    def test_harness_words_stay_public_in_every_control_shape(self) -> None:
        """The fence refuses nothing its own claim permits.

        Harness words used AS harness words, the product verb, a
        non-seat branch, the lane-workspace record shape, the help
        example, the verification seat's product compounds and its
        harness-lane branch spellings all stay public; an alphanumeric
        continuation of any new projection stays public too.
        """

        module = self.module()
        harness = bytes.fromhex("7a636f6465").decode("ascii")
        verify = VERIFICATION_SEAT
        controls = {
            "ctl-harness-bare.txt": harness,
            "ctl-other-harnesses.txt": "codex cursor claude",
            "ctl-product-verb.txt": "floati lane open --root R",
            "ctl-nonseat-branch.txt": "the bundle canonical_ref lane/hm0",
            "ctl-verify-harness-branch.txt": (
                "branch lane/" + verify + "-gauntlet off main"
            ),
            "ctl-lane-workspace.txt": (
                "lane-workspace-018f6d2e-7c3a-7f21-9b2e-3f1a4d5c6b7e"
            ),
            "ctl-help-example.txt": "example lane-b form",
            "ctl-product-compounds.txt": (
                verify + "-build " + verify + "_build the " + verify.title() + " console"
            ),
            "ctl-continuations.txt": (
                "x" + harness + "-2 lane/" + harness + "2f tail"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, text in controls.items():
                (root / name).write_text(text + "\n", encoding="utf-8")

            findings = module.scan_tree(root)

        self.assertEqual(
            [],
            [
                finding
                for finding in findings
                if finding["code"] in ("seat_name", "seat_name_path")
            ],
        )

    def test_the_redactor_adapts_every_planted_shape_in_its_projection(self) -> None:
        """One shared pattern: every catchable shape is a seat_name_to_role edit.

        Each planted markdown surface goes through the exporter's exact
        seat redaction and comes out with the seat_name_to_role adapter
        fired, the token replaced by its reviewed role label, and no
        seat-shaped residue. Executable source is the rejected arm: the
        redactor refuses to rewrite it, which is why the fence must
        refuse it instead.
        """

        fence = self.module()
        exporter = self.exporter()
        world = self._world()
        plants = [plant for plant in world if plant["file"].endswith(".md")]
        self.assertEqual(len(world), len(plants))
        # One pre-existing splice (measured at the cut tip too): the
        # hyphen form of a compound seat whose head is itself a seat —
        # head-replacing semantics splice the role label against the
        # residue and the residue still spells seat vocabulary. The
        # redaction still fires; the post-write fence then refuses the
        # projection, which is the fail-safe. Named in the FL-4 receipt.
        splice_seat_hex = bytes.fromhex(
            "707564646c652d666c6f6174692d617263686974656374"
        ).decode("ascii")
        for plant in plants:
            with self.subTest(shape=plant["shape"], seat=plant["seat_hex"]):
                token = bytes.fromhex(plant["token_hex"]).decode("utf-8")
                data = ("plant: " + token + "\n").encode("utf-8")
                adapted, adapters = exporter._adapt(
                    "docs/notes/" + plant["file"], data
                )
                self.assertEqual(
                    ["seat_name_to_role"],
                    adapters,
                    "the projection must adapt this shape via seat_name_to_role",
                )
                text = adapted.decode("utf-8")
                self.assertNotIn(token, text)
                residue = list(fence.SEAT_NAME_PATTERN.finditer(text))
                if (
                    plant["seat_hex"] == splice_seat_hex.encode("utf-8").hex()
                    and plant["shape"] == "lane-<seat>"
                ):
                    self.assertNotEqual(
                        [],
                        residue,
                        "the known compound-head splice must stay measured",
                    )
                else:
                    self.assertEqual(
                        [],
                        residue,
                        "redacted bytes still carry a seat-shaped token",
                    )

        numbered = bytes.fromhex("7a636f6465").decode("ascii") + "-2"
        py_docstring = (
            '"""lane/' + numbered + "-wd-4 and a " + numbered + "'s\"\"\"\n"
        )
        py_updated, py_changed = exporter._redact_exact_seat_ids(
            "tests/planted_surface.py", py_docstring.encode("utf-8")
        )
        self.assertFalse(py_changed, "the redactor must never rewrite .py")
        self.assertEqual(py_docstring.encode("utf-8"), py_updated)


if __name__ == "__main__":
    unittest.main()
