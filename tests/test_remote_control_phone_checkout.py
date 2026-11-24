"""Execute the supported phone recipe against dirty, leased scratch repositories."""
from __future__ import annotations

import tests  # Pin HOME/XDG before fixture or product imports.

from pathlib import Path
import re
import shlex
import subprocess
import unittest

from floati.cursor_workers import project_remote_control_worker_findings
from tests import test_remote_control_coexist as coexist_fixtures


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_PLACEHOLDER = "/absolute/seat/repo"
PHONE_PLACEHOLDER = "/absolute/sibling-phone-clone"


class PhoneCheckoutRecipeTests(unittest.TestCase):
    def setUp(self):
        self.leases = coexist_fixtures.RemoteControlCensusUnitTests()
        self.leases.setUp()
        self.addCleanup(self.leases.doCleanups)
        self.fixture = self.leases.fixture
        self.seat_home = self.fixture.base / "leased seat home"
        self.seat = self.seat_home / "repo with spaces"
        self.phone = self.fixture.base / "sibling phone clone with spaces"
        self.seat.mkdir(parents=True)
        self.git(self.seat, "init", "--initial-branch=main")
        self.configure_author(self.seat)
        (self.seat / "work.txt").write_text("committed source\n")
        self.git(self.seat, "add", "work.txt")
        self.git(self.seat, "commit", "-m", "fixture seed")
        self.git(self.seat, "remote", "add", "upstream", "https://example.invalid/unused")
        (self.seat / "work.txt").write_text("protected dirty source\n")
        (self.seat / "untracked.txt").write_text("protected untracked source\n")
        self.leases.plant_lease(self.seat)

    def git(self, directory, *arguments, check=True):
        result = subprocess.run(
            ["/usr/bin/git", *arguments], cwd=directory,
            text=True, capture_output=True, timeout=20,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def configure_author(self, directory):
        self.git(directory, "config", "user.name", "Phone fixture")
        self.git(directory, "config", "user.email", "fixture@example.invalid")

    def recipe(self):
        text = (REPOSITORY / "README.md").read_text()
        match = re.search(
            r"<!-- floati-phone-checkout-recipe -->\s*```(?:sh|bash)\n([^`]+)\n```", text,
        )
        self.assertIsNotNone(
            match, "README offers no executable supported non-leased phone checkout recipe",
        )
        words = shlex.split(match.group(1))
        self.assertEqual(words[:2], ["git", "clone"], "the recipe must be one Git clone command")
        self.assertEqual(words.count(SOURCE_PLACEHOLDER), 1)
        self.assertEqual(words.count(PHONE_PLACEHOLDER), 1)
        return [str(self.seat) if word == SOURCE_PLACEHOLDER else
                str(self.phone) if word == PHONE_PLACEHOLDER else word
                for word in words[1:]]

    def clone(self, check=True):
        return self.git(self.fixture.base, *self.recipe(), check=check)

    def source_snapshot(self):
        return (
            self.git(self.seat, "rev-parse", "HEAD").stdout,
            self.git(self.seat, "for-each-ref", "--format=%(refname) %(objectname)").stdout,
            (self.seat / "work.txt").read_bytes(),
            (self.seat / "untracked.txt").read_bytes(),
        )

    def common_directory(self, directory):
        return Path(self.git(directory, "rev-parse", "--path-format=absolute",
                             "--git-common-dir").stdout.strip()).resolve()

    def test_recipe_creates_independent_storage_and_one_fetch_only_remote(self):
        self.clone()
        source_common = self.common_directory(self.seat)
        phone_common = self.common_directory(self.phone)
        self.assertNotEqual(source_common, phone_common)
        self.assertNotIn(self.seat_home.resolve(), self.phone.resolve().parents)
        self.assertFalse((phone_common / "objects/info/alternates").exists())
        object_sets = []
        for common in (source_common, phone_common):
            identities = {(path.stat().st_dev, path.stat().st_ino)
                          for path in (common / "objects").rglob("*") if path.is_file()}
            self.assertTrue(identities, "object independence cannot pass on empty stores")
            object_sets.append(identities)
        self.assertFalse(object_sets[0] & object_sets[1], "source and phone share object inodes")
        remotes = self.git(self.phone, "remote").stdout.splitlines()
        self.assertEqual(remotes, ["seat"], "the documented fetch remote must be the only remote")
        self.assertEqual(self.git(self.phone, "remote", "get-url", remotes[0]).stdout.strip(),
                         str(self.seat))
        self.assertEqual(self.git(self.phone, "remote", "get-url", "--push", remotes[0]).stdout.strip(),
                         "file:///dev/null")
        self.assertEqual((self.phone / "work.txt").read_text(), "committed source\n")
        self.assertFalse((self.phone / "untracked.txt").exists())

    def test_fetch_advances_only_remote_history_and_ordinary_push_cannot_write_source(self):
        self.clone()
        remote = "seat"
        phone_head = self.git(self.phone, "rev-parse", "HEAD").stdout
        (self.seat / "work.txt").write_text("next committed source\n")
        self.git(self.seat, "add", "work.txt")
        self.git(self.seat, "commit", "-m", "fixture source advance")
        source_head = self.git(self.seat, "rev-parse", "HEAD").stdout
        (self.seat / "work.txt").write_text("protected dirty source after advance\n")
        before = self.source_snapshot()
        self.git(self.phone, "fetch", remote)
        self.assertEqual(self.git(self.phone, "rev-parse", f"refs/remotes/{remote}/main").stdout,
                         source_head)
        self.assertEqual(self.git(self.phone, "rev-parse", "HEAD").stdout, phone_head)
        self.configure_author(self.phone)
        (self.phone / "work.txt").write_text("phone-only proposal\n")
        self.git(self.phone, "add", "work.txt")
        self.git(self.phone, "commit", "-m", "fixture phone proposal")
        self.assertEqual(self.git(self.phone, "remote", "get-url", "--push", remote).stdout.strip(),
                         "file:///dev/null", "never run a push to an unreviewed destination")
        pushed = self.git(self.phone, "push", remote, "HEAD:refs/heads/phone-proposal", check=False)
        self.assertNotEqual(pushed.returncode, 0)
        self.assertEqual(self.source_snapshot(), before)

    def test_worker_reset_and_checkout_preserve_dirty_and_untracked_seat_work(self):
        before = self.source_snapshot()
        self.clone()
        (self.phone / "work.txt").write_text("anonymous worker edit\n")
        self.git(self.phone, "checkout", "--", "work.txt")
        (self.phone / "work.txt").write_text("another worker edit\n")
        self.git(self.phone, "reset", "--hard", "HEAD")
        self.assertEqual(self.source_snapshot(), before)

    def test_nonempty_phone_destination_refuses_and_preserves_both_sides(self):
        self.phone.mkdir()
        marker = self.phone / "foreign.txt"
        marker.write_bytes(b"keep existing destination\n")
        before = self.source_snapshot()
        result = self.clone(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_bytes(), b"keep existing destination\n")
        self.assertFalse((self.phone / ".git").exists())
        self.assertEqual(self.source_snapshot(), before)

    def test_phone_is_outside_lease_and_recipe_does_not_register_or_rebind_seat(self):
        registry = self.fixture.root.resolve_relative("registry/entries.jsonl")
        before = registry.read_bytes()
        self.clone()
        outside = project_remote_control_worker_findings(
            self.fixture.root,
            source=self.leases.staged(
                "cursor-agent worker start --worker-dir " + shlex.quote(str(self.phone))),
        )
        inside = project_remote_control_worker_findings(
            self.fixture.root,
            source=self.leases.staged(
                "cursor-agent worker start --worker-dir " + shlex.quote(str(self.seat))),
        )
        self.assertEqual(outside[0]["remote_control_workers"]["classification"], 1)
        self.assertEqual(inside[0]["remote_control_workers"]["classification"], 2)
        self.assertEqual(registry.read_bytes(), before)
        self.assertNotIn(str(self.phone), outside[0]["detail"])


if __name__ == "__main__":
    unittest.main()
