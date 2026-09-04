from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "confirm_public_provenance.py"
HARBOR_SOURCE_SHA = "b" * 40
MANIFEST_DIGEST = "c" * 64

HARBOR_README = "the shared readme body\n"
HARBOR_NOTE = "ruling quoting the private seat name\n"
PUBLIC_NOTE = "ruling quoting <the-parent-project>\n"

BASELINE = json.dumps(
    {"public_commit": "a" * 40, "public_paths": ["README.md"]},
    sort_keys=True,
)


def git(root: Path, *arguments: str) -> str:
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


def initialize_harbor(root: Path) -> str:
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "config", "user.name", "fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "harbor base")
    (root / "README.md").write_text(HARBOR_README, encoding="utf-8")
    (root / "docs" / "design").mkdir(parents=True)
    (root / "docs" / "design" / "note.md").write_text(HARBOR_NOTE, encoding="utf-8")
    (root / ".github").mkdir()
    (root / ".github" / "public-export-baseline.v0.json").write_text(
        BASELINE, encoding="utf-8"
    )
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "harbor source")
    return git(root, "rev-parse", "HEAD")


def write_public_car(
    root: Path,
    source_sha: str,
    files: dict[str, str],
    subject: str | None = None,
    manifest_digest: str = MANIFEST_DIGEST,
) -> str:
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    git(root, "config", "user.name", "fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "public base")
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    body = f"Source-SHA: {source_sha}\nManifest-Digest: {manifest_digest}"
    git(
        root,
        "commit",
        "-q",
        "-m",
        subject or f"export: project harbor {source_sha[:12]}",
        "-m",
        body,
    )
    return git(root, "rev-parse", "HEAD")


class ConfirmPublicProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def run_script(
        self, public: Path, public_commit: str, harbor: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--public-root",
                str(public),
                "--public-commit",
                public_commit,
                "--harbor-root",
                str(harbor),
            ],
            capture_output=True,
            text=True,
        )

    def test_confirms_verbatim_and_projected_paths(self) -> None:
        harbor = self.base / "harbor"
        source_sha = initialize_harbor(harbor)
        public = self.base / "public"
        public_commit = write_public_car(
            public,
            source_sha,
            {
                "README.md": HARBOR_README,
                "docs/design/note.md": PUBLIC_NOTE,
            },
        )
        completed = self.run_script(public, public_commit, harbor)
        self.assertEqual(0, completed.returncode, completed.stderr)
        artifact = json.loads(completed.stdout)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("confirm-public-provenance", artifact["command"])
        evidence = artifact["evidence"]
        self.assertEqual(source_sha, evidence["harbor_source_commit"])
        self.assertEqual(public_commit, evidence["public_commit"])
        self.assertEqual(1, evidence["verbatim_count"])
        self.assertEqual(1, evidence["projected_count"])
        self.assertEqual(["docs/design/note.md"], evidence["projected_paths"])
        self.assertEqual([], evidence["absent_paths"])
        self.assertEqual(MANIFEST_DIGEST, evidence["manifest_digest"])
        self.assertEqual("a" * 40, evidence["baseline_public_commit"])
        self.assertFalse(evidence["baseline_matches_public_commit"])

    def test_refuses_public_path_absent_from_harbor_source(self) -> None:
        harbor = self.base / "harbor"
        source_sha = initialize_harbor(harbor)
        public = self.base / "public"
        public_commit = write_public_car(
            public,
            source_sha,
            {
                "README.md": HARBOR_README,
                "invented.md": "no harbor origin\n",
            },
        )
        completed = self.run_script(public, public_commit, harbor)
        self.assertEqual(20, completed.returncode)
        artifact = json.loads(completed.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "provenance_path_unsourced", artifact["evidence"]["code"]
        )
        self.assertIn("invented.md", artifact["evidence"]["absent_paths"])

    def test_refuses_commit_without_source_sha_body(self) -> None:
        harbor = self.base / "harbor"
        initialize_harbor(harbor)
        public = self.base / "public"
        public.mkdir()
        git(public, "init", "-q", "--initial-branch=main")
        git(public, "config", "user.name", "fixture")
        git(public, "config", "user.email", "fixture@example.invalid")
        (public / "README.md").write_text("seed\n", encoding="utf-8")
        git(public, "add", ".")
        git(public, "commit", "-q", "-m", "plain public commit")
        public_commit = git(public, "rev-parse", "HEAD")
        completed = self.run_script(public, public_commit, harbor)
        self.assertEqual(20, completed.returncode)
        artifact = json.loads(completed.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "provenance_source_sha_absent", artifact["evidence"]["code"]
        )

    def test_refuses_subject_source_mismatch(self) -> None:
        harbor = self.base / "harbor"
        source_sha = initialize_harbor(harbor)
        public = self.base / "public"
        public_commit = write_public_car(
            public,
            source_sha,
            {"README.md": HARBOR_README},
            subject=f"export: project harbor {'d' * 12}",
        )
        completed = self.run_script(public, public_commit, harbor)
        self.assertEqual(20, completed.returncode)
        artifact = json.loads(completed.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "provenance_subject_mismatch", artifact["evidence"]["code"]
        )

    def test_refuses_source_absent_from_harbor(self) -> None:
        harbor = self.base / "harbor"
        initialize_harbor(harbor)
        public = self.base / "public"
        public_commit = write_public_car(
            public,
            "e" * 40,
            {"README.md": HARBOR_README},
        )
        completed = self.run_script(public, public_commit, harbor)
        self.assertEqual(20, completed.returncode)
        artifact = json.loads(completed.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "provenance_source_unresolved", artifact["evidence"]["code"]
        )

    def test_refuses_missing_public_root(self) -> None:
        harbor = self.base / "harbor"
        initialize_harbor(harbor)
        completed = self.run_script(
            self.base / "absent", "f" * 40, harbor
        )
        self.assertEqual(20, completed.returncode)
        artifact = json.loads(completed.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("provenance_root_invalid", artifact["evidence"]["code"])

    def test_refuses_malformed_public_commit_argument(self) -> None:
        harbor = self.base / "harbor"
        source_sha = initialize_harbor(harbor)
        public = self.base / "public"
        write_public_car(public, source_sha, {"README.md": HARBOR_README})
        completed = self.run_script(public, "not-a-sha", harbor)
        self.assertEqual(20, completed.returncode)
        artifact = json.loads(completed.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "provenance_commit_invalid", artifact["evidence"]["code"]
        )


if __name__ == "__main__":
    unittest.main()
