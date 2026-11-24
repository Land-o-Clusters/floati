"""Owned Git fixtures for sends that cross the real SHA fence."""

import os
import subprocess
from pathlib import Path

def banked_send_repository(base: Path) -> tuple[Path, str]:
    """One fresh repository holding one banked commit: ``(path, sha)``.

    Use the path as the caller's working directory or pass it to
    ``--repository``. The fixture's remote ref satisfies the banked clause.
    """

    base.mkdir(parents=True, exist_ok=True)
    repository = base / "banked-send-repo"
    repository.mkdir()

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["/usr/bin/git", *arguments], cwd=str(repository),
            capture_output=True, text=True, check=True,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
        return completed.stdout.strip()

    git("init", "--quiet", "--initial-branch=main")
    git("config", "user.name", "Send Fixture")
    git("config", "user.email", "send-fixture@example.invalid")
    (repository / "note.md").write_text("banked\n", encoding="utf-8")
    git("add", "note.md")
    git("commit", "--quiet", "-m", "banked send fixture")
    sha = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", sha)
    return repository, sha
