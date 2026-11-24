"""Local coordinate observations for the version-one bound-record contract."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

from .errors import ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment
from .records import _bounded_string, _git_sha


OBSERVED = "observed"
CLAIMED = "claimed"
MAX_FILE_BYTES = 64 * 1024 * 1024


def stamp_fact(provenance, **fields):
    return {**fields, "provenance": provenance}


def _unresolved(detail):
    raise ProtocolRefusal("coordinate_unresolved", detail)


def file_path(value, refuse):
    _bounded_string(value, 1, 512, "coordinate_path", refuse)
    path = Path(value)
    if (path.is_absolute() or not path.parts or path.as_posix() != value
            or any(part.lower() in {"..", ".git"} for part in path.parts)):
        refuse("coordinate_path_invalid", "name a relative file in the bound checkout")
    return path


def _refuse(code, detail, remedy=None):
    raise ProtocolRefusal(code, detail, remedy=remedy)


def _git(repo, *args, missing_allowed=False):
    executable = "/usr/bin/git"
    try:
        result = subprocess.run(
            fixed_git_command(executable, repo, ("--literal-pathspecs", *args)),
            env={**fixed_git_environment(executable), "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ProtocolRefusal("coordinate_git_timeout", "Git timed out reading the bound checkout") from error
    except OSError as error:
        raise ProtocolRefusal("coordinate_git_unavailable", "Git could not read the bound checkout") from error
    if result.returncode == 1 and missing_allowed and not result.stdout and not result.stderr:
        return None
    if result.returncode != 0:
        raise ProtocolRefusal("coordinate_git_failed", "Git could not resolve the bound value")
    return result.stdout


def _require_repository(repo):
    if not repo.is_absolute() or not repo.is_dir() or repo.resolve() != repo:
        raise ProtocolRefusal("coordinate_checkout_unavailable", "the bound checkout could not be read")
    top = _git(repo, "rev-parse", "--show-toplevel").removesuffix(b"\n")
    if top != os.fsencode(repo):
        raise ProtocolRefusal("coordinate_checkout_unavailable", "the bound path is no longer a checkout root")


def resolve_repository(value):
    _bounded_string(value, 1, 4096, "repository", _refuse)
    path = Path(value)
    try:
        if not path.is_absolute():
            _unresolved("name the absolute path to the top of a Git checkout")
        repo = path.resolve(strict=True)
        _require_repository(repo)
        return repo
    except (ProtocolRefusal, OSError, RuntimeError) as error:
        raise ProtocolRefusal("coordinate_unresolved", "name the absolute path to the top of a Git checkout") from error


def _commit(repo, value):
    raw = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", value + "^{commit}",
               missing_allowed=True)
    if raw is None:
        return None
    commit = raw.decode("ascii", errors="replace").strip()
    _git_sha(commit, _refuse)
    return commit


def tracked_dirty(repo):
    return bool(_git(repo, "status", "--porcelain=v1", "--untracked-files=no",
                     "--ignore-submodules=none"))


def _file_digest(repo, value):
    parts = file_path(value, _refuse).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(repo, flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            start = os.fstat(fd)
            if not stat.S_ISREG(start.st_mode):
                raise ProtocolRefusal("coordinate_file_not_regular", "the bound file is not a regular file")
            digest = hashlib.sha256()
            remaining = MAX_FILE_BYTES
            while remaining:
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise ProtocolRefusal("coordinate_file_too_large", "the bound file exceeds 64 MiB")
            end = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(start, name) != getattr(end, name) for name in fields):
                raise ProtocolRefusal("coordinate_file_changed", "the bound file changed while it was read")
            return digest.hexdigest()
    finally:
        os.close(directory)


def observe_ref(repo, ref="HEAD", paths=()):
    _bounded_string(ref, 1, 512, "ref", _refuse)
    if ref.startswith("-") or len(paths) > 64:
        _unresolved("name a Git ref and up to 64 relative paths")
    for path in paths:
        file_path(path, _refuse)
    try:
        _require_repository(repo)
        if ref != "HEAD":
            _git(repo, "check-ref-format", "--allow-onelevel", ref)
        commit = _commit(repo, ref)
    except (ProtocolRefusal, OSError, RuntimeError) as error:
        raise ProtocolRefusal("coordinate_unresolved", "the named Git ref could not be read") from error
    if commit is None:
        _unresolved("the named Git ref does not resolve to a commit")
    return stamp_fact(OBSERVED, kind="ref", repository=str(repo), ref=ref, commit=commit,
                      paths=sorted(set(paths)))


def bind_commit(repo, commit):
    _git_sha(commit, _refuse)
    try:
        _require_repository(repo)
        resolved = _commit(repo, commit)
    except (ProtocolRefusal, OSError, RuntimeError) as error:
        raise ProtocolRefusal("coordinate_unresolved", "the named Git commit could not be read") from error
    if resolved != commit:
        _unresolved("the named Git commit does not resolve")
    return stamp_fact(CLAIMED, kind="commit", repository=str(repo), commit=commit)


def observe_files(repo, paths):
    if not 1 <= len(paths) <= 64:
        _unresolved("name 1 to 64 files for the bound checkout")
    try:
        _require_repository(repo)
        files = [{"path": path, "sha256": _file_digest(repo, path)} for path in sorted(set(paths))]
    except (ProtocolRefusal, OSError, RuntimeError) as error:
        raise ProtocolRefusal("coordinate_unresolved", "a named file could not be read") from error
    return stamp_fact(OBSERVED, kind="files", repository=str(repo), files=files)


def _verdict(state="current", cause=None, moved=()):
    return {"state": state, "cause": cause, "moved": list(moved)}


def _files_freshness(repo, coordinate):
    moved = []
    unreadable = []
    for entry in coordinate["files"]:
        path = entry["path"]
        try:
            digest = _file_digest(repo, path)
        except FileNotFoundError:
            digest = None
        except ProtocolRefusal as error:
            unreadable.append({"code": error.code, "path": path})
            continue
        except OSError:
            unreadable.append({"code": "coordinate_file_unreadable", "path": path})
            continue
        if digest != entry["sha256"]:
            moved.append({"what": "file", "path": path, "from": entry["sha256"], "to": digest})
    if unreadable:
        return _verdict("unknown", {"code": "coordinate_files_unavailable", "files": unreadable}, moved)
    return _verdict("stale", moved=moved) if moved else _verdict()


def _git_freshness(repo, coordinate):
    kind = coordinate["kind"]
    before = coordinate["commit"]
    ref = coordinate.get("ref")
    now = _commit(repo, ref if ref is not None else before)
    if now == before:
        return _verdict()
    change = {"what": kind, "from": before, "to": now}
    if ref is not None:
        change["ref"] = ref
    paths = coordinate.get("paths", [])
    if now is None or not paths:
        return _verdict("stale", moved=[change])
    raw = _git(repo, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
               before, now, "--", *paths)
    changed_paths = [os.fsdecode(name) for name in raw.split(b"\0") if name]
    if not changed_paths:
        return _verdict()
    return _verdict("stale", moved=[change, *({"what": "file", "path": path} for path in changed_paths)])


def _coordinate_freshness(coordinate):
    if coordinate["kind"] not in {"ref", "commit", "files"}:
        return _verdict("unknown", {"code": "coordinate_kind_unsupported", "kind": coordinate["kind"]})
    repo = Path(coordinate["repository"])
    try:
        _require_repository(repo)
        if coordinate["kind"] == "files":
            return _files_freshness(repo, coordinate)
        return _git_freshness(repo, coordinate)
    except ProtocolRefusal as error:
        return _verdict("unknown", {"code": error.code, "repository": str(repo)})
    except (OSError, RuntimeError):
        return _verdict("unknown", {"code": "coordinate_checkout_unavailable", "repository": str(repo)})


def freshness(coordinates):
    results = [_coordinate_freshness(coordinate) for coordinate in coordinates]
    states = {result["state"] for result in results}
    state = next((value for value in ("stale", "unknown", "unbound") if value in states),
                 "current" if results else "unbound")
    return {"state": state, "coordinates": results}
