"""Actual d66 product bytes must outrank Git index cleanliness hints.

These correctness controls call the existing product proof against private
checkouts of genuine fetched objects. Bounded controls share an immutable
object bank and independently materialize the proof's complete product scope;
the full profile fetches and checks out the entire commit for each flag.
No fixture is constructed until the tests run. The selected commit is genuine
and fixed; no baseline override, live fleet, native teardown, or performance
measurement is involved.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from floati.git_process import fixed_git_command, fixed_git_environment
from tests.bus_scale_test_profile import full_scale_enabled
from tests.public_projection import require_harbor_history
from tests.temp_roots import REAL_TEMP_ROOT, _PLATFORM_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py"
PRODUCT = "d66a94daa357bd392b3040c55cbe21e20ac884a3"
TARGET = "floati/seat_teardown.py"
PRODUCT_PATHS = ("floati", "schemas", "bundle-manifest.v0.json")
SOURCES = (
    Path(__file__).resolve(), ADAPTER, ROOT / "tests/bus_scale_native_refusal.py",
    ROOT / "tests/fixtures/bus_scale_census/bus-scale-routine-teardown-2026-10-15.py",
    ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py",
)


def source_hashes():
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCES}


def blob_id(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def inventory(home):
    result = {}
    for path in sorted(home.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or path.resolve(strict=True) != path or info.st_nlink != 1:
            raise AssertionError("private product fixture contains an alias or shared/special file")
        raw = path.read_bytes()
        result[path.relative_to(home).as_posix()] = {
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "mode": stat.S_IMODE(info.st_mode),
        }
    return result


class TeardownProductProofFlaggedIndexTests(unittest.TestCase):
    _object_bank = None

    @classmethod
    def _prepare_object_bank(cls):
        owner = cls()
        owner.temporary = tempfile.TemporaryDirectory(prefix="bus-scale-product-object-bank-", dir=REAL_TEMP_ROOT)
        owner.base = Path(owner.temporary.name).resolve()
        owner.retained = Path(tempfile.mkdtemp(prefix="bus-scale-product-object-bank-evidence-",
                                             dir=_PLATFORM_TEMP_ROOT)).resolve()
        owner.record = {"kind": "shared_genuine_product_object_bank", "product_sha": PRODUCT,
                        "source_before": source_hashes(), "git_commands": [],
                        "performance_evidence": False, "native_teardown_executed": False,
                        "original_coordinate": str(owner.base), "retained": str(owner.retained)}
        owner.addCleanup(owner.retain_and_remove)
        cls.addClassCleanup(owner.doCleanups)
        cls.addClassCleanup(setattr, cls, "_object_bank", None)
        owner.object_bank = owner.base / "object-bank.git"
        owner.object_bank.mkdir(mode=0o700)
        owner.git(owner.object_bank, "init", "--quiet", "--bare")
        owner.git(owner.object_bank, "fetch", "--quiet", "--depth=1", str(ROOT), PRODUCT)
        owner.assertEqual((PRODUCT + "\n").encode(),
                          owner.git(owner.object_bank, "rev-parse", "FETCH_HEAD"))
        owner.object_inventory = inventory(owner.object_bank)
        cls._object_bank = owner

    def setUp(self):
        require_harbor_history(self, ROOT, PRODUCT)
        cls = type(self)
        if not full_scale_enabled() and cls._object_bank is None:
            cls._prepare_object_bank()
        self.temporary = tempfile.TemporaryDirectory(prefix="bus-scale-product-proof-owned-", dir=REAL_TEMP_ROOT)
        self.base = Path(self.temporary.name).resolve()
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-product-proof-flagged-index-",
                                             dir=_PLATFORM_TEMP_ROOT)).resolve()
        self.record = {"test": self.id(), "product_sha": PRODUCT, "target": TARGET,
                       "source_before": source_hashes(), "git_commands": [],
                       "performance_evidence": False, "native_teardown_executed": False,
                       "original_coordinate": str(self.base), "retained": str(self.retained)}
        self.addCleanup(self.retain_and_remove)
        self.checkout = self.base / "checkout"
        self.checkout.mkdir(mode=0o700)
        self.git(self.checkout, "init", "--quiet")
        if full_scale_enabled():
            # Full controls retain their original independent local fetch and
            # complete checkout, including unrelated historical documentation.
            self.git(self.checkout, "fetch", "--quiet", "--depth=1", str(ROOT), PRODUCT)
            self.git(self.checkout, "checkout", "--quiet", "--detach", PRODUCT)
        else:
            owner = type(self)._object_bank
            self.assertEqual(owner.object_inventory, inventory(owner.object_bank))
            destination = self.checkout / ".git" / "objects"
            shutil.copytree(owner.object_bank / "objects", destination,
                            copy_function=shutil.copy2, dirs_exist_ok=True)
            shallow = owner.object_bank / "shallow"
            if shallow.exists():
                shutil.copyfile(shallow, self.checkout / ".git" / "shallow")
            self.assertFalse((destination / "info" / "alternates").exists(),
                             "each flag case requires its own real object bytes")
            self.assertEqual(inventory(owner.object_bank / "objects"), inventory(destination))
            # Build a fresh independent index with native Git. Only the proof's
            # declared complete product scope is materialized; no source byte,
            # index flag, proof result or committed object is fabricated.
            self.git(self.checkout, "update-ref", "--no-deref", "HEAD", PRODUCT)
            self.git(self.checkout, "read-tree", PRODUCT)
            self.git(self.checkout, "checkout", "--quiet", PRODUCT, "--", *PRODUCT_PATHS)
            self.record.update(shared_object_bank=str(owner.retained / "fixture" / "object-bank.git"),
                               working_tree_scope=list(PRODUCT_PATHS))
            self.assertEqual(owner.object_inventory, inventory(owner.object_bank),
                             "a flag case changed its immutable genuine object bank")
        self.assertEqual((PRODUCT + "\n").encode(), self.git(self.checkout, "rev-parse", "HEAD"))
        specification = importlib.util.spec_from_file_location("flagged_index_product_proof", ADAPTER)
        self.adapter = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(self.adapter)

    def git(self, repository, *arguments):
        argv = fixed_git_command("/usr/bin/git", repository, arguments)
        environment = fixed_git_environment("/usr/bin/git")
        result = subprocess.run(argv, cwd=str(self.base), env=environment,
                                capture_output=True, timeout=30)
        self.record["git_commands"].append({
            "argv": argv, "cwd": str(self.base), "environment": environment,
            "exit_code": result.returncode,
            "stdout_base64": base64.b64encode(result.stdout).decode("ascii"),
            "stderr_base64": base64.b64encode(result.stderr).decode("ascii"),
        })
        self.assertEqual(0, result.returncode, "private native Git prerequisite failed: "
                         + result.stderr.decode("utf-8", "replace"))
        return result.stdout

    def product_proof(self):
        """Record actual subprocess testimony even if proof refuses it."""
        actual_run = subprocess.run

        def recording_run(*args, **kwargs):
            command = args[0] if args else kwargs.get("args")
            entry = {
                "argv": [str(value) for value in command],
                "cwd": None if kwargs.get("cwd") is None else str(kwargs["cwd"]),
                "environment": dict(kwargs.get("env") or {}),
                "capture_output": kwargs.get("capture_output"),
                "timeout": kwargs.get("timeout"),
            }
            # Insert before delegation so a native launch/timeout refusal is
            # retained too. Nothing below supplies or changes a child result.
            self.record.setdefault("proof_subprocess_calls", []).append(entry)
            try:
                result = actual_run(*args, **kwargs)
            except BaseException as error:
                entry["exception"] = {"type": type(error).__name__, "detail": str(error)}
                for name in ("stdout", "stderr"):
                    raw = getattr(error, name, None)
                    if isinstance(raw, bytes):
                        entry[name + "_base64"] = base64.b64encode(raw).decode("ascii")
                raise
            entry.update(exit_code=result.returncode,
                         stdout_base64=base64.b64encode(result.stdout).decode("ascii"),
                         stderr_base64=base64.b64encode(result.stderr).decode("ascii"))
            return result

        with mock.patch.object(self.adapter.subprocess, "run", side_effect=recording_run):
            return self.adapter.teardown_product_proof()

    def retain_and_remove(self):
        self.record["source_after"] = source_hashes()
        try:
            for path in SOURCES:
                destination = self.retained / "source" / path.relative_to(ROOT)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
                destination.chmod(0o600)
            shutil.copytree(self.base, self.retained / "fixture", copy_function=shutil.copy2)
            self.record["retained_fixture_inventory"] = inventory(self.retained / "fixture")
        finally:
            self.temporary.cleanup()
            self.record["scratch_removed"] = not self.base.exists()
            (self.retained / "result.json").write_text(json.dumps(self.record, sort_keys=True, indent=2) + "\n")
            print("private flagged-index product proof retained: " + str(self.retained), flush=True)

    def assert_flagged_edit_refuses(self, flag, index_marker):
        with mock.patch.object(self.adapter, "ROOT", self.checkout):
            pristine = self.product_proof()
            self.assertEqual(PRODUCT, pristine["product_sha"], "pristine real d66 must be accepted first")
            self.record["pristine_proof"] = pristine
            path = self.checkout / TARGET
            before = path.read_bytes()
            tree_entry = self.git(self.checkout, "ls-tree", PRODUCT, "--", TARGET)
            cells = tree_entry.decode("ascii").rstrip("\n").split()
            self.assertEqual(["100644", "blob", blob_id(before), TARGET], cells)
            committed = self.git(self.checkout, "cat-file", "blob", cells[2])
            self.assertEqual(before, committed, "private input must contain the actual pinned committed bytes")
            (self.retained / "before-product.bin").write_bytes(before)
            (self.retained / "committed-product.bin").write_bytes(committed)
            self.record.update(index_flag=flag, committed_blob=cells[2], before_blob=blob_id(before),
                               target_before_sha256=hashlib.sha256(before).hexdigest())
            self.git(self.checkout, "update-index", flag, "--", TARGET)
            index_state = self.git(self.checkout, "ls-files", "-v", "--", TARGET)
            self.assertEqual((index_marker + " " + TARGET + "\n").encode(), index_state)
            after = before + b"\n# private flagged-index product-proof regression\n"
            path.write_bytes(after)
            (self.retained / "after-product.bin").write_bytes(after)
            self.record.update(after_blob=blob_id(after), target_after_sha256=hashlib.sha256(after).hexdigest())
            self.assertNotEqual(cells[2], blob_id(after))
            self.assertEqual(b"", self.git(self.checkout, "status", "--porcelain=v1", "-z",
                                           "--untracked-files=all", "--", *PRODUCT_PATHS),
                             "flagged-index control must reproduce Git's clean-status blind spot")
            self.assertEqual(b"", self.git(self.checkout, "diff", "--exit-code", PRODUCT, "--", *PRODUCT_PATHS),
                             "flagged-index control must reproduce the existing proof's clean diff")
            self.record["git_status_and_diff_clean_after_real_edit"] = True
            try:
                with self.assertRaises(self.adapter.UnresolvedObservation,
                                       msg="actual changed product bytes must refuse despite clean Git index hints"):
                    admitted = self.product_proof()
                    self.record["incorrectly_admitted_changed_product"] = admitted
            finally:
                self.assertEqual(after, path.read_bytes(), "read-only product proof changed its input")
                self.assertEqual(self.record["source_before"], source_hashes(), "reviewed instrument source changed")

    def test_assume_unchanged_cannot_authenticate_modified_d66_product_bytes(self):
        self.assert_flagged_edit_refuses("--assume-unchanged", "h")

    def test_skip_worktree_cannot_authenticate_modified_d66_product_bytes(self):
        self.assert_flagged_edit_refuses("--skip-worktree", "S")


if __name__ == "__main__":
    unittest.main()
