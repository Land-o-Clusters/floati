"""Am10's read-only admission for the exact constant native verifier bank.

This is not a child I/O observer. No process is launched here. Whole-tree CPU
is collected by the owner; only parent Python bus bytes/flocks are observed.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import zlib


def require(value, detail):
    if not value:
        raise ValueError(detail)


def ordinary(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and path.resolve(strict=True) == path and info.st_uid == os.getuid(),
            "verification input is a foreign or aliased coordinate")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            "verification input has a foreign file type")
    if not directory:
        require(info.st_nlink == 1 and not info.st_mode & 0o022,
                "verification input is shared or writable by another user")
    return path


def read(path, limit=65536):
    path = ordinary(path)
    require(path.stat().st_size <= limit, "verification input exceeds its closed byte bound")
    return path.read_bytes()


def object_bytes(workspace, identifier, kind):
    require(type(identifier) is str and re.fullmatch(r"[0-9a-f]{40}", identifier),
            "verification object must use an exact SHA1")
    path = workspace / ".git/objects" / identifier[:2] / identifier[2:]
    compressed = read(path)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, 262145)
    require(len(raw) <= 262144 and decoder.eof and not decoder.unused_data
            and not decoder.unconsumed_tail and hashlib.sha1(raw).hexdigest() == identifier,
            "verification loose object is malformed or differs from its identity")
    header, body = raw.split(b"\0", 1)
    require(header == kind.encode() + b" " + str(len(body)).encode(),
            "verification object has a foreign kind or declared length")
    return body


def tree_entries(workspace, identifier, prefix=""):
    raw = object_bytes(workspace, identifier, "tree")
    rows, names = [], set()
    while raw:
        header, rest = raw.split(b"\0", 1)
        require(len(rest) >= 20, "verification tree has a truncated object identity")
        mode, name = header.split(b" ", 1)
        name = name.decode("ascii")
        require(name not in names and name not in {"", ".", ".."} and "/" not in name,
                "verification tree has an ambiguous path")
        names.add(name)
        blob, raw = rest[:20].hex(), rest[20:]
        relative = prefix + name
        if mode == b"40000" and relative == "tests":
            rows.extend(tree_entries(workspace, blob, "tests/"))
        else:
            require(mode == b"100644", "verification tree contains an unreviewed mode or subtree")
            rows.append({"path": relative, "mode": "100644", "kind": "blob", "blob": blob})
        require(len(rows) <= 3 and len(names) <= 3, "verification tree exceeds its exact population")
    return sorted(rows, key=lambda row: row["path"])


def test_source(note_digest):
    return ("import hashlib\nfrom pathlib import Path\nimport unittest\n\n"
            "class DeliveryArtifact(unittest.TestCase):\n"
            "    def test_owned_artifact(self):\n"
            "        self.assertEqual(" + repr(note_digest) + ", "
            "hashlib.sha256(Path('note.md').read_bytes()).hexdigest())\n").encode()


def require_loose_ref_namespace(workspace, sha):
    """Bound every ref consumed by native all-ref cleanup, without Git I/O."""
    require(not os.path.lexists(workspace / ".git/packed-refs"),
            "verification fixture must retain only its exact loose refs")
    # Native git init creates the empty tags container. It must remain empty;
    # the only actual refs are the selected local and remote main leaves.
    directories = {"refs": {"heads", "remotes", "tags"}, "refs/tags": set(), "refs/heads": {"main"},
                   "refs/remotes": {"origin"}, "refs/remotes/origin": {"main"}}
    for relative, names in directories.items():
        path = ordinary(workspace / ".git" / relative, directory=True)
        actual = set(os.listdir(path))
        require(not path.stat().st_mode & 0o022 and actual == names,
                "verification ref namespace contains an unreviewed or shared path: "
                + relative + " expected " + repr(sorted(names)) + " observed " + repr(sorted(actual)))
    for relative in ("refs/heads/main", "refs/remotes/origin/main"):
        require(read(workspace / ".git" / relative) == (sha + "\n").encode(),
                "verification loose ref differs from its exact selected commit")


def validate_inputs(fixture, workspace, context):
    from floati.records import validate_record
    fixture, workspace = ordinary(fixture, directory=True), ordinary(workspace, directory=True)
    temporary = Path("\x2fprivate/tmp" if os.uname().sysname == "Darwin" else "\x2ftmp")
    require(temporary in fixture.parents and temporary in workspace.parents
            and fixture != workspace and fixture not in workspace.parents and workspace not in fixture.parents
            and not fixture.stat().st_mode & 0o077 and not workspace.stat().st_mode & 0o077,
            "verification roots must be disjoint owned private temporary directories")
    require(type(context) is dict and context.get("case") == "verify"
            and context.get("seed_home") == str(fixture) and context.get("workspace") == str(workspace),
            "verification context names a foreign workload or root")
    ordinary(workspace / ".git", directory=True)
    require(set(os.listdir(workspace)) == {".git", "note.md", "tests"}
            and set(os.listdir(ordinary(workspace / "tests", directory=True))) ==
                {"__init__.py", "test_census_delivery.py"},
            "verification source bank contains an unreviewed import or runner path")
    note = read(workspace / "note.md", 4096)
    digest = hashlib.sha256(note).hexdigest()
    wanted = {"note.md": note, "tests/__init__.py": b"",
              "tests/test_census_delivery.py": test_source(digest)}
    claim, sha = context["claim"], context["verification_repo_sha"]
    validate_record(claim, fixture.name, {"delivery_claim"}, integrity=True)
    require(claim["sha"] == sha and claim["repo_path"] == str(workspace)
            and claim["bank"] == ["tests.test_census_delivery"]
            and claim["declared"] == {"ran": 1, "result": "OK"}
            and type(claim["declared"]["ran"]) is int and claim["deadline_seconds"] == 15
            and claim["artifacts"] == [{"path": "note.md", "sha256": digest}],
            "verification claim differs from the closed constant real artifact bank")
    proof = context["verification_tree"]
    require(type(proof) is list and len(proof) == 3 and {row["path"] for row in proof} == set(wanted),
            "verification source proof has an inexact population")
    for row in proof:
        path = workspace / row["path"]
        raw = read(path)
        expected = wanted[row["path"]]
        blob = hashlib.sha1(b"blob " + str(len(expected)).encode() + b"\0" + expected).hexdigest()
        require(row == {"path": row["path"], "mode": "100644", "kind": "blob", "blob": blob,
                       "bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest(),
                       "mode_bits": stat.S_IMODE(path.stat().st_mode)} and raw == expected,
                "verification source bytes, mode or Git blob differs from the native preparation")
        require(object_bytes(workspace, blob, "blob") == expected,
                "verification committed blob differs from its real source")
    git_files = context["verification_git_files"]
    require(type(git_files) is dict and set(git_files) ==
            {".git/HEAD", ".git/config", ".git/refs/remotes/origin/main"},
            "verification Git-input proof has an unreviewed coordinate")
    for relative, expected in git_files.items():
        raw = read(workspace / relative)
        require(expected == {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                "verification Git configuration or reference changed")
    require_loose_ref_namespace(workspace, sha)
    require(read(workspace / ".git/HEAD") == b"ref: refs/heads/main\n"
            and not os.path.lexists(workspace / ".git/objects/info/alternates"),
            "verification HEAD, selected remote or object repository changed")
    commit = object_bytes(workspace, sha, "commit")
    first = commit.split(b"\n", 1)[0]
    require(re.fullmatch(rb"tree [0-9a-f]{40}", first), "verification commit has no exact tree")
    actual_tree = tree_entries(workspace, first[5:].decode("ascii"))
    require(actual_tree == sorted([{key: row[key] for key in ("path", "mode", "kind", "blob")}
                                  for row in proof], key=lambda row: row["path"]),
            "verification complete committed tree differs from its pinned three-file bank")
    scratch = fixture / "scratch/verifications" / claim["id"]
    require(scratch.resolve() == scratch, "verification scratch coordinate is aliased")
    return sha, scratch


def verify_observer_scope(context):
    return {"bus_bytes": "parent_python_application_visible", "ledger_flock_hold": "parent_native_flock",
            "cpu": "whole_entry_child_tree",
            "child_bytes": {"status": "UNMEASURED", "reason": "not-ledger-addressed"},
            "git_locks": {"status": "UNMEASURED", "reason": "not-ledger-addressed"},
            **{key: copy.deepcopy(context[key]) for key in
               ("verification_repo_sha", "verification_tree", "verification_git_files")}}


def verify_subprocess(argv, kwargs, fixture, workspace, context):
    from floati.errors import IntegrityFailure, ProtocolRefusal
    from floati.git_process import GIT_MAINTENANCE_OPTIONS, fixed_git_command, fixed_git_environment
    from floati.verification import DeliveryVerifier
    from floati.worktree_safety import _SANDBOX_ENV
    try:
        if (not isinstance(argv, (tuple, list)) or not all(type(value) is str for value in argv)
                or type(kwargs) is not dict or set(kwargs) - {"env", "cwd", "stdin", "stdout", "stderr", "text"}
                or kwargs.get("stdout") != subprocess.PIPE or kwargs.get("stderr") != subprocess.PIPE):
            return False
        fixture, workspace = Path(fixture), Path(workspace)
        sha, scratch = validate_inputs(fixture, workspace, context)
        verifier_env = DeliveryVerifier._environment()
        hardened = [(workspace, ("cat-file", "-e", sha + "^{commit}")),
                    (workspace, ("for-each-ref", "--format=%(refname)", "--contains", sha, "refs/remotes")),
                    (workspace, ("worktree", "add", "--detach", str(scratch), sha)),
                    (scratch, ("status", "--porcelain", "--untracked-files=all")),
                    (workspace, ("worktree", "remove", str(scratch)))]
        if any(list(argv) == fixed_git_command("/usr/bin/git", repo, query) for repo, query in hardened):
            return (kwargs.get("env") == verifier_env and kwargs.get("cwd") is None
                    and kwargs.get("stdin") is None and "text" not in kwargs)
        python = (["python3", "-c", "import importlib,sys;[importlib.import_module(name) for name in sys.argv[1:]]",
                   "tests.test_census_delivery"], ["python3", "-m", "unittest", "tests.test_census_delivery"])
        if list(argv) in python:
            return (kwargs.get("env") == verifier_env and Path(kwargs.get("cwd", "/")) == scratch
                    and kwargs.get("stdin") is None and "text" not in kwargs)
        if list(argv) == fixed_git_command("/usr/bin/git", scratch, ("rev-parse", "--is-shallow-repository")):
            return (kwargs.get("env") == fixed_git_environment("/usr/bin/git") and kwargs.get("cwd") is None
                    and kwargs.get("stdin") is None and kwargs.get("text") is True)
        raw_queries = [(workspace, ("rev-parse", "--path-format=absolute", "--git-common-dir")),
                       (scratch, ("rev-parse", "--path-format=absolute", "--git-common-dir")),
                       (scratch, ("rev-parse", "--verify", "HEAD^{commit}")),
                       (scratch, ("rev-list", sha)),
                       (workspace, ("for-each-ref", "--format=%(refname)")),
                       (workspace, ("rev-list", "--stdin"))]
        for repo, query in raw_queries:
            if (list(argv) == ["/usr/bin/git", *GIT_MAINTENANCE_OPTIONS, *query]
                    and Path(kwargs.get("cwd", "/")) == repo):
                return (kwargs.get("env") == _SANDBOX_ENV and "text" not in kwargs
                        and kwargs.get("stdin") == (subprocess.PIPE if query == ("rev-list", "--stdin") else None))
        return False
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, zlib.error, IntegrityFailure, ProtocolRefusal):
        return False
