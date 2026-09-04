from __future__ import annotations

import tempfile
import unittest
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.scrub import scan_generated_tree, scan_git_history_notes
from tests.export_inventory import (
    POLICY_RELATIVE,
    classify_inventory,
    export_include_set,
    materialise_exposed_tree,
)
from tests.private_artifacts import require_private_artifact


PRIVATE_FLEET = bytes.fromhex("707564646c652d666c656574").decode("ascii")

# This constant is an ALLOWLIST ENTRY: one exact README line the private-name
# scan below is permitted to skip. Its bytes are therefore load-bearing in the
# only sense an exemption can be -- change them and a different line is
# exempted, or none is. The recipient carries the retired repository name, so
# it is built from the fence's own governed token rather than spelled: this is
# the file that RUNS the tree scan, and a scanner that spells the word it
# forbids reports itself as a finding. Pinned by nothing but this comment and
# the scan itself, which is why the token is imported rather than retyped.
APPROVED_README_RECEIPT = (
    '{"id":"ack-01a0088d18e77c9e9fa3599f20038f9d",'
    '"item_ids":["msg-01a0088c08207e31b874d517621e164b"],'
    f'"kind":"ack_receipt","recipient":"builder-{RETIRED_PRODUCT_NAME}",'
    f'"schema_version":0,"tenant_id":"{PRIVATE_FLEET}",'
    '"timestamp":"2026-08-16T03:10:59.815Z"}'
)


class SourceScrubTests(unittest.TestCase):
    def test_managed_session_fixture_uses_no_legacy_name(self) -> None:
        forbidden = bytes.fromhex("707564646c65").decode("ascii")
        fixtures = [
            Path(__file__).with_name("test_managed.py"),
            *sorted(Path("tests/fixtures/managed/v0").glob("*.json")),
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture.as_posix()):
                self.assertNotIn(
                    forbidden,
                    fixture.read_text(encoding="utf-8").casefold(),
                )

    def test_public_text_contains_no_private_source_name(self) -> None:
        forbidden = bytes.fromhex("707564646c65").decode("ascii")
        public_files = (
            Path("README.md"),
            Path("docs/CONFLUENCE-v0.md"),
            Path("docs/DESIGN.md"),
            Path("docs/SPEC-DRAFT.md"),
            Path("floati/managed.py"),
        )
        hits = []
        for path in public_files:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if path == Path("README.md") and line == APPROVED_README_RECEIPT:
                    continue
                if forbidden in line.casefold():
                    hits.append((path.as_posix(), line_number, line))

        self.assertEqual([], hits)

    def test_fixture_proves_scrubber_detects_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifact.txt").write_bytes(bytes.fromhex("7369676e616c6372616674"))
            self.assertEqual(["artifact.txt"], scan_generated_tree(root))

    def test_generated_repository_artifacts_are_scrubbed(self) -> None:
        """R-N4 Am.3: the population is the exporter's include set, derived.

        Raw containment, so this scanner and the sweep in `test_name_sweep.py`
        do not subsume each other — the sweep's pattern spares the on-disk
        coordinates the product reads, and this one does not. "Green" needs
        both, over the same derived population.
        """

        with tempfile.TemporaryDirectory() as temporary:
            exposed = Path(temporary)
            written = materialise_exposed_tree(exposed)
            self.assertEqual(len(export_include_set()), len(written))
            self.assertEqual([], scan_generated_tree(exposed, paths=written))

    def test_a_planted_name_in_an_included_path_is_a_finding(self) -> None:
        """Control (a): the narrowed scrub still reads what an export carries."""

        require_private_artifact(self, POLICY_RELATIVE)
        relative = "floati/scrub_control_fixture.py"
        self.assertEqual((relative,), classify_inventory((relative,)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            planted = root / relative
            planted.parent.mkdir(parents=True, exist_ok=True)
            planted.write_text(f"DOMAIN = '{RETIRED_PRODUCT_NAME}'\n", encoding="utf-8")

            self.assertEqual(
                [relative],
                scan_generated_tree(root, paths=classify_inventory((relative,))),
            )

    def test_the_same_planted_name_in_an_excluded_path_is_not_a_finding(self) -> None:
        """Control (b): the same bytes, cleared by the policy and nothing else.

        A PROJECTION DETECTOR: with no policy there is no "excluded", so the
        excluded path classifies as included and the control cannot pose its
        question. It skips as a typed absence rather than failing.
        """

        require_private_artifact(self, POLICY_RELATIVE)
        relative = "docs/superpowers/scrub-control-fixture.md"
        self.assertEqual((), classify_inventory((relative,)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            planted = root / relative
            planted.parent.mkdir(parents=True, exist_ok=True)
            planted.write_text(f"DOMAIN = '{RETIRED_PRODUCT_NAME}'\n", encoding="utf-8")

            self.assertEqual([relative], scan_generated_tree(root, paths=(relative,)))
            self.assertEqual(
                [], scan_generated_tree(root, paths=classify_inventory((relative,)))
            )

    def test_generated_repository_scrub_uses_only_tracked_files(self) -> None:
        """Ignored worktrees are not publication inputs, but forced tracked files are."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"],
                cwd=root,
                check=True,
            )
            (root / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
            (root / "README.md").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=root, check=True)
            ignored = root / ".worktrees/hm3i/HM3I_BRIEF.md"
            ignored.parent.mkdir(parents=True)
            ignored.write_bytes(bytes.fromhex("5369676e616c4372616674"))

            self.assertEqual([], scan_generated_tree(root))
            hostile_git_environment = {
                "GIT_DIR": str(root / "missing-git-dir"),
                "GIT_WORK_TREE": str(root / "wrong-work-tree"),
                "GIT_INDEX_FILE": str(root / "missing-index"),
            }
            with patch.dict(os.environ, hostile_git_environment):
                self.assertEqual([], scan_generated_tree(root))

            subprocess.run(
                ["git", "add", "--force", ".worktrees/hm3i/HM3I_BRIEF.md"],
                cwd=root,
                check=True,
            )
            self.assertEqual(
                [".worktrees/hm3i/HM3I_BRIEF.md"],
                scan_generated_tree(root),
            )

    def test_history_note_scanner_detects_forbidden_commit_message_without_source_literal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Scrub Fixture",
                "GIT_AUTHOR_EMAIL": "scrub@example.invalid",
                "GIT_COMMITTER_NAME": "Scrub Fixture",
                "GIT_COMMITTER_EMAIL": "scrub@example.invalid",
            }
            forbidden = bytes.fromhex("5369676e616c4372616674").decode("ascii")
            subprocess.run(
                ["git", "commit", "--quiet", "-m", f"remove {forbidden} source"],
                cwd=root,
                env=environment,
                check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            self.assertEqual(
                [f"{sha}:commit-message"], scan_git_history_notes(root)
            )

    def test_repository_history_notes_are_scrubbed(self) -> None:
        self.assertEqual([], scan_git_history_notes(Path.cwd()))

    def test_history_note_scanner_checks_git_notes_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Scrub Fixture",
                "GIT_AUTHOR_EMAIL": "scrub@example.invalid",
                "GIT_COMMITTER_NAME": "Scrub Fixture",
                "GIT_COMMITTER_EMAIL": "scrub@example.invalid",
            }
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=root, check=True)
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "clean subject"], cwd=root, env=environment, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            forbidden = bytes.fromhex("5369676e616c4372616674").decode("ascii")
            subprocess.run(
                ["git", "notes", "--ref=publication", "add", "-m", f"archive {forbidden}"],
                cwd=root,
                env=environment,
                check=True,
            )

            self.assertEqual(
                [f"refs/notes/publication:{sha}:note"], scan_git_history_notes(root)
            )

    def test_history_note_scanner_detects_annotated_tag_message(self) -> None:
        """Catches a forbidden name living only in an annotated tag object."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Scrub Fixture",
                "GIT_AUTHOR_EMAIL": "scrub@example.invalid",
                "GIT_COMMITTER_NAME": "Scrub Fixture",
                "GIT_COMMITTER_EMAIL": "scrub@example.invalid",
            }
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "clean subject"],
                cwd=root,
                env=environment,
                check=True,
            )
            forbidden = bytes.fromhex("5369676e616c4372616674").decode("ascii")
            subprocess.run(
                [
                    "git", "tag", "-a", "hist-1-fixture",
                    "-m", f"archive {forbidden}",
                ],
                cwd=root,
                env=environment,
                check=True,
            )

            self.assertEqual(
                ["refs/tags/hist-1-fixture:tag-message"],
                scan_git_history_notes(root),
            )

    def test_history_note_scanner_detects_forbidden_ref_name(self) -> None:
        """Catches a forbidden name living only in the ref path, not the object."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Scrub Fixture",
                "GIT_AUTHOR_EMAIL": "scrub@example.invalid",
                "GIT_COMMITTER_NAME": "Scrub Fixture",
                "GIT_COMMITTER_EMAIL": "scrub@example.invalid",
            }
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "clean subject"],
                cwd=root,
                env=environment,
                check=True,
            )
            forbidden = bytes.fromhex("5369676e616c4372616674").decode("ascii")
            subprocess.run(
                ["git", "tag", f"hist-1-{forbidden}"],
                cwd=root,
                env=environment,
                check=True,
            )

            self.assertEqual(
                [f"refs/tags/hist-1-{forbidden}:ref-name"],
                scan_git_history_notes(root),
            )

    def test_history_note_scanner_ignores_clean_annotated_tag_and_lightweight_tag(self) -> None:
        """A clean annotated-tag message and a clean lightweight tag are not hits.

        A lightweight tag whose name is clean must not reprint the pointed-to
        commit message as a tag-message coordinate.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Scrub Fixture",
                "GIT_AUTHOR_EMAIL": "scrub@example.invalid",
                "GIT_COMMITTER_NAME": "Scrub Fixture",
                "GIT_COMMITTER_EMAIL": "scrub@example.invalid",
            }
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            forbidden = bytes.fromhex("5369676e616c4372616674").decode("ascii")
            subprocess.run(
                ["git", "commit", "--quiet", "-m", f"remove {forbidden} source"],
                cwd=root,
                env=environment,
                check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "tag", "-a", "hist-1-clean", "-m", "release notes"],
                cwd=root,
                env=environment,
                check=True,
            )
            subprocess.run(
                ["git", "tag", "hist-1-light"],
                cwd=root,
                env=environment,
                check=True,
            )

            self.assertEqual(
                [f"{sha}:commit-message"],
                scan_git_history_notes(root),
            )


if __name__ == "__main__":
    unittest.main()
