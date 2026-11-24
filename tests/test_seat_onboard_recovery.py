"""Scratch seat lifecycle drift and interrupted transaction recovery.

Execution is deferred while the fleet BUILDS HOLD is active. These tests use
the existing onboarding fixture; no real HOME or fleet state is a target.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE, consent_ledger_relative
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from tests import test_seat_onboard as fixtures


class SeatOnboardingRecoveryTests(unittest.TestCase):
    # Reuse fixture setup without inheriting and rerunning its existing tests.
    setUp = fixtures.SeatOnboardingTests.setUp
    git = fixtures.SeatOnboardingTests.git
    cli = fixtures.SeatOnboardingTests.cli
    add = fixtures.SeatOnboardingTests.add

    def prepared(self, *extra):
        code, output, errors = self.add(*extra)
        self.assertEqual(0, code, errors or output)
        return json.loads(output)["evidence"]

    def remove(self):
        return self.cli("seat", "remove", "--root", str(self.root.path),
                        "--home", str(self.home))

    def assert_io_failure(self, response):
        code, output, errors = response
        self.assertEqual(20, code, errors or output)
        artifact = json.loads(output)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("seat_onboard_io_failed", artifact["evidence"]["code"])

    def assert_replay_refuses_without_repair(self):
        registry_before = Registry(self.root).path.read_bytes()
        config_before = self.config.read_bytes()
        profiles_before = self.profiles.read_bytes()
        map_path = self.root.path / WORKSPACE_MAP_RELATIVE
        map_before = map_path.read_bytes() if map_path.exists() else None
        code, output, errors = self.add()
        self.assertEqual(20, code, errors or output)
        self.assertEqual("refused", json.loads(output)["status"])
        self.assertEqual(registry_before, Registry(self.root).path.read_bytes())
        self.assertEqual(config_before, self.config.read_bytes())
        self.assertEqual(profiles_before, self.profiles.read_bytes())
        self.assertEqual(map_before, map_path.read_bytes() if map_path.exists() else None)

    def test_ready_replay_refuses_changed_gateway_profile_without_repair(self):
        result = self.prepared()
        profiles = json.loads(self.profiles.read_bytes())
        profiles["profiles"][result["profile"]]["identity"] = "foreign-seat"
        self.profiles.write_text(json.dumps(profiles))
        self.assert_replay_refuses_without_repair()

    def test_ready_replay_refuses_removed_workspace_mapping_without_repair(self):
        self.prepared()
        path = self.root.path / WORKSPACE_MAP_RELATIVE
        document = json.loads(path.read_bytes())
        document["mappings"] = []
        path.write_text(json.dumps(document))
        self.assert_replay_refuses_without_repair()

    def test_ready_replay_refuses_retired_registration_without_reactivation(self):
        result = self.prepared()
        Registry(self.root).retire(result["node_id"])
        self.assert_replay_refuses_without_repair()
        self.assertEqual("retired", Registry(self.root)._latest(result["node_id"])["state"])

    def test_add_remove_add_suggests_unused_name_and_owns_new_lifecycle(self):
        baseline = (self.profiles.read_bytes(), self.config.read_bytes())
        first = self.prepared()
        code, output, errors = self.remove()
        self.assertEqual(0, code, errors or output)
        second = self.prepared()
        self.assertNotEqual(first["node_id"], second["node_id"])
        registry = Registry(self.root)
        self.assertEqual("retired", registry._latest(first["node_id"])["state"])
        self.assertEqual("active", registry._latest(second["node_id"])["state"])
        self.assertEqual(second["node_id"], json.loads(
            Path(second["workspace"]).joinpath("SEAT.json").read_bytes())["node_id"])
        code, output, errors = self.remove()
        self.assertEqual(0, code, errors or output)
        self.assertEqual(baseline, (self.profiles.read_bytes(), self.config.read_bytes()))
        self.assertFalse(self.home.exists())

    def test_declared_owned_warm_script_runs_in_exact_clone_and_is_reversible(self):
        scripts = self.source / "scripts"
        scripts.mkdir()
        script = scripts / "warm.sh"
        script.write_text("#!/bin/sh\nset -eu\nmkdir -p .build\n"
                          "printf '%s\\n' \"$PWD\" > .build/workspace\n"
                          "printf 'fixture warmed\\n'\n")
        script.chmod(0o700)
        self.source.joinpath("Package.swift").write_text("// scratch compiled-package marker\n")
        self.git("add", "scripts/warm.sh", "Package.swift")
        self.git("commit", "--quiet", "-m", "scratch owned warm script")
        result = self.prepared("--warm-build-script", "scripts/warm.sh")
        self.assertEqual("completed", result["warm_build"]["state"])
        self.assertEqual(0, result["warm_build"]["exit_code"])
        self.assertEqual(str(self.home / "repo") + "\n",
                         self.home.joinpath("repo/.build/workspace").read_text())
        self.assertEqual("fixture warmed\n", self.home.joinpath("scratch/warm-build.log").read_text())
        self.assertFalse(self.source.joinpath(".build").exists())
        code, output, errors = self.remove()
        self.assertEqual(0, code, errors or output)
        self.assertFalse(self.home.exists())
        self.assertTrue(script.is_file())

    def test_remove_retry_reconciles_disarm_done_before_progress_write(self):
        from floati import seat_onboarding
        result = self.prepared()
        save = seat_onboarding._save
        interrupted = False
        def interrupt_after_waiter(path, record):
            nonlocal interrupted
            if not interrupted and record["state"] == "removing" and (
                "waiter" in record.get("removal_steps", [])
            ):
                interrupted = True
                raise InterruptedError("scratch interruption after native waiter retirement")
            return save(path, record)
        with mock.patch.object(seat_onboarding, "_save", interrupt_after_waiter):
            self.assert_io_failure(self.remove())
        rows_before = read_records_snapshot(self.root, consent_ledger_relative(result["node_id"]),
                                           allowed_kinds={"codex_wait_consent_receipt"})
        self.assertEqual("disarmed", rows_before[-1]["state"])
        code, output, errors = self.remove()
        self.assertEqual(0, code, errors or output)
        rows_after = read_records_snapshot(self.root, consent_ledger_relative(result["node_id"]),
                                          allowed_kinds={"codex_wait_consent_receipt"})
        self.assertEqual(rows_before, rows_after)
        self.assertFalse(self.home.exists())

    def test_remove_retry_resumes_partial_owned_tree_deletion(self):
        from floati import seat_onboarding
        result = self.prepared()
        rmtree = seat_onboarding.shutil.rmtree
        interrupted = False
        def partial_delete(path, *args, **kwargs):
            nonlocal interrupted
            path = Path(path)
            if not interrupted and path.name.startswith(".seat-remove-"):
                interrupted = True
                path.joinpath("repo/README.md").unlink()
                raise OSError("scratch partial tree deletion")
            return rmtree(path, *args, **kwargs)
        with mock.patch.object(seat_onboarding.shutil, "rmtree", partial_delete):
            self.assert_io_failure(self.remove())
        record = json.loads(Path(result["ownership_record"]).read_bytes())
        self.assertEqual("removing", record["state"])
        self.assertTrue(Path(record["removal_stage"]).is_dir())
        self.assertFalse(self.home.exists())
        code, output, errors = self.remove()
        self.assertEqual(0, code, errors or output)
        self.assertFalse(Path(record["removal_stage"]).exists())
        self.assertEqual("retired", Registry(self.root)._latest(result["node_id"])["state"])

    def test_preparing_journal_precedes_stage_allocation_and_can_be_removed(self):
        baseline = (self.config.read_bytes(), self.profiles.read_bytes())
        observed = []
        mkdir = Path.mkdir
        def interrupt_allocation(path, *args, **kwargs):
            if path.parent != self.home.parent or not path.name.startswith(".seat-add-"):
                return mkdir(path, *args, **kwargs)
            journals = list(self.root.path.joinpath("state/seat-onboarding").glob("*.json"))
            self.assertEqual(1, len(journals))
            record = json.loads(journals[0].read_bytes())
            self.assertEqual("preparing", record["state"])
            self.assertEqual(str(self.home), record["home"])
            self.assertEqual(str(path), record["stage"])
            self.assertIsNone(record["stage_identity"])
            observed.append(journals[0])
            raise OSError("scratch staging allocation interruption")
        with mock.patch.object(Path, "mkdir", interrupt_allocation):
            self.assert_io_failure(self.add())
        self.assertEqual(1, len(observed))
        self.assertFalse(self.home.exists())
        code, output, errors = self.remove()
        self.assertEqual(0, code, errors or output)
        self.assertEqual("removed", json.loads(observed[0].read_bytes())["state"])
        self.assertEqual(baseline, (self.config.read_bytes(), self.profiles.read_bytes()))

    def test_unknown_created_stage_without_identity_is_preserved_on_remove(self):
        baseline = (self.config.read_bytes(), self.profiles.read_bytes())
        mkdir = Path.mkdir
        observed = []
        def create_then_interrupt(path, *args, **kwargs):
            result = mkdir(path, *args, **kwargs)
            if path.parent == self.home.parent and path.name.startswith(".seat-add-"):
                observed.append(path)
                path.joinpath("personal.txt").write_text("uncertain directory ownership\n")
                raise InterruptedError("scratch interruption before stage identity was saved")
            return result
        with mock.patch.object(Path, "mkdir", create_then_interrupt):
            self.assert_io_failure(self.add())
        self.assertEqual(1, len(observed))
        code, output, errors = self.remove()
        self.assertEqual(20, code, errors or output)
        self.assertEqual("uncertain directory ownership\n", observed[0].joinpath("personal.txt").read_text())
        self.assertEqual(baseline, (self.config.read_bytes(), self.profiles.read_bytes()))

    def test_legacy_preparing_journal_without_stage_recovers_when_no_home_exists(self):
        from floati import seat_onboarding
        baseline = (self.config.read_bytes(), self.profiles.read_bytes())
        save = seat_onboarding._save
        journal = []
        def interrupt_after_preparing(path, record):
            if record["state"] == "preparing" and not journal:
                # Model the earlier durable format before stage allocation.
                legacy = {key: value for key, value in record.items()
                          if key not in {"stage", "stage_identity"}}
                save(path, legacy)
                journal.append(path)
                raise InterruptedError("scratch legacy preparing journal")
            return save(path, record)
        with mock.patch.object(seat_onboarding, "_save", interrupt_after_preparing):
            self.assert_io_failure(self.add())
        self.assertEqual(1, len(journal))
        self.assertFalse(self.home.exists())
        code, output, errors = self.remove()
        self.assertEqual(0, code, errors or output)
        self.assertEqual("removed", json.loads(journal[0].read_bytes())["state"])
        self.assertEqual(baseline, (self.config.read_bytes(), self.profiles.read_bytes()))


if __name__ == "__main__":
    unittest.main()
