#!/usr/bin/env python3
"""Regenerate `.github/public-export-baseline.v0.json` from a public clone.

The baseline is the one classification input that cannot be derived from this
repository: `classify_path` asks "was this path already published", and that is
a fact about the OTHER repository. It is therefore a recorded snapshot, and a
recorded snapshot needs two things a committed file does not supply by itself --
somebody able to rebuild it, and somebody able to notice it has gone stale.

This is the rebuild half. It reads a local clone of the published repository and
nothing else: no network, no remote, no fetch. The caller supplies the clone,
and the clone must be clean, because a snapshot taken over an edited working
tree records a tree nobody published.

The stale half is `assert_public_commit_current`, called by the fences where
they read the baseline (`tests/export_inventory.py`). It asks the weaker but
checkable question: is the recorded commit an ancestor of the published tip. An
ancestor recording may still be behind -- only a regeneration proves currency --
but a NON-ancestor recording is always wrong, and stale hides in the direction
that makes the fences pass on a smaller world.

Usage:

    python3 scripts/regen_public_baseline.py \\
        --public-root <public-clone-root> --out .github/public-export-baseline.v0.json

When --out is a repository's committed baseline and that repository carries the
shipped fixture (tests/fixtures/baseline-1-f1/public-export-baseline.v0.json),
the SAME bytes are written to the fixture in the same act, so the committed
file and the shipped copy cannot drift apart through a regeneration.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence


COMMAND = "regen-public-baseline"
BASELINE_RELATIVE = ".github/public-export-baseline.v0.json"
#: BASELINE-1-F1: the shipped byte-for-byte copy a projection can read. The
#: regenerator writes it in the same act as the committed baseline, so the two
#: cannot drift apart through a regeneration (Am.1: an export followed by a
#: regen reds main otherwise).
FIXTURE_RELATIVE = "tests/fixtures/baseline-1-f1/public-export-baseline.v0.json"
SCHEMA_VERSION = 0
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
GIT_CANDIDATES = ("/usr/bin/git",)
REMEDY = (
    "regenerate the baseline against a clean clone of the published repository: "
    "python3 scripts/regen_public_baseline.py --public-root <public-clone-root> "
    f"--out {BASELINE_RELATIVE}"
)


class BaselineRefusal(Exception):
    """A typed refusal. Every one names what to run next."""

    def __init__(self, code: str, detail: str, remedy: str = REMEDY) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.remedy = remedy


def git_executable() -> str:
    """Return one declared, canonical Git. PATH is never searched.

    Same rule as `floati.fleet_update._explicit_executable`, restated here so
    this script carries no import of a private module: the candidate list is
    written down, and a candidate that is not an absolute canonical executable
    file is not used.
    """

    for candidate in GIT_CANDIDATES:
        path = Path(candidate)
        try:
            if (
                path.is_absolute()
                and not path.is_symlink()
                and path.is_file()
                and path.resolve(strict=True) == path
                and os.access(path, os.X_OK)
            ):
                return str(path)
        except OSError:
            continue
    raise BaselineRefusal(
        "public_export_baseline_git_unavailable",
        "no declared Git executable among " + ", ".join(GIT_CANDIDATES),
        "install Git at one of the declared candidate paths, or run the "
        "regeneration on a host that has one",
    )


def _environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat"})
    return environment


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [git_executable(), "-C", str(root), *arguments],
        env=_environment(),
        check=False,
        capture_output=True,
        timeout=120,
    )


def _git_text(root: Path, *arguments: str) -> str:
    completed = _run_git(root, *arguments)
    if completed.returncode != 0:
        raise BaselineRefusal(
            "public_export_baseline_git_failed",
            "bounded local Git command failed: "
            + completed.stderr.decode("utf-8", "replace").strip(),
        )
    return completed.stdout.decode("utf-8").strip()


def require_public_root(root: Path) -> Path:
    """The clone must be an absolute, non-symlink Git worktree on this host."""

    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise BaselineRefusal(
            "public_export_baseline_root_invalid",
            f"public clone root must be an absolute non-symlink directory: {root}",
        )
    completed = _run_git(root, "rev-parse", "--is-inside-work-tree")
    if completed.returncode != 0 or completed.stdout.decode("utf-8").strip() != "true":
        raise BaselineRefusal(
            "public_export_baseline_root_invalid",
            f"public clone root must be a Git worktree: {root}",
        )
    return root


def require_clean(root: Path) -> None:
    """A snapshot over an edited tree records a tree nobody published."""

    completed = _run_git(root, "status", "--porcelain", "-z")
    if completed.returncode != 0:
        raise BaselineRefusal(
            "public_export_baseline_git_failed",
            "bounded local Git command failed: "
            + completed.stderr.decode("utf-8", "replace").strip(),
        )
    # `-z` and no `.strip()`: the two status columns are positional, and a
    # stripped line loses the leading space of an unstaged modification, which
    # then eats the first character of the path it is meant to name. A rename
    # emits the old path as a bare record with no status columns; it is counted
    # as dirt but not named, rather than named with three characters removed.
    records = [record for record in completed.stdout.split(b"\0") if record]
    entries = [
        record[3:].decode("utf-8", "replace")
        for record in records
        if len(record) > 3 and record[2:3] == b" "
    ]
    if records:
        raise BaselineRefusal(
            "public_export_baseline_clone_dirty",
            f"public clone has {len(records)} uncommitted record(s), naming "
            + (", ".join(sorted(entries)[:10]) or "none in the status columns"),
            f"discard or commit the local edits first (git -C {root} status), then "
            + REMEDY,
        )


def resolve_commit(root: Path, commit: str) -> str:
    completed = _run_git(root, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    resolved = completed.stdout.decode("utf-8").strip()
    if completed.returncode != 0 or not FULL_SHA.fullmatch(resolved):
        raise BaselineRefusal(
            "public_export_baseline_commit_unresolved",
            f"public clone does not contain commit {commit}",
            "fetch the published repository into the clone, or name a commit it "
            "carries; then " + REMEDY,
        )
    return resolved


def public_paths(root: Path, commit: str) -> tuple[str, ...]:
    """Every path in the published tree at `commit`.

    `-z` rather than the default listing: Git quotes and escapes unusual path
    names in the newline form, and a quoted path in the baseline would not match
    the path the exporter classifies.
    """

    completed = _run_git(root, "ls-tree", "-r", "-z", "--name-only", commit)
    if completed.returncode != 0:
        raise BaselineRefusal(
            "public_export_baseline_tree_unreadable",
            f"could not read a recursive tree for {commit}",
        )
    paths = tuple(
        entry.decode("utf-8") for entry in completed.stdout.split(b"\0") if entry
    )
    if not paths:
        raise BaselineRefusal(
            "public_export_baseline_tree_empty",
            f"the tree at {commit} carries no paths",
        )
    if len(set(paths)) != len(paths):
        raise BaselineRefusal(
            "public_export_baseline_tree_unreadable",
            f"the tree at {commit} lists a path twice",
        )
    return paths


def build_document(paths: Sequence[str], commit: str) -> dict[str, object]:
    return {
        "public_commit": commit,
        "public_paths": sorted(paths),
        "schema_version": SCHEMA_VERSION,
    }


def render_document(document: Mapping[str, object]) -> str:
    """The committed file's exact bytes: sorted keys, two-space indent, newline."""

    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def assert_public_commit_current(
    public_root: Path, recorded_commit: str, public_tip: str = "HEAD"
) -> str:
    """RED when the recorded public commit is not an ancestor of the tip.

    Returns the resolved tip so a caller can record what it checked against.
    """

    root = require_public_root(Path(public_root))
    if not isinstance(recorded_commit, str) or not FULL_SHA.fullmatch(recorded_commit):
        raise BaselineRefusal(
            "public_export_baseline_commit_invalid",
            f"recorded public_commit must be a full 40-hex commit id, got {recorded_commit!r}",
        )
    tip = resolve_commit(root, public_tip)
    recorded = resolve_commit(root, recorded_commit)
    completed = _run_git(root, "merge-base", "--is-ancestor", recorded, tip)
    if completed.returncode != 0:
        raise BaselineRefusal(
            "public_export_baseline_stale",
            f"the recorded public commit {recorded} is not an ancestor of the "
            f"published tip {tip}: the baseline was taken against a tree that is "
            "not behind what is published, so the fences are walking a population "
            "the exporter would not produce",
            REMEDY,
        )
    return tip


def companion_fixture(out: Path) -> Path | None:
    """The shipped fixture copy when `out` is a repository's committed baseline.

    Anchored on two fixed paths, never a scan: the fixture is written only when
    `out` sits at `<tree>/.github/public-export-baseline.v0.json` -- the
    committed location -- and `<tree>/tests/fixtures/baseline-1-f1/` already
    carries the fixture. A foreign tree, or a scratch `--out`, is untouched, and
    the fixture is never created where it does not already exist.
    """

    if out.parent.name != ".github" or out.name != Path(BASELINE_RELATIVE).name:
        return None
    candidate = out.parent.parent / FIXTURE_RELATIVE
    if candidate.is_file():
        return candidate
    return None


def regenerate(
    public_root: Path, public_commit: str | None, out: Path
) -> dict[str, object]:
    root = require_public_root(Path(public_root))
    require_clean(root)
    if public_commit is None:
        commit = resolve_commit(root, "HEAD")
        source = "clone-head"
    else:
        commit = resolve_commit(root, public_commit)
        source = "argument"
    paths = public_paths(root, commit)
    document = build_document(paths, commit)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_document(document)
    out.write_text(rendered, encoding="utf-8")
    fixture = companion_fixture(out)
    if fixture is not None:
        fixture.write_text(rendered, encoding="utf-8")
    return {
        "fixture": str(fixture) if fixture is not None else None,
        "out": str(out),
        "path_count": len(paths),
        "public_commit": commit,
        "public_commit_source": source,
    }


def _artifact(status: str, evidence: Mapping[str, object]) -> str:
    return json.dumps(
        {
            "artifact_version": 0,
            "command": COMMAND,
            "evidence": evidence,
            "status": status,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description=(
            "Regenerate the published-export baseline from a local clone of the "
            "published repository. Reads the clone only; makes no network call."
        ),
    )
    parser.add_argument(
        "--public-root", required=True, help="absolute path to a clean public clone"
    )
    parser.add_argument(
        "--public-commit",
        default=None,
        help="the published commit to record; defaults to the clone's HEAD, and "
        "the artifact says which was used",
    )
    parser.add_argument(
        "--out", required=True, help=f"where to write the document (usually {BASELINE_RELATIVE})"
    )
    args = parser.parse_args(argv)
    try:
        evidence = regenerate(Path(args.public_root), args.public_commit, Path(args.out))
    except BaselineRefusal as exc:
        print(_artifact("refused", {"code": exc.code, "detail": exc.detail, "remedy": exc.remedy}))
        return 20
    print(_artifact("ok", evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
