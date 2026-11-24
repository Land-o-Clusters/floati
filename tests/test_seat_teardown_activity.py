"""F2 activity proof uses scratch Git roots and owned native cwd controls."""
from __future__ import annotations

import importlib
import io
import json
from datetime import datetime, timezone
from functools import wraps
from types import SimpleNamespace
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import HOME_PIN_ROOT
from floati.errors import ProtocolRefusal
from floati.git_process import fixed_git_command, fixed_git_environment


def native_only(test):
    """Isolate Git proof, leaving the native metadata boundary under test.

    Actual scratch Git/lease construction still runs. The separate Git tests
    and supported-host composed qualification exercise real Git observation.
    """
    @wraps(test)
    def wrapped(self):
        module = self._module()
        def clean(path):
            return {"workspace": str(path), "head": "a" * 40, "tracked_changes": [],
                    "untracked_files": [], "unpushed_commits": []}
        with patch.object(module, "_inventory", return_value=[self.repo]), patch.object(module, "_worktree", side_effect=clean):
            return test(self)
    return wrapped


class DarwinPidMetadataTests(unittest.TestCase):
    """Only the fixed pid/ppid/state tool boundary is substituted."""

    def _snapshot(self, raw):
        module = importlib.import_module("floati.seat_teardown_activity")
        def observation(argv, **kwargs):
            if argv != ["/bin/ps", "-A", "-o", "pid=,ppid=,uid=,state="]:
                raise AssertionError("unexpected native observation fields")
            return subprocess.CompletedProcess(argv, 0, raw, b"")
        with patch.object(module, "_command", side_effect=observation), patch.object(os, "getuid", return_value=501):
            try:
                return module._darwin_pid_snapshot()
            except ValueError as exc:
                self.fail("Malformed metadata escaped the typed unknown boundary: " + str(exc))

    def _unknown(self, raw):
        with self.assertRaises(ProtocolRefusal) as caught:
            self._snapshot(raw)
        self.assertEqual("seat_teardown_activity_unknown", caught.exception.code)
        return caught.exception.detail

    def test_halted_state_is_readable_native_metadata(self):
        """Apple ps maps TH_STATE_HALTED to H; rejecting H loses valid ancestry."""
        try:
            result = self._snapshot(b"41 1 501 Hs+\n")
        except ProtocolRefusal as exc:
            self.fail("Valid halted metadata was refused: " + str(exc))
        self.assertEqual(({41: 1}, {41: "H"}, {41: 501}), result)

    def test_documented_state_modifiers_preserve_run_state(self):
        self.assertEqual(({41: 1}, {41: "S"}, {41: 501}), self._snapshot(b"41 1 501 S<NXEVLs+\n"))

    def test_unknown_state_keeps_readable_owner_and_parent_for_cwd_decision(self):
        """§23: ?E is neither completed exit nor proof of seat location."""
        self.assertEqual(({41: 1}, {41: "?"}, {41: 501}),
                         self._snapshot(b"41 1 501 ?E\n"))

    def test_malformed_row_diagnostic_does_not_echo_extra_fields(self):
        detail = self._unknown(b"41 1 501 S secret-commandline-token /private/credential\n")
        for field in ("row=1", "pid=41", "ppid=1", "state='S'"):
            self.assertIn(field, detail)
        self.assertNotIn("secret-commandline", detail)
        self.assertNotIn("credential", detail)

    def test_oversize_state_refuses_without_echoing_its_bytes(self):
        detail = self._unknown(b"41 1 501 S" + b"N" * 4096 + b"\n")
        self.assertIn("state=<invalid>", detail)
        self.assertLess(len(detail), 256)

    def test_non_ascii_pid_is_typed_unknown(self):
        detail = self._unknown("\u00b2 1 S\n".encode())
        self.assertIn("pid=<invalid>", detail)

    def test_invalid_state_token_and_identifiers_are_redacted(self):
        for raw in (b"credential-token 1 501 S\n", b"41 credential-token 501 S\n",
                    b"41 1 501 secret-commandline-token\n", b"41 1 501 S\x1b[31m\n"):
            with self.subTest(raw=raw):
                detail = self._unknown(raw)
                self.assertIn("<invalid>", detail)
                self.assertNotIn("credential", detail)
                self.assertNotIn("secret", detail)
                self.assertNotIn("\x1b", detail)

    def test_empty_missing_zero_negative_and_oversize_pid_refuse_unknown(self):
        for raw in (b"", b"41 1\n", b"0 1 501 S\n", b"41 -1 501 S\n",
                    b"9" * 5000 + b" 1 501 S\n"):
            with self.subTest(size=len(raw)):
                self.assertLess(len(self._unknown(raw)), 256)

    def test_duplicate_pid_names_exact_bounded_row(self):
        detail = self._unknown(b"41 1 501 S\n41 1 501 R\n")
        self.assertIn("Duplicate", detail)
        self.assertIn("row=2", detail)
        self.assertIn("pid=41", detail)

    def test_ps_metadata_retries_once_then_recovers(self):
        module = importlib.import_module("floati.seat_teardown_activity")
        argv = ["/bin/ps", "-A", "-o", "pid=,ppid=,uid=,state="]
        outcomes = iter((b"41 1 501 ?\n", b"41 1 501 S\n"))
        def observation(actual, **kwargs):
            self.assertEqual(argv, actual)
            return subprocess.CompletedProcess(actual, 0, next(outcomes), b"")
        with patch.object(os, "getuid", return_value=501), patch.object(module, "_command", side_effect=observation):
            self.assertEqual(({41: 1}, {41: "S"}, {41: 501}), module._darwin_pid_snapshot())
        with self.assertRaises(StopIteration):
            next(outcomes)

    def test_ps_second_malformed_observation_refuses_with_last_coordinates(self):
        module = importlib.import_module("floati.seat_teardown_activity")
        outcomes = iter((b"41 1 501 bad\n", b"42 1 501 bad\n"))
        def observation(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, next(outcomes), b"")
        with patch.object(os, "getuid", return_value=501), patch.object(module, "_command", side_effect=observation):
            with self.assertRaises(ProtocolRefusal) as caught:
                module._darwin_pid_snapshot()
        self.assertIn("pid=42", caught.exception.detail)
        self.assertIn("retry", caught.exception.detail)
        with self.assertRaises(StopIteration):
            next(outcomes)

    def test_malformed_ps_row_retries_once_before_readable_metadata(self):
        module = importlib.import_module("floati.seat_teardown_activity")
        outcomes = iter((b"41 1 501 invalid-state\n", b"41 1 501 R\n"))
        def observation(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, next(outcomes), b"")
        with patch.object(os, "getuid", return_value=501), patch.object(module, "_command", side_effect=observation):
            self.assertEqual(({41: 1}, {41: "R"}, {41: 501}), module._darwin_pid_snapshot())
        with self.assertRaises(StopIteration):
            next(outcomes)

    def test_unreadable_uid_cannot_establish_foreign_scope(self):
        self.assertIn("uid=<invalid>", self._unknown(b"41 1 credential-token S\n"))

    def test_foreign_uid_unknown_state_does_not_hide_ancestry(self):
        self.assertEqual(({41: 1}, {41: "?"}, {41: 0}), self._snapshot(b"41 1 0 ?s\n"))

    def test_signed_darwin_uid_normalizes_unsigned_ownership(self):
        """Apple's signed UID formatter must not discard readable uint32 owners."""
        for token, owner in (("0", 0), ("501", 501), ("-2", 4294967294), ("-2147483648", 2147483648),
                             ("-2147483647", 2147483649), ("2147483648", 2147483648),
                             ("4294967294", 4294967294)):
            with self.subTest(token=token):
                try:
                    result = self._snapshot(("41 1 " + token + " S\n").encode())
                except ProtocolRefusal as exc:
                    self.fail("Readable native UID format was refused: " + str(exc))
                self.assertEqual(({41: 1}, {41: "S"}, {41: owner}), result)

    def test_signed_uid_invalid_range_and_tokens_remain_unknown(self):
        for token in ("-1", "-2147483649", "-4294967296", "4294967296", "-0",
                      "+501", "--2", "-02", "-\u00b2", "-credential", "-2\x1b[31m"):
            with self.subTest(token=token):
                detail = self._unknown(("41 1 " + token + " S\n").encode())
                self.assertIn("retry", detail)
                self.assertNotIn("credential", detail)
                self.assertNotIn("\x1b", detail)

    def test_signed_numeric_uid_diagnostic_stays_bounded_and_redacted(self):
        detail = self._unknown(b"41 1 -2 invalid-state secret-command\n")
        self.assertIn("uid=-2", detail)
        self.assertNotIn("secret", detail)
        self.assertNotIn("invalid-state", detail)
        self.assertLess(len(detail), 256)

    def test_darwin_uid_sentinels_refuse_in_both_numeric_representations(self):
        """Sentinel normalization must never manufacture foreign-owner absence."""
        for token in ("-1", "4294967295", "-101", "4294967195"):
            with self.subTest(token=token):
                detail = self._unknown(("41 1 " + token + " S\n").encode())
                self.assertIn("retry", detail)


class LinuxUidPopulationTests(unittest.TestCase):
    def _snapshot(self, *, unreadable_owned=False, unreadable_owner=False, nondumpable_owned=False, owned_status=None):
        module = importlib.import_module("floati.seat_teardown_activity")
        records = {41: (1, 501), 1: (0, 0), 42: (41, 501), 43: (1, 502)}
        class Entries:
            def __enter__(self):
                return iter(SimpleNamespace(name=str(pid)) for pid in records)
            def __exit__(self, *args):
                pass
        def opened(path, *args, **kwargs):
            pid = int(path.parent.name)
            if path.name == "status":
                if unreadable_owner and pid == 43:
                    raise PermissionError("owner unreadable")
                owner = records[pid][1]
                if owner == 501 and owned_status is not None:
                    return io.BytesIO(owned_status)
                return io.BytesIO(("Uid:\t" + "\t".join([str(owner)] * 4) + "\n").encode())
            return io.BytesIO((str(pid) + " (fixture) S " + str(records[pid][0]) + " 0\n").encode())
        def ownership(path, *args, **kwargs):
            if unreadable_owner and int(path.name) == 43:
                raise PermissionError("owner unreadable")
            owner = records[int(path.name)][1]
            return SimpleNamespace(st_uid=0 if nondumpable_owned and owner == 501 else owner)
        def cwd(path):
            pid = int(path.parent.name)
            if records[pid][1] != 501:
                raise PermissionError("foreign cwd must be out of scope")
            if unreadable_owned and pid == 42:
                raise PermissionError("same-user cwd unreadable")
            return "/seat/repo" if pid == 42 else "/"
        with patch.object(os, "getpid", return_value=41), patch.object(os, "getuid", return_value=501), patch.object(os, "scandir", return_value=Entries()), patch.object(Path, "open", opened), patch.object(Path, "stat", ownership), patch.object(os, "readlink", side_effect=cwd):
            return module._linux_snapshot()

    def test_linux_foreign_uid_cwd_is_outside_population_full_ancestry_remains(self):
        rows, ancestry, unknown = self._snapshot()
        self.assertEqual([{"pid": 41, "cwd": "/"}, {"pid": 42, "cwd": "/seat/repo"}], rows)
        self.assertEqual([41, 1], ancestry)
        self.assertEqual([], unknown)

    def test_linux_same_uid_unreadable_cwd_is_explicit_unknown(self):
        rows, ancestry, unknown = self._snapshot(unreadable_owned=True)
        self.assertEqual([{"pid": 41, "cwd": "/"}], rows)
        self.assertEqual([41, 1], ancestry)
        self.assertEqual([{"pid": 42, "reason": "cwd_unreadable"}], unknown)

    def test_linux_unreadable_owner_does_not_assume_foreign_scope(self):
        with self.assertRaises(ProtocolRefusal) as caught:
            self._snapshot(unreadable_owner=True)
        self.assertEqual("seat_teardown_activity_unknown", caught.exception.code)
        self.assertIn("43", caught.exception.detail)

    def test_linux_nondumpable_inode_owner_is_not_process_uid_evidence(self):
        """Non-dumpable same-user /proc files are root-owned, not foreign processes."""
        rows, ancestry, unknown = self._snapshot(nondumpable_owned=True)
        self.assertEqual([{"pid": 41, "cwd": "/"}, {"pid": 42, "cwd": "/seat/repo"}], rows)
        self.assertEqual([41, 1], ancestry)

    def test_linux_effective_uid_field_establishes_same_user_population(self):
        rows, ancestry, unknown = self._snapshot(nondumpable_owned=True, owned_status=b"Uid:\t0\t501\t501\t501\n")
        self.assertEqual([{"pid": 41, "cwd": "/"}, {"pid": 42, "cwd": "/seat/repo"}], rows)

    def test_linux_missing_malformed_duplicate_or_oversize_uid_refuses(self):
        for raw in (b"Name:\tfixture\n", b"Uid:\t501\tsecret\t501\t501\n",
                    b"Uid:\t501\t501\t501\n", b"Uid:\t501\t4294967296\t501\t501\n",
                    b"Uid:\t501\t501\t501\t501\n" * 2, b"x" * 8193):
            with self.subTest(size=len(raw)):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._snapshot(nondumpable_owned=True, owned_status=raw)
                self.assertEqual("seat_teardown_activity_unknown", caught.exception.code)
                self.assertNotIn("secret", caught.exception.detail)


class SeatTeardownActivityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=HOME_PIN_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "seat"
        self.home.mkdir()
        self.repo = self.home / "repo"
        self._repository(self.repo)
        self.remote = self.base / "remote.git"
        self._git(self.repo, "init", "--bare", str(self.remote))
        self._git(self.repo, "remote", "add", "origin", str(self.remote))
        self._git(self.repo, "push", "-u", "origin", "main")

    def _git(self, root, *args):
        return subprocess.run(fixed_git_command("/usr/bin/git", root, args),
                              env=fixed_git_environment("/usr/bin/git"),
                              check=True, capture_output=True, text=True,
                              timeout=20).stdout.strip()

    def _repository(self, root):
        root.mkdir(parents=True)
        self._git(root, "init", "--quiet", "--initial-branch=main")
        self._git(root, "config", "user.name", "Activity Fixture")
        self._git(root, "config", "user.email", "activity@example.invalid")
        (root / "tracked").write_text("base\n")
        (root / ".gitignore").write_text("ignored\n")
        self._git(root, "add", ".")
        self._git(root, "commit", "--quiet", "-m", "fixture base")

    def _module(self):
        try:
            return importlib.import_module("floati.seat_teardown_activity")
        except ModuleNotFoundError as exc:
            if exc.name != "floati.seat_teardown_activity":
                raise
            self.fail("F2 is missing the native seat activity observer")

    def _inspect(self):
        return self._module().inspect_seat_activity(self.home, self.repo)

    def _quiet(self):
        return patch.object(self._module(), "_native_snapshot", return_value=([], [os.getpid(), os.getppid()], []))

    def _refusal(self, code="seat_teardown_activity_unknown"):
        with self.assertRaises(ProtocolRefusal) as caught:
            self._inspect()
        self.assertEqual(code, caught.exception.code)
        self.assertTrue(caught.exception.remedy)
        return caught.exception

    def test_valid_union_includes_external_link_and_independent_nested_root(self):
        """Catches restricting inventory to HOME or just the registered clone."""
        linked = self.base / "outside-linked"
        self._git(self.repo, "worktree", "add", "--detach", str(linked), "HEAD")
        nested = self.home / "projects" / "independent"
        self._git(self.repo, "clone", str(self.remote), str(nested))
        self._git(nested, "checkout", "main")
        (self.repo / "ignored").write_text("ignored does not mean work")
        with self._quiet():
            result = self._inspect()
        self.assertEqual(sorted(map(str, [self.repo, linked, nested])), result["workspace_set"])
        self.assertEqual([], result["native_jobs"])
        self.assertEqual({"population": "invoking_uid", "uid": os.getuid()}, result["native_scope"])
        self.assertTrue(all(not row["tracked_changes"] and not row["untracked_files"] and
                            not row["unpushed_commits"] for row in result["worktrees"]))

    def test_lists_each_dirty_untracked_and_unpushed_workspace(self):
        """Catches stopping after the first unsafe workspace or skipping unpushed work."""
        linked = self.home / "work" / "linked"
        self._git(self.repo, "worktree", "add", "--detach", str(linked), "HEAD")
        independent = self.home / "extra"
        self._git(self.repo, "clone", str(self.remote), str(independent))
        self._git(independent, "checkout", "main")
        self._git(independent, "config", "user.name", "Activity Fixture")
        self._git(independent, "config", "user.email", "activity@example.invalid")
        (self.repo / "tracked").write_text("changed\n")
        (linked / "untracked").write_text("pending\n")
        (independent / "tracked").write_text("new commit\n")
        self._git(independent, "commit", "--quiet", "-am", "local work")
        with self._quiet():
            refusal = self._refusal("seat_teardown_activity_in_flight")
        for root in (self.repo, linked, independent):
            self.assertIn(str(root), refusal.detail)
        self.assertIn("tracked", refusal.detail)
        self.assertIn("untracked", refusal.detail)
        self.assertIn("unpushed", refusal.detail)

    def test_dormant_local_branch_is_not_an_in_flight_worktree(self):
        self._git(self.repo, "checkout", "-b", "unfinished")
        (self.repo / "tracked").write_text("unfinished\n")
        self._git(self.repo, "commit", "--quiet", "-am", "unfinished")
        self._git(self.repo, "checkout", "main")
        with self._quiet():
            self.assertEqual([], self._inspect()["worktrees"][0]["unpushed_commits"])

    def test_absent_registered_worktree_is_unknown(self):
        linked = self.base / "vanished"
        self._git(self.repo, "worktree", "add", "--detach", str(linked), "HEAD")
        linked.rename(self.base / "moved")
        with self._quiet():
            self.assertIn(str(linked), self._refusal().detail)

    def test_registered_workspace_symlink_loop_is_typed_unknown(self):
        linked = self.base / "self-linked"
        self._git(self.repo, "worktree", "add", "--detach", str(linked), "HEAD")
        linked.rename(self.base / "moved-linked")
        linked.symlink_to(linked, target_is_directory=True)
        with self._quiet():
            self.assertIn(str(linked), self._refusal().detail)

    def test_absent_primary_checkout_is_unknown(self):
        self.repo.rename(self.home / "moved")
        with self._quiet():
            self._refusal()

    def test_malformed_discovered_git_marker_is_unknown(self):
        extra = self.home / "broken"
        extra.mkdir()
        (extra / ".git").write_text("not a Git directory\n")
        with self._quiet():
            self.assertIn(str(extra), self._refusal().detail)

    def test_symlink_git_marker_is_unknown(self):
        extra = self.home / "alias"
        extra.mkdir()
        (extra / ".git").symlink_to(self.repo / ".git", target_is_directory=True)
        with self._quiet():
            self._refusal()

    def test_directory_symlink_is_not_followed(self):
        outside = self.base / "outside"
        self._repository(outside)
        (self.home / "alias").symlink_to(outside, target_is_directory=True)
        with self._quiet():
            result = self._inspect()
        self.assertEqual([str(self.repo)], result["workspace_set"])

    def test_unreadable_inventory_refuses_unknown(self):
        original = os.scandir
        def denied(path):
            if Path(path) == self.home:
                raise PermissionError("fixture unreadable seat")
            return original(path)
        with self._quiet(), patch("os.scandir", side_effect=denied):
            self._refusal()

    def test_inventory_limit_refuses_instead_of_truncating(self):
        (self.home / "one").mkdir()
        (self.home / "two").mkdir()
        with self._quiet(), patch.object(self._module(), "_MAX_DIRECTORIES", 1):
            self._refusal()

    def test_malformed_worktree_output_refuses_unknown(self):
        module = self._module()
        original = module._command
        def damaged(argv, **kwargs):
            if "worktree" in argv:
                return subprocess.CompletedProcess(argv, 0, b"worktree relative\0HEAD nope\0\0", b"")
            return original(argv, **kwargs)
        with self._quiet(), patch.object(module, "_command", side_effect=damaged):
            self._refusal()

    def test_command_output_bound_refuses_unknown(self):
        with self._quiet(), patch.object(self._module(), "_MAX_OUTPUT", 8):
            self._refusal()

    def test_process_read_error_refuses_unknown(self):
        with patch.object(self._module(), "_native_snapshot", side_effect=PermissionError("fixture process read denied")):
            self._refusal()

    def test_malformed_process_rows_do_not_prove_absence(self):
        with patch.object(self._module(), "_native_snapshot", return_value=([{"pid": "bad", "cwd": str(self.home)}], [os.getpid()], [])):
            self._refusal()

    def test_computed_ancestry_exempts_only_exact_pids_and_path_boundary(self):
        rows = [{"pid": os.getpid(), "cwd": str(self.repo)},
                {"pid": os.getppid(), "cwd": str(self.home)},
                {"pid": 987654, "cwd": str(self.home) + "-outside"}]
        with patch.object(self._module(), "_native_snapshot", return_value=(rows, [os.getpid(), os.getppid()], [])):
            result = self._inspect()
        self.assertEqual([], result["native_jobs"])
        rows.append({"pid": 987655, "cwd": str(self.repo)})
        with patch.object(self._module(), "_native_snapshot", return_value=(rows, [os.getpid(), os.getppid()], [])):
            self.assertIn("987655", self._refusal("seat_teardown_activity_in_flight").detail)

    def _darwin_metadata(self, before, after, cwd_rows):
        """Model actual tools' requested fields; retain real scratch Git calls."""
        module = self._module()
        original = module._command
        snapshots = iter((before, after))
        def observation(argv, **kwargs):
            if argv[0] == "/bin/ps":
                self.assertEqual(["/bin/ps", "-A", "-o", "pid=,ppid=,uid=,state="], argv)
                records = next(snapshots)
                with_state = "state=" in argv[-1]
                raw = "".join(str(pid) + " " + str(parent) + " " + str(uid) +
                              (" " + state if with_state else "") + "\n"
                              for pid, parent, uid, state in records).encode()
                return subprocess.CompletedProcess(argv, 0, raw, b"")
            if argv[0] in ("/usr/sbin/lsof", "/usr/bin/lsof"):
                self.assertEqual([argv[0], "-n", "-P", "-a", "-u", str(os.getuid()), "-d", "cwd", "-F0pfn"], argv)
                raw = "".join("p" + str(pid) + "\0\nfcwd\0n" + cwd + "\0\n"
                              for pid, cwd in cwd_rows).encode()
                return subprocess.CompletedProcess(argv, 0, raw, b"")
            return original(argv, **kwargs)
        return observation

    def _parents(self):
        return [(os.getpid(), os.getppid(), os.getuid(), "R"), (os.getppid(), 0, os.getuid(), "S")]

    @native_only
    def test_live_pid_missing_from_lsof_is_reported_not_silently_ignored(self):
        """§23: unnamed process uncertainty is visible, not seat ownership."""
        records = self._parents() + [(987655, os.getpid(), os.getuid(), "S")]
        module = self._module()
        observer = self._darwin_metadata(records, records, [(os.getpid(), "/"), (os.getppid(), "/")])
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
            result = self._inspect()
        self.assertEqual([{"pid": 987655, "reason": "cwd_missing", "receipt_ids": []}], result["native_unknown"])

    @native_only
    def test_halted_pid_missing_from_lsof_is_not_absence(self):
        """H parses but is never a terminal-process exemption or an absence proof."""
        records = self._parents() + [(987655, os.getpid(), os.getuid(), "H")]
        module = self._module()
        observer = self._darwin_metadata(records, records, [(os.getpid(), "/"), (os.getppid(), "/")])
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
            result = self._inspect()
        self.assertEqual([{"pid": 987655, "reason": "cwd_missing", "receipt_ids": []}], result["native_unknown"])

    def test_exited_snapshot_child_and_terminal_zombie_do_not_require_cwd(self):
        """Measured state/absence handles ps' own reaped child without names."""
        before = self._parents() + [(987654, os.getpid(), os.getuid(), "R"), (987655, os.getpid(), os.getuid(), "Z")]
        after = self._parents() + [(987655, os.getpid(), os.getuid(), "Z"), (987656, os.getpid(), os.getuid(), "R")]
        module = self._module()
        observer = self._darwin_metadata(before, after, [(os.getpid(), "/"), (os.getppid(), "/")])
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
            self.assertEqual([], self._inspect()["native_jobs"])

    def test_covered_persistent_child_inside_home_still_blocks(self):
        records = self._parents() + [(987655, os.getpid(), os.getuid(), "S")]
        module = self._module()
        observer = self._darwin_metadata(records, records, [(os.getpid(), "/"), (os.getppid(), "/"), (987655, str(self.repo))])
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
            self.assertIn("987655", self._refusal("seat_teardown_activity_in_flight").detail)

    def test_foreign_uid_omission_preserves_full_ancestry_and_scope_receipt(self):
        records = [(os.getpid(), os.getppid(), os.getuid(), "R"),
                   (os.getppid(), 1, os.getuid(), "S"),
                   (1, 0, os.getuid() + 1, "?"),
                   (987655, 1, os.getuid() + 1, "S")]
        module = self._module()
        observer = self._darwin_metadata(records, records, [(os.getpid(), "/"), (os.getppid(), "/")])
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
            result = self._inspect()
        self.assertEqual([os.getpid(), os.getppid(), 1], result["exempt_ancestry"])
        self.assertEqual({"population": "invoking_uid", "uid": os.getuid()}, result["native_scope"])
        self.assertEqual([], result["native_jobs"])

    @native_only
    def test_persistent_pid_becoming_owned_still_requires_cwd_coverage(self):
        """Catches intersecting owned UID subsets and silently losing a UID transition."""
        before = self._parents() + [(987655, 1, os.getuid() + 1, "S")]
        after = self._parents() + [(987655, 1, os.getuid(), "S")]
        module = self._module()
        observer = self._darwin_metadata(before, after, [(os.getpid(), "/"), (os.getppid(), "/")])
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
            result = self._inspect()
        self.assertEqual([{"pid": 987655, "reason": "cwd_missing", "receipt_ids": []}], result["native_unknown"])

    def test_signed_foreign_uid_keeps_global_ancestry_without_cwd_obligation(self):
        records = [(os.getpid(), os.getppid(), os.getuid(), "R"),
                   (os.getppid(), 1, os.getuid(), "S"), (1, 0, -2, "?"),
                   (987655, 1, -2, "S")]
        module = self._module()
        observer = self._darwin_metadata(records, records, [(os.getpid(), "/"), (os.getppid(), "/")])
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
            try:
                result = self._inspect()
            except ProtocolRefusal as exc:
                self.fail("Signed foreign owner lost complete native ancestry: " + str(exc))
        self.assertEqual([os.getpid(), os.getppid(), 1], result["exempt_ancestry"])
        self.assertEqual([], result["native_jobs"])

    @native_only
    def test_signed_invoking_uid_still_requires_persistent_cwd_coverage(self):
        records = [(os.getpid(), os.getppid(), -2, "R"), (os.getppid(), 1, -2, "S"),
                   (1, 0, 0, "S"), (987655, os.getppid(), -2, "S")]
        module = self._module()
        with patch.object(os, "getuid", return_value=4294967294):
            observer = self._darwin_metadata(records, records, [(os.getpid(), "/"), (os.getppid(), "/")])
            with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
                result = self._inspect()
        self.assertEqual([{"pid": 987655, "reason": "cwd_missing", "receipt_ids": []}], result["native_unknown"])

    def test_signed_invoking_uid_covered_child_inside_home_still_blocks(self):
        records = [(os.getpid(), os.getppid(), -2, "R"), (os.getppid(), 1, -2, "S"),
                   (1, 0, 0, "S"), (987655, os.getppid(), -2, "S")]
        module = self._module()
        with patch.object(os, "getuid", return_value=4294967294):
            observer = self._darwin_metadata(records, records, [(os.getpid(), "/"), (os.getppid(), "/"), (987655, str(self.repo))])
            with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
                detail = self._refusal("seat_teardown_activity_in_flight").detail
        self.assertIn("PID 987655", detail)

    def test_covered_same_user_outside_home_is_not_work(self):
        records = self._parents() + [(987655, os.getpid(), os.getuid(), "S")]
        module = self._module()
        observer = self._darwin_metadata(records, records, [(os.getpid(), "/"), (os.getppid(), "/"), (987655, str(self.base))])
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observer), patch.object(Path, "is_file", return_value=True):
            self.assertEqual([], self._inspect()["native_jobs"])

    def test_cwd_metadata_failure_is_not_retried(self):
        module = self._module()
        observer = self._darwin_metadata(self._parents(), self._parents(), [(os.getpid(), "/"), (os.getppid(), "/")])
        cwd_calls = []
        def malformed(argv, **kwargs):
            if argv[0] in ("/usr/sbin/lsof", "/usr/bin/lsof"):
                cwd_calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, b"not-cwd-evidence", b"")
            return observer(argv, **kwargs)
        with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=malformed), patch.object(Path, "is_file", return_value=True):
            self._refusal()
        self.assertEqual(1, len(cwd_calls))

    def _fleet(self):
        from floati.root import FloatiRoot
        from floati.registry import Registry
        root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("builder", "other", "architect"):
            Registry(root).register(node, "Codex")
        return root

    def _worker_audit(self, root, *, node="builder", detail=None):
        """Native source/callback produce retained evidence; no process is launched."""
        from floati.planes import AuthorityGrantStore
        from floati.work import WorkLog
        from floati.workers import WorkerReceipts
        from floati.seat_execution import SeatExecutionController
        from floati.ids import uuid7_hex
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        work = WorkLog(root)
        grant = AuthorityGrantStore(root).claim("work-claims", node, 60, 60, now)
        item = work.add("native scope fixture", node,
                        [{"repo": "floati", "sha": "a" * 40, "doc": "docs/car.md"}],
                        car_binding={"row": "F2-SEAT-TEARDOWN-1", "repository": "floati", "branch": "codex/car"}, now=now)
        work.claim(item["id"], node, "work-claims", grant["epoch"], now=now)
        receipts = WorkerReceipts(root)
        session = "worker-" + uuid7_hex()
        receipts.append(session, item["id"], node, "codex", "claim", None, [], now=now)
        source = receipts.append(session, item["id"], node, "codex", "spawn", None, [], now=now)
        controller = SeatExecutionController(root, recipient="architect")
        invocation = controller.bind_worker(source, self.repo)
        audit = {"adapter_pid": 987655, "registered_process_groups": [987656],
                 "alive_after_cleanup": [987656], "isolation_backend": "macos-sandbox"}
        row = controller.callback(invocation, {"event": "unknown", "code": "worker_cleanup_unconfirmed",
                                  "detail": json.dumps(audit) if detail is None else detail})["execution"]
        return row, source

    def _unknown_inspection(self, root, pid=987655):
        with patch.object(self._module(), "_native_snapshot", return_value=([], [os.getpid(), os.getppid()],
                         [{"pid": pid, "reason": "cwd_unreadable"}])):
            return self._module().inspect_seat_activity(self.home, self.repo, root=root, node="builder")

    @native_only
    def test_native_unknown_named_by_own_worker_receipt_refuses(self):
        root = self._fleet()
        row, source = self._worker_audit(root)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._unknown_inspection(root)
        self.assertEqual("seat_teardown_activity_unknown", caught.exception.code)
        self.assertIn("987655", caught.exception.detail)
        self.assertIn(row["id"], caught.exception.detail)

    @native_only
    def test_live_workspace_job_outranks_own_unreadable_process(self):
        root = self._fleet()
        row, _ = self._worker_audit(root)
        with patch.object(self._module(), "_native_snapshot", return_value=(
                [{"pid": 987657, "cwd": str(self.repo)}], [os.getpid()],
                [{"pid": 987655, "reason": "cwd_unreadable"}])):
            with self.assertRaises(ProtocolRefusal) as caught:
                self._module().inspect_seat_activity(self.home, self.repo, root=root, node="builder")
        self.assertEqual("seat_teardown_activity_in_flight", caught.exception.code)
        self.assertIn("PID 987657", caught.exception.detail)
        self.assertIn(row["id"], caught.exception.detail)

    @native_only
    def test_live_workspace_job_outranks_unreadable_git_evidence(self):
        with patch.object(self._module(), "_worktree", side_effect=PermissionError("fixture denied")), patch.object(
                self._module(), "_native_snapshot", return_value=(
                    [{"pid": 987657, "cwd": str(self.repo)}], [os.getpid()], [])):
            refusal = self._refusal("seat_teardown_activity_in_flight")
        self.assertIn("PID 987657", refusal.detail)
        self.assertIn("fixture denied", refusal.detail)

    @native_only
    def test_foreign_worker_audit_cannot_name_local_unknown(self):
        root = self._fleet()
        self._worker_audit(root, node="other")
        result = self._unknown_inspection(root)
        self.assertEqual([{"pid": 987655, "reason": "cwd_unreadable", "receipt_ids": []}], result["native_unknown"])

    @native_only
    def test_worker_group_identifier_is_not_individual_pid_ownership(self):
        root = self._fleet()
        self._worker_audit(root)
        result = self._unknown_inspection(root, pid=987656)
        self.assertEqual([{"pid": 987656, "reason": "cwd_unreadable", "receipt_ids": []}], result["native_unknown"])

    @native_only
    def test_noncanonical_worker_detail_cannot_fabricate_pid_ownership(self):
        for detail in ('{"adapter_pid":987655}',
                       '{"adapter_pid":true,"registered_process_groups":[],"alive_after_cleanup":[],"isolation_backend":null}',
                       '{"adapter_pid":987655,"adapter_pid":987655,"registered_process_groups":[],"alive_after_cleanup":[],"isolation_backend":null}',
                       '{"adapter_pid":987655,"registered_process_groups":[],"alive_after_cleanup":[],"isolation_backend":"invented"}'):
            with self.subTest(detail=detail):
                with tempfile.TemporaryDirectory(dir=self.base) as directory:
                    from floati.root import FloatiRoot
                    from floati.registry import Registry
                    root = FloatiRoot.open_direct_home(Path(directory) / "fleet", create=True)
                    for node in ("builder", "architect"):
                        Registry(root).register(node, "Codex")
                    self._worker_audit(root, detail=detail)
                    self.assertEqual([], self._unknown_inspection(root)["native_unknown"][0]["receipt_ids"])

    @native_only
    def test_worker_source_identity_mismatch_cannot_mint_ownership(self):
        root = self._fleet()
        row, source = self._worker_audit(root)
        import floati.workers as workers
        changed = dict(source, node_id="other")
        with patch.object(workers.WorkerReceipts, "records", return_value=[changed]):
            with self.assertRaises(ProtocolRefusal):
                self._unknown_inspection(root)

    @native_only
    def test_cwd_alias_resolves_inside_home_and_prefix_lookalike_is_outside(self):
        alias = self.base / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        outside = self.base / "seat-other"
        outside.mkdir()
        rows = [{"pid": 987655, "cwd": str(outside)}]
        with patch.object(self._module(), "_native_snapshot", return_value=(rows, [os.getpid()], [])):
            self.assertEqual([], self._inspect()["native_jobs"])
        rows[0]["cwd"] = str(alias)
        with patch.object(self._module(), "_native_snapshot", return_value=(rows, [os.getpid()], [])):
            self.assertIn("987655", self._refusal("seat_teardown_activity_in_flight").detail)

    @native_only
    def test_unresolvable_cwd_is_explicit_unknown(self):
        rows = [{"pid": 987655, "cwd": str(self.base / "absent")}]
        with patch.object(self._module(), "_native_snapshot", return_value=(rows, [os.getpid()], [])):
            result = self._inspect()
        self.assertEqual([{"pid": 987655, "reason": "cwd_unresolvable", "receipt_ids": []}], result["native_unknown"])

    @native_only
    def test_unknown_rows_are_closed_sorted_and_never_silently_dropped(self):
        rows = [{"pid": 987656, "reason": "cwd_missing"}, {"pid": 987655, "reason": "cwd_unreadable"}]
        with patch.object(self._module(), "_native_snapshot", return_value=([], [os.getpid()], rows)):
            result = self._inspect()
        self.assertEqual([987655, 987656], [row["pid"] for row in result["native_unknown"]])
        self.assertEqual([str(self.home)], result["native_roots"])
        for damaged in ({"pid": True, "reason": "cwd_missing"}, {"pid": 987655, "reason": "invented"},
                        {"pid": 987655, "reason": "cwd_missing", "owned": True}):
            with self.subTest(damaged=damaged), patch.object(self._module(), "_native_snapshot", return_value=([], [os.getpid()], [damaged])):
                self._refusal()

    @native_only
    def test_unknown_state_with_readable_cwd_still_uses_location(self):
        module = self._module()
        records = self._parents() + [(987655, os.getpid(), os.getuid(), "?E")]
        for path, inside in ((self.base, False), (self.repo, True)):
            with self.subTest(inside=inside):
                # Unknown metadata uses its one retry before each bracket.
                snapshots = iter((records, records, records, records))
                original = module._command
                def observed(argv, **kwargs):
                    if argv[0] == "/bin/ps":
                        raw = "".join("%s %s %s %s\n" % row for row in next(snapshots)).encode()
                        return subprocess.CompletedProcess(argv, 0, raw, b"")
                    if argv[0] in ("/usr/sbin/lsof", "/usr/bin/lsof"):
                        raw = ("p%d\0\nfcwd\0n/\0\np%d\0\nfcwd\0n/\0\np987655\0\nfcwd\0n%s\0\n" % (os.getpid(), os.getppid(), path)).encode()
                        return subprocess.CompletedProcess(argv, 0, raw, b"")
                    return original(argv, **kwargs)
                with patch.object(module, "_native_snapshot", module._darwin_snapshot), patch.object(module, "_command", side_effect=observed), patch.object(Path, "is_file", return_value=True):
                    if inside:
                        self._refusal("seat_teardown_activity_in_flight")
                    else:
                        self.assertEqual([], self._inspect()["native_unknown"])

    @native_only
    def test_active_own_lease_outside_home_is_in_git_and_native_scope(self):
        from floati.jsonl import append_record
        from floati.ids import uuid7_hex
        root = self._fleet()
        lanes = self.base / "lanes"
        leased = lanes / "builder" / "work" / "ROW"
        leased.parent.mkdir(parents=True)
        self._git(self.repo, "worktree", "add", "-b", "codex/lane/builder/ROW", str(leased), "main")
        (root.resolve_relative("state")).mkdir(exist_ok=True)
        root.resolve_relative("state/lanes-root.json").write_text(json.dumps({"schema_version": 0, "path": str(lanes)}))
        root.resolve_relative("state/lane-repositories.json").write_text(json.dumps({"schema_version": 0, "repositories": {"fixture": {"path": str(self.repo), "default_base": "main"}}}))
        record = {"schema_version": 0, "kind": "lane_workspace_record", "id": "lane-workspace-" + uuid7_hex(),
                  "tenant_id": root.tenant_id, "timestamp": "2026-10-15T00:00:00Z", "node_id": "builder", "row": "ROW",
                  "path": "builder/work/ROW", "repo": "fixture", "base_sha": self._git(self.repo, "rev-parse", "HEAD"),
                  "branch": "codex/lane/builder/ROW", "state": "open", "opened_at": "2026-10-15T00:00:00Z",
                  "closed_at": None, "closed_by": None, "opened_record_id": None, "why": None}
        append_record(root, "nodes/builder/lanes.jsonl", record, allowed_kinds={"lane_workspace_record"})
        rows = [{"pid": 987655, "cwd": str(leased)}]
        with patch.object(self._module(), "_native_snapshot", return_value=(rows, [os.getpid()], [])):
            with self.assertRaises(ProtocolRefusal) as caught:
                self._module().inspect_seat_activity(self.home, self.repo, root=root, node="builder")
        self.assertEqual("seat_teardown_activity_in_flight", caught.exception.code)
        with self._quiet():
            result = self._module().inspect_seat_activity(self.home, self.repo, root=root, node="builder")
        self.assertIn(str(leased), result["workspace_set"])
        self.assertEqual(sorted([str(self.home), str(leased)]), result["native_roots"])

    def _child(self, cwd, argv=None):
        child = subprocess.Popen(argv or [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
                                 cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def close():
            child.communicate(timeout=15)
        self.addCleanup(close)
        if argv is None:
            self.assertEqual("ready\n", child.stdout.readline())
        else:
            self.assertIsNone(child.poll())
        return child

    def test_same_user_sleep_cwd_inside_refuses_and_outside_passes(self):
        """Literal ruling control: identical sleep argv, actual uid and native cwd."""
        outside = self._child(self.base, ["/bin/sleep", "10"])
        result = self._inspect()
        self.assertIsNone(outside.poll(), "outside sleep must still be live at observation cutoff")
        self.assertEqual({"population": "invoking_uid", "uid": os.getuid()}, result["native_scope"])
        self.assertEqual([], result["native_jobs"])
        self.assertNotIn(outside.pid, result["exempt_ancestry"])
        inside = self._child(self.repo, ["/bin/sleep", "10"])
        self.assertIn(str(inside.pid), self._refusal("seat_teardown_activity_in_flight").detail)

    def test_live_sibling_cwd_blocks_and_outside_child_does_not(self):
        """Uses a synchronized real child; same executable names earn no exemption."""
        outside = self._child(self.base)
        prior_cwd = Path.cwd()
        try:
            os.chdir(self.repo)
            result = self._inspect()
        finally:
            os.chdir(prior_cwd)
        self.assertEqual([], result["native_jobs"])
        self.assertIn(os.getpid(), result["exempt_ancestry"])
        self.assertIn(os.getppid(), result["exempt_ancestry"])
        self.assertNotIn(outside.pid, result["exempt_ancestry"])
        inside = self._child(self.repo)
        self.assertIn(str(inside.pid), self._refusal("seat_teardown_activity_in_flight").detail)
