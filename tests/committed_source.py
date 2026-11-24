"""Complete-history install fixtures with exactly the caller's committed files.

A local/shared clone also copies a shallow boundary. These fixtures need a
deployable committed tree, not the caller's ancestry or uncommitted work.
Archive HEAD and commit that tree into an independent repository once per
source commit. Capture tests may borrow it for temporary worktrees; tests
that edit files still make their own clone.
"""

from __future__ import annotations

import atexit
from contextlib import ExitStack
from functools import lru_cache
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

from floati.git_process import GIT_MAINTENANCE_OPTIONS
from tests.temp_roots import REAL_TEMP_ROOT


_SCRATCH = ExitStack()
atexit.register(_SCRATCH.close)


def _git(repository: Path, *arguments: str) -> bytes:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    return subprocess.run(
        ["/usr/bin/git", *GIT_MAINTENANCE_OPTIONS, "-C", str(repository),
         *arguments],
        env=environment, capture_output=True, check=True,
    ).stdout


def complete_source(repository: Path) -> Path:
    """Return an owned clean fixture; never fetch or mutate the caller."""

    repository = repository.resolve()
    head = _git(repository, "rev-parse", "HEAD").decode().strip()
    return _snapshot(repository, head)


@lru_cache(maxsize=None)
def _snapshot(repository: Path, head: str) -> Path:
    bank = _SCRATCH.enter_context(
        tempfile.TemporaryDirectory(prefix="complete-source-", dir=REAL_TEMP_ROOT)
    )
    source = Path(bank) / "source"
    source.mkdir()
    archive = _git(repository, "-c", "tar.umask=0022", "archive", "--format=tar", head)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
        stream.extractall(source)
    _git(source, "init", "--quiet", "--initial-branch=main")
    _git(source, "add", "--force", "--all")
    _git(source, "-c", "user.name=Floati Fixture",
         "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "--quiet",
         "-m", "Committed source fixture")
    if _git(source, "rev-parse", "HEAD^{tree}") != _git(
        repository, "rev-parse", head + "^{tree}"
    ):
        raise AssertionError("complete source fixture changed the committed tree")
    return source
