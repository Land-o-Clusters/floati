"""Failed owned child output must reverse without adopting later caller files.

All configuration, executables, repositories, and fleet state are scratch
fixtures. These tests never invoke a real Codex executable or build tool.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from floati import seat_onboarding
from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE
from floati.registry import Registry
from tests import test_seat_onboard as baseline


class SeatOnboardProcessFailureTests(unittest.TestCase):
    # Reuse only the scratch fixture and CLI helpers, without inheriting and
    # rediscovering the baseline module's test methods.
    setUp = baseline.SeatOnboardingTests.setUp
    git = baseline.SeatOnboardingTests.git
    cli = baseline.SeatOnboardingTests.cli
    add = baseline.SeatOnboardingTests.add

    def fake_cli(self, output, commands=""):
        executable = self.base / "fake-codex"
        executable.write_text("#!/bin/sh\n[ \"$1\" = app-server ] || exit 9\n"
            + "IFS= read -r request\nprintf '{\"id\":1,\"result\":{}}\\n'\n"
            + "IFS= read -r initialized\nIFS= read -r request\n" + commands
            + "cat <<'FLOATI_FIXTURE_JSON'\n" + output + "\nFLOATI_FIXTURE_JSON\n"
            + "printf 'fixture diagnostic\\n' >&2\nexit 0\n")
        executable.chmod(0o700)
        return executable

    def failed_cli(self, output):
        executable = self.fake_cli(output)
        code, artifact, errors = self.add("--mode", "cli", "--codex-executable", str(executable))
        self.assertEqual(20, code, errors or artifact)
        evidence = json.loads(artifact)["evidence"]
        self.assertEqual("seat_onboard_invalid", evidence["code"])
        self.assertIn("creation-only", evidence["detail"])
        self.assertIn(output.splitlines()[0] + "\n", (self.home / "scratch/cli-start.jsonl").read_text())
        self.assertEqual("fixture diagnostic\n", (self.home / "scratch/cli-start.stderr").read_text())
        return self.journal()

    def journal(self):
        path = seat_onboarding._record_path(self.root, self.home)
        record = json.loads(path.read_text())
        self.assertEqual("failed", record["state"])
        return record

    def config_bytes(self):
        return {path: path.read_bytes() for path in (self.config, self.profiles, self.hooks)}

    def assert_clean_removal(self, before, node):
        code, artifact, errors = self.cli("seat", "remove", "--root", str(self.root.path),
            "--home", str(self.home))
        self.assertEqual(0, code, errors or artifact)
        self.assertEqual("removed", json.loads(artifact)["evidence"]["state"])
        self.assertFalse(self.home.exists())
        self.assertEqual(before, self.config_bytes())
        self.assertFalse((self.root.path / WORKSPACE_MAP_RELATIVE).exists())
        self.assertEqual("retired", Registry(self.root)._latest(node)["state"])
        record_path = seat_onboarding._record_path(self.root, self.home)
        record = json.loads(record_path.read_text())
        self.assertFalse(Path(record["stage"]).exists())
        self.assertFalse(Path(record["removal_stage"]).exists())

    def test_invalid_thread_started_cleans_owned_stdout_and_stderr(self):
        before = self.config_bytes()
        record = self.failed_cli('{"type":"thread.started","thread_id":17}')
        self.assert_clean_removal(before, record["node_id"])

    def test_missing_thread_started_cleans_owned_stdout_and_stderr(self):
        before = self.config_bytes()
        record = self.failed_cli('[]\n{"type":"turn.completed"}')
        self.assert_clean_removal(before, record["node_id"])

    def test_failed_warm_script_cleans_owned_partial_build_and_log(self):
        script = self.source / "warm-fixture.sh"
        script.write_text("#!/bin/sh\nmkdir -p .build\n"
            + "printf 'partial artifact\\n' > .build/partial.txt\n"
            + "printf 'fixture build failed\\n'\nexit 7\n")
        script.chmod(0o700)
        (self.source / "Package.swift").write_text("// fixture package marker; no Swift invocation\n")
        self.git("add", "warm-fixture.sh", "Package.swift")
        self.git("commit", "--quiet", "-m", "scratch warm failure fixture")
        before = self.config_bytes()
        code, artifact, errors = self.add("--warm-build-script", "warm-fixture.sh")
        self.assertEqual(20, code, errors or artifact)
        evidence = json.loads(artifact)["evidence"]
        self.assertEqual("seat_onboard_invalid", evidence["code"])
        self.assertIn("warm build failed", evidence["detail"])
        self.assertEqual("partial artifact\n", (self.home / "repo/.build/partial.txt").read_text())
        self.assertEqual("fixture build failed\n", (self.home / "scratch/warm-build.log").read_text())
        record = self.journal()
        self.assert_clean_removal(before, record["node_id"])

    def test_caller_file_after_child_failure_is_preserved_before_any_removal(self):
        self.failed_cli('{"type":"turn.completed"}')
        personal = self.home / "scratch/personal.txt"
        personal.write_text("caller content survives\n")
        before = self.config_bytes()
        registry_before = Registry(self.root).path.read_bytes()
        map_path = self.root.path / WORKSPACE_MAP_RELATIVE
        map_before = map_path.read_bytes()
        code, artifact, errors = self.cli("seat", "remove", "--root", str(self.root.path),
            "--home", str(self.home))
        self.assertEqual(20, code, errors or artifact)
        self.assertEqual("seat_onboard_invalid", json.loads(artifact)["evidence"]["code"])
        self.assertEqual("caller content survives\n", personal.read_text())
        self.assertEqual(before, self.config_bytes())
        self.assertEqual(registry_before, Registry(self.root).path.read_bytes())
        self.assertEqual(map_before, map_path.read_bytes())
        self.assertTrue((self.home / "scratch/cli-start.jsonl").is_file())

    def test_failed_clone_reverses_its_owned_partial_repository(self):
        before = self.config_bytes()
        original_git = seat_onboarding._git
        staged = []
        def partial_clone(repository, *arguments):
            if arguments[0] == "clone":
                destination = Path(arguments[-1])
                destination.mkdir()
                (destination / "partial.txt").write_text("owned clone fragment\n")
                staged.append(destination)
                raise OSError("fixture clone interrupted after writing")
            return original_git(repository, *arguments)
        with mock.patch.object(seat_onboarding, "_git", side_effect=partial_clone):
            code, artifact, errors = self.add()
        self.assertEqual(20, code, errors or artifact)
        self.assertEqual("seat_onboard_io_failed", json.loads(artifact)["evidence"]["code"])
        self.assertEqual("owned clone fragment\n", (staged[0] / "partial.txt").read_text())
        self.journal()
        code, artifact, errors = self.cli("seat", "remove", "--root", str(self.root.path),
            "--home", str(self.home))
        self.assertEqual(0, code, errors or artifact)
        self.assertFalse(staged[0].parent.exists())
        self.assertFalse(self.home.exists())
        self.assertEqual(before, self.config_bytes())

    def test_unfinalized_child_capture_preserves_outputs_and_capabilities(self):
        executable = self.fake_cli('{"type":"turn.completed"}')
        original_capture = seat_onboarding._capture_child_outputs
        def interrupted_capture(path, record, home, intent):
            if intent["kind"] == "cli":
                raise OSError("fixture capture interrupted before durable output cut")
            return original_capture(path, record, home, intent)
        with mock.patch.object(seat_onboarding, "_capture_child_outputs", side_effect=interrupted_capture):
            code, artifact, errors = self.add("--mode", "cli", "--codex-executable", str(executable))
        self.assertEqual(20, code, errors or artifact)
        self.assertEqual("seat_onboard_io_failed", json.loads(artifact)["evidence"]["code"])
        record = self.journal()
        self.assertEqual("pending", record["child_outputs"][-1]["state"])
        self.assertIsNone(record["child_outputs"][-1]["output_inventory"])
        before = self.config_bytes()
        registry_before = Registry(self.root).path.read_bytes()
        map_path = self.root.path / WORKSPACE_MAP_RELATIVE
        map_before = map_path.read_bytes()
        code, artifact, errors = self.cli("seat", "remove", "--root", str(self.root.path), "--home", str(self.home))
        self.assertEqual(20, code, errors or artifact)
        self.assertIn("no finalized output inventory", json.loads(artifact)["evidence"]["detail"])
        self.assertEqual(before, self.config_bytes())
        self.assertEqual(registry_before, Registry(self.root).path.read_bytes())
        self.assertEqual(map_before, map_path.read_bytes())
        self.assertEqual('fixture diagnostic\n', (self.home / "scratch/cli-start.stderr").read_text())

    def test_cli_workspace_write_is_never_adopted_by_output_capture(self):
        executable = self.fake_cli('{"type":"turn.completed"}', "printf 'foreign workspace edit\\n' > README.md\n")
        code, artifact, errors = self.add("--mode", "cli", "--codex-executable", str(executable))
        self.assertEqual(20, code, errors or artifact)
        self.assertIn("outside its declared output contract", json.loads(artifact)["evidence"]["detail"])
        record = self.journal()
        self.assertEqual("finalized", record["child_outputs"][-1]["state"])
        self.assertNotIn("repo/README.md", record["child_outputs"][-1]["output_inventory"])
        before = self.config_bytes()
        code, artifact, errors = self.cli("seat", "remove", "--root", str(self.root.path), "--home", str(self.home))
        self.assertEqual(20, code, errors or artifact)
        self.assertEqual(before, self.config_bytes())
        self.assertEqual("foreign workspace edit\n", (self.home / "repo/README.md").read_text())

    def test_declared_log_changed_after_failure_is_not_adopted_on_removal(self):
        self.failed_cli('{"type":"turn.completed"}')
        changed_log = self.home / "scratch/cli-start.stderr"
        changed_log.write_text("caller replacement diagnostic\n")
        before = self.config_bytes()
        code, artifact, errors = self.cli("seat", "remove", "--root", str(self.root.path), "--home", str(self.home))
        self.assertEqual(20, code, errors or artifact)
        self.assertEqual(before, self.config_bytes())
        self.assertEqual("caller replacement diagnostic\n", changed_log.read_text())


if __name__ == "__main__":
    unittest.main()
