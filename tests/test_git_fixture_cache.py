"""The shared git fixture cache hands every caller an independent, clean copy.

A template cache is only a speed-up if no test can observe it. These cases pin
the three ways one could leak: a caller mutating its copy reaching another
caller or the template, a copied index reporting tracked files as modified,
and two different inputs sharing one template.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import git_fixture_cache
from tests.temp_roots import REAL_TEMP_ROOT


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        [git_fixture_cache.fixture_git(), "-C", str(root), *arguments],
        env=environment, capture_output=True, text=True,
    )


BUILDS = []


def _build(root: Path, text: str = "template\n") -> str:
    BUILDS.append(root)
    _git(root, "init", "--quiet", "--initial-branch=main").check_returncode()
    _git(root, "config", "user.name", "fixture").check_returncode()
    _git(root, "config", "user.email", "fixture@example.invalid").check_returncode()
    (root / "README.md").write_text(text, encoding="utf-8")
    _git(root, "add", "README.md").check_returncode()
    _git(root, "commit", "--quiet", "-m", "fixture").check_returncode()
    return _git(root, "rev-parse", "HEAD").stdout.strip()


class GitFixtureCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.key = ("test_git_fixture_cache", self.id())

    def test_each_copy_is_independent_of_the_template_and_of_each_other(self):
        first_sha = git_fixture_cache.cached_tree(self.base / "first", self.key, _build)
        second_sha = git_fixture_cache.cached_tree(self.base / "second", self.key, _build)
        self.assertEqual(first_sha, second_sha)
        self.assertEqual(1, sum(1 for path in BUILDS if path.name == git_fixture_cache._digest(self.key, _build)))

        first, second = self.base / "first", self.base / "second"
        (first / "README.md").write_text("mutated by the first test\n", encoding="utf-8")
        (first / "added.txt").write_text("only in first\n", encoding="utf-8")
        _git(first, "add", "--all").check_returncode()
        _git(first, "commit", "--quiet", "-m", "first mutates").check_returncode()
        _git(first, "config", "user.name", "someone else").check_returncode()

        self.assertEqual("template\n", (second / "README.md").read_text(encoding="utf-8"))
        self.assertFalse((second / "added.txt").exists())
        self.assertEqual(second_sha, _git(second, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual("fixture", _git(second, "config", "user.name").stdout.strip())

        third = self.base / "third"
        self.assertEqual(first_sha, git_fixture_cache.cached_tree(third, self.key, _build))
        self.assertEqual("template\n", (third / "README.md").read_text(encoding="utf-8"))
        self.assertFalse((third / "added.txt").exists())
        self.assertEqual(first_sha, _git(third, "rev-parse", "HEAD").stdout.strip())

    def test_a_copy_is_stat_clean_for_plumbing_that_does_not_refresh(self):
        git_fixture_cache.cached_tree(self.base / "copy", self.key, _build)
        copy = self.base / "copy"
        self.assertEqual(0, _git(copy, "diff-files", "--quiet").returncode)
        self.assertEqual("", _git(copy, "status", "--porcelain").stdout)
        (copy / "README.md").write_text("changed\n", encoding="utf-8")
        self.assertEqual(1, _git(copy, "diff-files", "--quiet").returncode)

    def test_a_caller_owned_refresh_is_what_makes_the_copy_clean(self):
        copy = self.base / "deferred"
        git_fixture_cache.cached_tree(copy, self.key, _build, refresh_index=False)
        self.assertEqual(1, _git(copy, "diff-files", "--quiet").returncode)
        _git(copy, "update-index", "-q", "--refresh").check_returncode()
        self.assertEqual(0, _git(copy, "diff-files", "--quiet").returncode)

    def test_a_different_key_never_shares_a_template(self):
        one = git_fixture_cache.cached_tree(
            self.base / "one", self.key + ("one",), lambda root: _build(root, "one\n"))
        two = git_fixture_cache.cached_tree(
            self.base / "two", self.key + ("two",), lambda root: _build(root, "two\n"))
        self.assertNotEqual(one, two)
        self.assertEqual("one\n", (self.base / "one" / "README.md").read_text(encoding="utf-8"))
        self.assertEqual("two\n", (self.base / "two" / "README.md").read_text(encoding="utf-8"))

    def test_an_empty_destination_directory_is_accepted_and_a_full_one_refused(self):
        empty = self.base / "empty"
        empty.mkdir(mode=0o700)
        identity = empty.stat().st_ino
        git_fixture_cache.cached_tree(empty, self.key, _build)
        self.assertTrue((empty / ".git").is_dir())
        self.assertEqual("template\n", (empty / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(0, _git(empty, "diff-files", "--quiet").returncode)
        self.assertEqual(identity, empty.stat().st_ino, "the caller's directory was replaced")
        self.assertEqual(0o700, empty.stat().st_mode & 0o777)
        occupied = self.base / "occupied"
        occupied.mkdir()
        (occupied / "keep.txt").write_text("keep\n", encoding="utf-8")
        with self.assertRaises(OSError):
            git_fixture_cache.cached_tree(occupied, self.key, _build)
        self.assertEqual("keep\n", (occupied / "keep.txt").read_text(encoding="utf-8"))

    def test_fixture_git_resolves_to_the_same_git_build(self):
        resolved = git_fixture_cache.fixture_git("/usr/bin/git") if os.path.exists("/usr/bin/git") else git_fixture_cache.fixture_git()
        self.assertTrue(os.path.isabs(resolved), resolved)
        self.assertTrue(os.access(resolved, os.X_OK), resolved)
        if os.path.exists("/usr/bin/git"):
            shim = subprocess.run(["/usr/bin/git", "--version"], capture_output=True, text=True, check=True)
            real = subprocess.run([resolved, "--version"], capture_output=True, text=True, check=True)
            self.assertEqual(shim.stdout, real.stdout)
        if sys.platform != "darwin":
            self.assertEqual("/usr/bin/git", git_fixture_cache.fixture_git("/usr/bin/git"))


if __name__ == "__main__":
    unittest.main()
