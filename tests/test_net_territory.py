"""NET-TERRITORY-1: working paths are a leased liveness fact, read as advisory overlap.

The row (docs/status/FLOATI-NETWORK-2026-10-16.md section 3), narrowed by the
2026-11-22 field-research ruling: a lease records who holds which paths until
when, and a dispatch that names its edit paths carries an advisory preview of
the live leases and observed changes it would overlap. Nothing is refused and
no override exists. Paths compare canonically (symlinks, dot segments, the
filesystem's own case rule, repository boundaries, and the other worktrees of
one repository), and an expired or released lease is not a live territory.

A new module because no existing one covers territories or a dispatch
preview: tests/test_overlap_radar.py pins the read-only R1 fact this row reads.

Every assertion reads what a caller sees: the command artifact, its exit code
and the work items that exist afterwards.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.git_process import fixed_git_command, fixed_git_environment
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.temp_roots import REAL_TEMP_ROOT


def _cli(*argv: str) -> tuple[int, dict]:
    from floati.cli import main

    output = io.StringIO()
    errors = io.StringIO()
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(list(argv))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    try:
        artifact = json.loads(output.getvalue())
    except json.JSONDecodeError:
        artifact = {}
    return code, artifact


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        fixed_git_command("/usr/bin/git", repository, arguments),
        env=fixed_git_environment("/usr/bin/git"),
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _commit(repository: Path, relative: str, content: str, message: str) -> str:
    target = repository / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Floati Fixture",
         "-c", "user.email=floati-fixture@example.invalid",
         "commit", "-q", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


SERVICE = "def shared():\n    return '{}'\n\n\ndef stable():\n    return True\n"


class TerritoryFixture(unittest.TestCase):
    """One fleet, one repository with two worktrees, one plain directory."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        cls.base = Path(cls.temporary.name)
        cls.repo = cls.base / "repo"
        cls.repo.mkdir()
        _git(cls.repo, "init", "-q", "-b", "main")
        _commit(cls.repo, "docs/notes.md", "notes\n", "base docs")
        cls.base_sha = _commit(cls.repo, "pkg/service.py", SERVICE.format("base"), "base")
        cls.holder_sha = _commit(cls.repo, "pkg/service.py", SERVICE.format("holder"), "holder")
        cls.other = cls.base / "repo-b"
        _git(cls.repo, "worktree", "add", "-q", "-b", "owner", str(cls.other), cls.base_sha)
        cls.owner_sha = _commit(cls.other, "pkg/service.py", SERVICE.format("owner"), "owner")
        cls.plain = cls.base / "plain"
        (cls.plain / "inner").mkdir(parents=True)
        cls.nest = cls.base / "nest"
        (cls.nest / "inner").mkdir(parents=True)
        _git(cls.nest / "inner", "init", "-q", "-b", "main")
        cls.outer = cls.base / "outer"
        (cls.outer / "repo2" / "lib").mkdir(parents=True)
        _git(cls.outer / "repo2", "init", "-q", "-b", "main")
        cls.alias = cls.base / "alias"
        cls.alias.symlink_to(cls.repo, target_is_directory=True)
        probe = cls.base / "CaseProbe"
        probe.mkdir()
        cls.case_insensitive = (cls.base / "caseprobe").exists()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.scratch.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.scratch.name) / "fleet", create=True)
        self.holder = public_ids.builder("a")
        self.owner = public_ids.builder("b")
        self.architect = public_ids.builder("c")
        registry = Registry(self.root)
        for node in (self.holder, self.owner, self.architect):
            registry.register(node, "zcode")
        self.rootarg = str(self.root.path)

    def declare(self, node: str, *paths: Path, now: str | None = None,
                key: str = "territory-1", ttl: int = 3600) -> tuple[int, dict]:
        argv = ["territory", "declare", "--root", self.rootarg, "--as", node,
                "--ttl-seconds", str(ttl), "--idempotency-key", key]
        for path in paths:
            argv += ["--path", str(path)]
        if now is not None:
            argv += ["--now", now]
        return _cli(*argv)

    def preview(self, *edits: Path) -> tuple[int, dict]:
        argv = ["territory", "preview", "--root", self.rootarg, "--owner", self.owner]
        for path in edits:
            argv += ["--edits", str(path)]
        return _cli(*argv)

    def dispatch(self, title: str, *edits: Path) -> tuple[int, dict]:
        argv = ["work", "add", "--root", self.rootarg, "--title", title, "--owner", self.owner]
        for path in edits:
            argv += ["--edits", str(path)]
        return _cli(*argv)

    def advised_holders(self, *edits: Path) -> list[str]:
        """Dispatch and return the holders its advisory preview names; it never refuses."""
        self.dispatches = getattr(self, "dispatches", 0) + 1
        code, added = self.dispatch("advised dispatch %d" % self.dispatches, *edits)
        self.assertEqual((0, "ok"), (code, added.get("status")), added)
        return [c["holder"] for c in added["evidence"]["territory"]["conflicts"]]

    def titles(self) -> list[str]:
        return [str(item["title"]) for item in WorkLog(self.root).show()]


class TerritoryAdvisoryTests(TerritoryFixture):
    def test_unreadable_territory_history_does_not_prevent_dispatch(self) -> None:
        from floati.territory_records import TERRITORY_RELATIVE

        edit = self.plain / "inner" / "file.txt"
        code, declared = self.declare(self.holder, self.plain)
        self.assertEqual(0, code, declared)
        with (self.root.tenant_home / TERRITORY_RELATIVE).open("ab") as stream:
            stream.write(b"{")
        code, preview = self.preview(edit)
        self.assertEqual((33, "malformed_evidence"), (code, preview["status"]), preview)

        code, added = self.dispatch("edit with unavailable territory history", edit)
        self.assertEqual((0, "ok"), (code, added["status"]), added)
        self.assertEqual(["edit with unavailable territory history"], self.titles())
        advisory = added["evidence"]["territory"]
        self.assertTrue(advisory["advisory"])
        self.assertEqual(("UNKNOWN", preview["evidence"]["code"]),
                         (advisory["stamp"], advisory["reason"]))

    def test_dispatch_into_a_live_territory_carries_the_overlap_preview_and_is_never_refused(self) -> None:
        code, declared = self.declare(self.holder, self.repo / "pkg")
        self.assertEqual((0, "ok"), (code, declared.get("status")), declared)
        territory = declared["evidence"]
        for entry in territory["paths"]:
            # The contract pins a path's shape, never a host prefix; the
            # repository-relative path is taken at claim time.
            self.assertTrue(os.path.isabs(entry["path"]), entry)
            self.assertEqual(os.path.normpath(entry["path"]), entry["path"])
            self.assertEqual("pkg", entry["relative"])

        edit = self.other / "pkg" / "service.py"
        code, added = self.dispatch("edit service", edit)
        self.assertEqual((0, "ok"), (code, added.get("status")), added)
        preview = added["evidence"]["territory"]
        self.assertTrue(preview["advisory"])
        self.assertEqual([territory["id"]], [c["territory_id"] for c in preview["conflicts"]])
        conflict = preview["conflicts"][0]
        self.assertEqual(self.holder, conflict["holder"])
        self.assertEqual("pkg/service.py", conflict["signal"]["coordinate"])
        self.assertFalse(conflict["signal"]["hard_lock"])
        # The preview carries the existing `floati overlap` fact for the two lines of work.
        report = conflict["overlap"]["report"]
        self.assertEqual([self.base_sha, self.holder_sha, self.owner_sha],
                         [report["inputs"][k] for k in ("base_sha", "left_sha", "right_sha")])
        self.assertIn("pkg/service.py:shared", [s["coordinate"] for s in report["signals"]])
        self.assertEqual(["edit service"], self.titles())

        code, previewed = self.preview(edit)
        self.assertEqual((0, "ok"), (code, previewed.get("status")), previewed)
        self.assertEqual(preview["conflicts"], previewed["evidence"]["conflicts"])

        # No override flow exists.
        code, absent = _cli("territory", "override", "--root", self.rootarg)
        self.assertEqual((20, "arguments_invalid"), (code, absent["evidence"].get("code")), absent)

    def test_paths_compare_canonically(self) -> None:
        code, declared = self.declare(self.holder, self.repo / "pkg", self.plain, self.nest,
                                      self.outer / "repo2" / "lib")
        self.assertEqual(0, code, declared)
        code, own = self.declare(self.owner, self.other / "docs", key="territory-own")
        self.assertEqual(0, code, own)
        cases = (
            ("same file", self.repo / "pkg" / "service.py", True),
            ("symlinked spelling", self.alias / "pkg" / "service.py", True),
            ("dot segments", Path(str(self.repo / "docs" / ".." / "pkg" / "new.py")), True),
            ("another worktree of the repository", self.other / "pkg" / "service.py", True),
            ("whole repository contains the territory", self.other, True),
            ("plain directory", self.plain / "inner" / "file.txt", True),
            ("a repository inside a plain territory", self.nest / "inner" / "src" / "x.py", True),
            ("a plain directory holding a territory's repository", self.outer, True),
            ("case variant", self.repo / "PKG" / "service.py", self.case_insensitive),
            ("disjoint path", self.repo / "docs" / "notes.md", False),
            ("sibling with a shared name prefix", self.base / "plainer", False),
            ("the owner's own territory", self.other / "docs" / "notes.md", False),
        )
        for label, edit, collides in cases:
            with self.subTest(label):
                code, previewed = self.preview(edit)
                self.assertEqual(0, code, previewed)
                holders = [c["holder"] for c in previewed["evidence"]["conflicts"]]
                self.assertEqual([self.holder] if collides else [], holders)

    def test_only_a_live_lease_is_a_territory(self) -> None:
        edit = self.repo / "pkg" / "service.py"
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        code, expired = self.declare(self.holder, self.repo / "pkg", now=past, key="expired")
        self.assertEqual(0, code, expired)
        self.assertEqual([], self.advised_holders(edit))

        code, live = self.declare(self.holder, self.repo / "pkg", key="live")
        self.assertEqual(0, code, live)
        self.assertEqual([self.holder], self.advised_holders(edit))
        code, released = _cli("territory", "release", "--root", self.rootarg, "--as", self.holder,
                              "--territory", live["evidence"]["id"], "--idempotency-key", "release-1")
        self.assertEqual((0, "ok"), (code, released.get("status")), released)
        self.assertEqual([], self.advised_holders(edit))

        code, again = self.declare(self.holder, self.repo / "pkg", key="again")
        self.assertEqual(0, code, again)
        code, retired = _cli("retire", "--root", self.rootarg, self.holder)
        self.assertEqual(0, code, retired)
        self.assertEqual([], self.advised_holders(edit))
        self.assertEqual(4, len(self.titles()))

    def test_an_overlapping_declaration_is_listed_never_refused(self) -> None:
        code, declared = self.declare(self.holder, self.repo / "pkg")
        self.assertEqual(0, code, declared)
        code, nested = self.declare(self.owner, self.other / "pkg" / "service.py", key="nested")
        self.assertEqual((0, "ok"), (code, nested.get("status")), nested)
        self.assertEqual([declared["evidence"]["id"]],
                         [o["territory_id"] for o in nested["evidence"]["advisory"]["overlapping_leases"]])
        code, shown = _cli("territory", "show", "--root", self.rootarg)
        self.assertEqual(0, code, shown)
        self.assertEqual([(self.holder, "live"), (self.owner, "live")],
                         [(t["node_id"], t["state"]) for t in shown["evidence"]["territories"]])


class ObservedOverlapTests(TerritoryFixture):
    """Overlap observed from what each worktree changed, with nothing registered."""

    def fresh_repository(self) -> dict[str, Path]:
        base = Path(self.scratch.name) / "observed"
        main = base / "main"
        main.mkdir(parents=True)
        _git(main, "init", "-q", "-b", "trunk")
        _commit(main, "pkg/f.py", SERVICE.format("base"), "base")
        _commit(main, "docs/x.md", "base\n", "docs")
        paths = {"main": main, "a": base / "wa", "b": base / "wb"}
        for name in ("a", "b"):
            _git(main, "worktree", "add", "-q", "-b", name, str(paths[name]), "trunk")
            _commit(paths[name], "pkg/f.py", SERVICE.format(name), name)
        _commit(paths["b"], "docs/x.md", "b\n", "b docs")
        (paths["a"] / "docs" / "x.md").write_text("a, uncommitted\n", encoding="utf-8")
        _git(main, "update-ref", "refs/remotes/origin/trunk", "trunk")
        _git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        return paths

    @staticmethod
    def observe(repository: Path, base_ref: str) -> tuple[int, dict]:
        return _cli("overlap", "observe", "--repository", str(repository), "--base-ref", base_ref)

    def test_overlap_observe_names_the_pair_and_paths_with_nothing_registered(self) -> None:
        repo = self.fresh_repository()
        code, observed = self.observe(repo["main"], "trunk")
        self.assertEqual((0, "ok"), (code, observed.get("status")), observed)
        evidence = observed["evidence"]
        self.assertEqual("participating_writers", evidence["coverage"]["ends_at"])
        names = {os.path.basename(w["worktree"]) for w in evidence["worktrees"]}
        self.assertEqual({"main", "wa", "wb"}, names)
        pairs = [(sorted(os.path.basename(p) for p in (pair["left"], pair["right"])), pair["paths"])
                 for pair in evidence["pairs"]]
        # The uncommitted edit in wa counts: observed is what the worktree holds.
        self.assertEqual([(["wa", "wb"], ["docs/x.md", "pkg/f.py"])], pairs)

    def test_observed_overlap_expires_when_a_branch_lands(self) -> None:
        repo = self.fresh_repository()
        _git(repo["main"], "merge", "-q", "--ff-only", "b")
        code, observed = self.observe(repo["main"], "trunk")
        self.assertEqual(0, code, observed)
        self.assertEqual([], observed["evidence"]["pairs"])

    def test_dispatch_preview_shows_observed_changes_without_any_lease(self) -> None:
        repo = self.fresh_repository()
        code, added = self.dispatch("edit f", repo["b"] / "pkg" / "f.py")
        self.assertEqual((0, "ok"), (code, added.get("status")), added)
        preview = added["evidence"]["territory"]
        self.assertEqual([], preview["conflicts"])
        self.assertEqual("participating_writers", preview["coverage"]["ends_at"])
        seen = [(os.path.basename(o["worktree"]), o["paths"], o["basis"]) for o in preview["observed"]]
        self.assertEqual([("wa", ["pkg/f.py"], "observed")], seen)
        # Without a base the observed read is a typed absence, never an empty claim.
        code, unknown = self.preview(self.other / "pkg" / "service.py")
        self.assertEqual(0, code, unknown)
        absences = unknown["evidence"]["observed_absences"]
        self.assertEqual(["base_ref_unknown"], [a["reason"] for a in absences])
        code, based = _cli("territory", "preview", "--root", self.rootarg, "--owner", self.owner,
                           "--edits", str(self.other / "pkg" / "service.py"),
                           "--base-ref", self.base_sha)
        self.assertEqual(0, code, based)
        self.assertEqual(["repo"], [os.path.basename(o["worktree"]) for o in based["evidence"]["observed"]])

    def test_lease_paths_are_repository_relative_from_claim_time(self) -> None:
        repo = self.fresh_repository()
        code, declared = self.declare(self.holder, repo["b"] / "pkg")
        self.assertEqual(0, code, declared)
        _git(repo["main"], "worktree", "remove", "--force", str(repo["b"]))
        self.assertEqual([self.holder], self.advised_holders(repo["a"] / "pkg" / "f.py"))


if __name__ == "__main__":
    unittest.main()
