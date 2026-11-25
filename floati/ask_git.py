"""Verify the tree and document coordinates cited by an ask and its answer."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .git_process import fixed_git_command, fixed_git_environment


_GIT = "/usr/bin/git"
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def compare_ask_commits(repository: str | None, question: dict, answer: dict) -> dict:
    if question["repo"] != answer["repo"]:
        return {"status": "missing", "reason": "ask_git_different_repositories"}
    if repository is None:
        return {"status": "missing", "reason": "ask_git_repository_not_supplied"}
    try:
        path = Path(repository)
        if not path.is_absolute():
            return {"status": "missing", "reason": "ask_git_repository_invalid"}
        path = path.resolve(strict=True)
        before_tree, before_doc = _inspect_commit(path, question)
        after_tree, after_doc = _inspect_commit(path, answer)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {"status": "missing", "reason": "ask_git_objects_unavailable"}

    if question["doc"] != answer["doc"]:
        document_change = "different_paths"
    elif before_doc is None and after_doc is None:
        document_change = "absent"
    elif before_doc is None:
        document_change = "added"
    elif after_doc is None:
        document_change = "removed"
    else:
        document_change = "unchanged" if before_doc == after_doc else "modified"
    return {
        "status": "tree_changed" if before_tree != after_tree else "tree_unchanged",
        "question": _coordinate(before_tree, before_doc),
        "answer": _coordinate(after_tree, after_doc),
        "document_change": document_change,
    }


def _coordinate(tree, document):
    result = {"tree": tree}
    if document is not None:
        result["document_object"] = document[1]
    return result


def _inspect_commit(path, reference):
    sha = reference["sha"]
    document = reference["doc"]
    commit, tree = _git(path, (
        "show", "--no-patch", "--no-notes", "--no-show-signature", "--no-decorate",
        "--format=%H%n%T", sha, "--",
    )).decode("ascii").splitlines()
    if commit != sha or not _OBJECT_ID.fullmatch(tree):
        raise ValueError("the exact commit and its tree are required")
    entry = _git(path, ("ls-tree", "--full-tree", "-z", tree, "--", ":(literal)" + document))
    if not entry:
        return tree, None
    metadata, name = entry.removesuffix(b"\0").split(b"\t", 1)
    mode, _kind, identity = metadata.decode("ascii").split()
    if name != os.fsencode(document) or not _OBJECT_ID.fullmatch(identity):
        raise ValueError("the exact document coordinate is required")
    return tree, (mode, identity)


def _git(path, arguments):
    environment = fixed_git_environment(_GIT)
    environment.update(GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1",
                       GIT_CEILING_DIRECTORIES=os.fspath(path.parent))
    return subprocess.run(
        fixed_git_command(_GIT, path, arguments), env=environment, check=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
    ).stdout
