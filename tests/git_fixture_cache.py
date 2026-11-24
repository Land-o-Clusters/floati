"""Build a git fixture repository once per process; give every test its own copy.

HARBOR-SPEED-R-B measured the suite's git-spawn tax: 16,311 git children in
one serial run, most of them fixtures re-running ``init`` / ``config`` /
``add`` / ``commit`` with byte-identical inputs in every ``setUp``. This module
removes that repetition and nothing else:

* ``cached_tree(destination, key, build)`` runs ``build`` ONCE per process for
  a given ``key`` (into a private template directory) and then materialises an
  independent copy at ``destination`` for each caller. The copy is an APFS
  ``clonefile(2)`` where the platform offers it (one syscall, copy-on-write)
  and ``shutil.copytree`` everywhere else. Either way the caller owns every
  byte it receives: mutating one copy can never reach the template or another
  test's copy (``tests.test_git_fixture_cache`` proves it).

  A fresh commit leaves the index stat-clean; a copied tree does not, because
  inode and ctime change with the copy, and plumbing such as
  ``git diff-files`` would then report every tracked file as modified. Each
  copy of a git work tree is therefore refreshed (``update-index --refresh``)
  so the caller receives a repository indistinguishable from one it had just
  committed itself.

  ``key`` must name EVERYTHING ``build`` depends on. The builder's module and
  qualified name are folded into the digest automatically, so two builders
  can never share a template by accident; the caller owns the rest.

* ``fixture_git(name)`` resolves the git binary a fixture already used to the
  executable that actually runs. On macOS ``/usr/bin/git`` is an ``xcrun``
  shim that re-locates the developer directory on every call — measured at
  roughly 6 ms of pure overhead per spawn on top of git's own ~6 ms — and then
  executes the Command Line Tools git. Resolving that once yields the same
  binary, so fixture semantics are unchanged. Product code is never pointed at
  this resolution; it keeps whatever executable its test hands it.
"""
from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from tests.temp_roots import REAL_TEMP_ROOT

_XCRUN_SHIM = "/usr/bin/git"
_resolved: Dict[Tuple[str, str], str] = {}
_templates: Dict[str, Tuple[Path, Any]] = {}
_lock = threading.Lock()
_cache_root: Optional[Path] = None
_clonefile: Any = None


def fixture_git(name: str = "git") -> str:
    """Return the executable that ``name`` runs, without the macOS xcrun shim.

    ``name`` is either an absolute path (``"/usr/bin/git"``) or a bare command
    resolved on the current ``PATH`` exactly as ``subprocess`` would. Only the
    shim is rewritten, and only to the binary the shim itself would execute;
    every other answer is returned unchanged.
    """

    key = (name, os.environ.get("PATH", ""))
    cached = _resolved.get(key)
    if cached is not None:
        return cached
    found = name if os.path.isabs(name) else (shutil.which(name) or name)
    resolved = found
    if sys.platform == "darwin" and found == _XCRUN_SHIM:
        try:
            completed = subprocess.run(
                ["/usr/bin/xcrun", "--find", "git"],
                capture_output=True, text=True, check=True, timeout=30,
            )
            candidate = completed.stdout.strip()
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                resolved = candidate
        except (OSError, subprocess.SubprocessError):
            resolved = found
    _resolved[key] = resolved
    return resolved


def _template_root() -> Path:
    global _cache_root
    if _cache_root is None:
        _cache_root = Path(
            tempfile.mkdtemp(prefix="git-fixture-templates-", dir=REAL_TEMP_ROOT)
        )
        atexit.register(shutil.rmtree, str(_cache_root), True)
    return _cache_root


def _digest(key: Any, build: Callable[..., Any]) -> str:
    identity = (getattr(build, "__module__", ""), getattr(build, "__qualname__", repr(build)))
    return hashlib.sha256(repr((identity, key)).encode("utf-8")).hexdigest()[:32]


def _clone(source: Path, destination: Path) -> None:
    """Copy-on-write clone where the platform offers it, a real copy otherwise."""

    global _clonefile
    if sys.platform == "darwin":
        if _clonefile is None:
            try:
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                function = libc.clonefile
                function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
                function.restype = ctypes.c_int
                _clonefile = function
            except (OSError, AttributeError):
                _clonefile = False
        if _clonefile:
            if _clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
                return
            if destination.exists():
                raise OSError(ctypes.get_errno(), "clonefile left a partial copy", str(destination))
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def materialise(
    template: Path, destination: Path, *, git: str = "git", refresh_index: bool = True
) -> None:
    """Copy ``template`` to ``destination`` and make any git index stat-clean.

    ``refresh_index=False`` leaves the refresh to a caller whose own git
    helper must stay the first git step it runs (a fixture that injects
    failures through that helper); such a caller MUST run
    ``update-index -q --refresh`` itself before handing the copy on.
    """

    destination = Path(destination)
    if destination.is_dir() and not destination.is_symlink() and not any(destination.iterdir()):
        # The caller made this directory (a TemporaryDirectory, a mkdir); keep
        # it — its mode and identity are the caller's — and fill it.
        for entry in sorted(os.listdir(template)):
            _clone(Path(template) / entry, destination / entry)
    elif os.path.lexists(destination):
        raise FileExistsError(17, "fixture destination is occupied", str(destination))
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _clone(Path(template), destination)
    if refresh_index and (destination / ".git").is_dir():
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(
            [fixture_git(git), "-C", str(destination), "update-index", "-q", "--refresh"],
            env=environment, capture_output=True, check=True,
        )


def cached_tree(
    destination: Path,
    key: Any,
    build: Callable[[Path], Any],
    *,
    git: str = "git",
    refresh_index: bool = True,
) -> Any:
    """Give ``destination`` its own copy of the tree ``build`` makes for ``key``.

    ``build(path)`` receives an existing empty directory and populates it; its
    return value is cached with the template and returned to every caller, so
    it must be immutable (a SHA string, a tuple). ``destination`` must not
    exist, or must be an empty directory.
    """

    digest = _digest(key, build)
    with _lock:
        entry = _templates.get(digest)
        if entry is None:
            template = _template_root() / digest
            template.mkdir()
            try:
                metadata = build(template)
            except BaseException:
                shutil.rmtree(template, ignore_errors=True)
                raise
            entry = (template, metadata)
            _templates[digest] = entry
    materialise(entry[0], Path(destination), git=git, refresh_index=refresh_index)
    return entry[1]
