#!/usr/bin/env python3
"""Confirm one public export commit against the harbor source it names.

The read-back half of the export provenance pair: prepare_public_export.py
writes the pointer (subject ``export: project harbor <12>``, body lines
``Source-SHA:`` and ``Manifest-Digest:``); this script takes a public commit,
prints the harbor SHA it was projected from, and verifies the pairing by
blob identity. Every public path must exist in the harbor source tree (a
projection invents nothing); blobs equal at both sides are verbatim carries,
blobs that differ were adapted at exposure and are counted and named, never
silently accepted. Refusals are typed and fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

COMMAND = "confirm-public-provenance"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
FULL_DIGEST = re.compile(r"^[0-9a-f]{64}$")
SOURCE_LINE = re.compile(r"^Source-SHA: ([0-9a-f]{40})$", re.MULTILINE)
DIGEST_LINE = re.compile(r"^Manifest-Digest: ([0-9a-f]{64})$", re.MULTILINE)
SUBJECT_HARBOR = re.compile(r"harbor ([0-9a-f]{12})\b")
BASELINE_PATH = ".github/public-export-baseline.v0.json"


class ConfirmationRefusal(Exception):
    def __init__(self, code: str, detail: str, extra: Mapping[str, object] | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = dict(extra or {})


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


def _environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat"})
    return environment


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=root,
        env=_environment(),
        check=False,
        capture_output=True,
        timeout=60,
    )


def _git(root: Path, *arguments: str) -> bytes:
    completed = _run_git(root, *arguments)
    if completed.returncode != 0:
        raise ConfirmationRefusal(
            "provenance_git_unavailable",
            "bounded local Git command failed: " + completed.stderr.decode("utf-8", "replace").strip(),
        )
    return completed.stdout


def _git_text(root: Path, *arguments: str) -> str:
    return _git(root, *arguments).decode("utf-8").strip()


def _require_repo_root(root: Path, role: str) -> None:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ConfirmationRefusal(
            "provenance_root_invalid",
            f"{role} root must be an absolute non-symlink directory",
        )
    if _git_text(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise ConfirmationRefusal(
            "provenance_root_invalid",
            f"{role} root must be a Git worktree",
        )


def _resolve_commit(root: Path, commit: str, role: str, code: str) -> None:
    completed = _run_git(root, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if completed.returncode != 0:
        raise ConfirmationRefusal(
            code,
            f"{role} repository does not contain commit {commit}",
        )


def _source_sha(public: Path, public_commit: str) -> str:
    subject = _git_text(public, "log", "-1", "--format=%s", public_commit)
    body = _git_text(public, "log", "-1", "--format=%B", public_commit)
    found = sorted(set(SOURCE_LINE.findall(body)))
    if not found:
        raise ConfirmationRefusal(
            "provenance_source_sha_absent",
            "public commit carries no Source-SHA body line; only commits prepared by "
            "prepare-public-export carry provenance (a merge of such a branch does not)",
        )
    if len(found) > 1:
        raise ConfirmationRefusal(
            "provenance_source_sha_ambiguous",
            f"public commit carries {len(found)} distinct Source-SHA lines",
        )
    source = found[0]
    subject_match = SUBJECT_HARBOR.search(subject)
    if subject_match is None or subject_match.group(1) != source[:12]:
        raise ConfirmationRefusal(
            "provenance_subject_mismatch",
            f"subject does not name harbor-{source[:12]}",
        )
    return source


def _manifest_digest(body: str) -> str | None:
    found = sorted(set(DIGEST_LINE.findall(body)))
    if len(found) != 1:
        return None
    return found[0]


def _tree(root: Path, commit: str) -> dict[str, str]:
    text = _git_text(root, "ls-tree", "-r", commit, "--format=%(objectname) %(path)")
    tree: dict[str, str] = {}
    for line in text.splitlines():
        blob, _, path = line.partition(" ")
        if not blob or not path or path in tree:
            raise ConfirmationRefusal(
                "provenance_tree_unreadable",
                f"could not read a clean recursive tree for {commit}",
            )
        tree[path] = blob
    return tree


def _baseline(harbor: Path, source: str) -> str | None:
    completed = _run_git(harbor, "show", f"{source}:{BASELINE_PATH}")
    if completed.returncode != 0:
        return None
    try:
        baseline = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(baseline, dict) or not isinstance(baseline.get("public_commit"), str):
        return None
    return baseline["public_commit"]


def confirm(public_commit: str, public: Path, harbor: Path) -> dict[str, object]:
    subject = _git_text(public, "log", "-1", "--format=%s", public_commit)
    body = _git_text(public, "log", "-1", "--format=%B", public_commit)
    source = _source_sha(public, public_commit)
    _resolve_commit(harbor, source, "source", "provenance_source_unresolved")
    public_tree = _tree(public, public_commit)
    harbor_tree = _tree(harbor, source)
    absent = sorted(path for path in public_tree if path not in harbor_tree)
    if absent:
        raise ConfirmationRefusal(
            "provenance_path_unsourced",
            f"{len(absent)} public path(s) absent from the harbor source tree",
            {"absent_paths": absent},
        )
    projected = sorted(path for path in public_tree if harbor_tree[path] != public_tree[path])
    verbatim_count = len(public_tree) - len(projected)
    baseline_public_commit = _baseline(harbor, source)
    manifest_digest = _manifest_digest(body)
    return {
        "absent_paths": [],
        "baseline_matches_public_commit": baseline_public_commit == public_commit,
        "baseline_public_commit": baseline_public_commit,
        "harbor_source_commit": source,
        "manifest_digest": manifest_digest,
        "manifest_digest_wellformed": manifest_digest is not None,
        "path_total": len(public_tree),
        "projected_count": len(projected),
        "projected_paths": projected,
        "public_commit": public_commit,
        "verbatim_count": verbatim_count,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--public-root", required=True)
    parser.add_argument("--public-commit", required=True)
    parser.add_argument("--harbor-root", required=True)
    args = parser.parse_args(argv)
    try:
        if not FULL_SHA.fullmatch(args.public_commit):
            raise ConfirmationRefusal(
                "provenance_commit_invalid",
                "--public-commit must be a full 40-hex commit id",
            )
        public = Path(args.public_root)
        harbor = Path(args.harbor_root)
        _require_repo_root(public, "public")
        _require_repo_root(harbor, "harbor")
        _resolve_commit(public, args.public_commit, "public", "provenance_public_commit_unresolved")
        evidence = confirm(args.public_commit, public, harbor)
    except ConfirmationRefusal as exc:
        refusal_evidence = {"code": exc.code, "detail": exc.detail, **exc.extra}
        print(_artifact("refused", refusal_evidence))
        return 20
    print(_artifact("ok", evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
