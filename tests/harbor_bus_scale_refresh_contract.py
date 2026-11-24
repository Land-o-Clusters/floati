"""Current native compatibility controls for the restored census instrument.

These are correctness controls, not scale/performance observations. Existing
command/workload tests retain responsibility for the full parser closure.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from floati import cli
from floati.git_process import fixed_git_command
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from tests import HOME_PIN_ROOT
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_effects import validate_status_cache
from tests.bus_scale_integration import normalize_status_cache
from tests.temp_roots import REAL_TEMP_ROOT


ENTRY = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"
SPEC = importlib.util.spec_from_file_location("bus_scale_refresh_native_entry", ENTRY)
entry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entry)


class BusScaleRefreshContractTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(Path(HOME_PIN_ROOT), Path.home())
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-refresh-contract-", dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "scale-fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        for node in ("scale-worker", "architect"):
            Registry(self.root).register(node, "codex")

    def native_cache(self):
        state, evidence, code = cli._status(SimpleNamespace(
            root=str(self.home), json=True, consumer=None, destination=None))
        self.assertEqual(("ok", 0), (state, code))
        artifact = dict(artifact_version=0, command="status", status=state, evidence=evidence)
        path = FleetProjection(self.root)._status_snapshot_store().path
        cache = json.loads(path.read_bytes())
        self.assertIn("message_progress_observation_version", cache["payload"])
        return artifact, cache, path

    def test_cold_native_cache_validator_retains_message_progress_generation(self):
        artifact, cache, path = self.native_cache()
        before = path.read_bytes()
        payload = validate_status_cache(self.home, artifact)
        self.assertEqual(cache["payload"], payload)
        self.assertEqual(cache["payload"]["message_progress_observation_version"],
                         payload["message_progress_observation_version"])
        self.assertEqual(before, path.read_bytes())

    def test_cache_audit_retains_native_message_progress_generation_without_normalizing_it(self):
        artifact, cache, path = self.native_cache()
        before = path.read_bytes()
        original = copy.deepcopy((cache, artifact))
        normalized = normalize_status_cache(cache, artifact,
            {"artifact": copy.deepcopy(artifact), "normalizations": []})
        self.assertEqual(cache, normalized["cache"])
        self.assertEqual([], normalized["normalizations"])
        self.assertEqual(original, (cache, artifact))
        self.assertEqual(before, path.read_bytes())

    def execute(self, argv, repository):
        previous = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(repository)
            with redirect_stdout(output):
                code = cli.main(argv)
        finally:
            os.chdir(previous)
        return code, json.loads(output.getvalue())

    def test_owned_repository_and_reachable_sha_are_valid_native_send_inputs(self):
        repository, sha = banked_send_repository(self.base)
        argv = ["send", "--root", str(self.home), "--from", "scale-worker",
                "--to", "architect", "--repo", "floati", "--sha", sha,
                "--doc", "note.md", "--note", "owned native compatibility control",
                "--idempotency-key", "refresh-native-control"]
        code, artifact = self.execute(argv, repository)
        self.assertEqual(0, code, json.dumps(artifact, sort_keys=True))
        self.assertEqual("ok", artifact["status"])

    def test_restored_entry_send_uses_the_owned_repository_reachable_sha(self):
        repository, _sha = banked_send_repository(self.base)
        argv = entry.arguments("send", self.home, repository)
        code, artifact = self.execute(argv, repository)
        self.assertEqual(0, code, json.dumps(artifact, sort_keys=True))
        self.assertEqual("ok", artifact["status"])

    def test_diagnostic_policy_admits_native_send_proof_only_in_owned_git_workspace(self):
        parent = self.base / "bus-scale-census-owned-control"
        repository, sha = banked_send_repository(parent)
        workspace = parent / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        queries = [("rev-parse", "--show-toplevel"), ("cat-file", "-e", sha + "^{commit}"),
                   ("for-each-ref", "--format=%(refname)", "refs/remotes"),
                   ("for-each-ref", "--format=%(refname)", "--contains", sha, "refs/remotes")]
        kwargs = {"env": {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"},
                  "stdout": -1, "stderr": -1, "text": True}
        self.assertEqual([True] * len(queries), [entry.scratch_git_probe(
            fixed_git_command("/usr/bin/git", workspace, query), kwargs, workspace) for query in queries])


if __name__ == "__main__":
    unittest.main()
