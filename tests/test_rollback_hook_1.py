"""Rollback must not create an untested product/host-wiring generation."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from floati import fixture_ids
from floati.codex_hook_install import CodexHookInstaller
from floati.codex_hook_trust import codex_hook_current_hash
from floati.deploy import DeploymentWriter
from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from tests import test_deploy as deploy_fixture


class RollbackHostWiringTests(unittest.TestCase):
    def setUp(self):
        self.fixture = deploy_fixture.DeploymentWriterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.home = (self.fixture.base / "home").resolve()
        self.home.mkdir()
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.codex = self.home / ".codex"
        self.codex.mkdir()
        self.hooks = self.codex / "hooks.json"
        self.store = self.codex / "floati-wake"
        self.store.mkdir()
        self.root = self.home / "fixture-fleet"
        Registry(FloatiRoot.open_direct_home(self.root, create=True)).register(
            fixture_ids.builder("floati"), "worker"
        )
        self.workspace = self.home / "workspace"
        self.workspace.mkdir()
        # A is an old digest command, already trusted by the fixture operator.
        old_tree = self.store / ("a" * 64)
        (old_tree / "scripts").mkdir(parents=True)
        (old_tree / "scripts/floati-codex-wait").write_text("#!/bin/sh\nexit 0\n")
        self.old_block = {"hooks": [{"type": "command", "command":
            f"/usr/bin/python3 {old_tree}/scripts/floati-codex-wait --root {self.root}",
            "timeout": 10, "statusMessage": "Watching Floati bus"}]}
        self.old_hooks = json.dumps({"hooks": {"Stop": [self.old_block],
            "SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}).encode()
        self.hooks.write_bytes(self.old_hooks)
        self.trust = self.codex / "config.toml"
        self.trust.write_text('[model]\nname = "foreign-sentinel"\n[hooks.state.'
            + json.dumps(f"{self.hooks}:stop:0:0") + ']\ntrusted_hash = '
            + json.dumps(codex_hook_current_hash(self.old_block)) + '\n')
        self.old_trust = self.trust.read_bytes()
        self.destination = self.home / "installed"
        self.sha_a = self.fixture._git("rev-parse", "HEAD")
        self.deploy()
        self.fixture._write_source("floati/__init__.py", "VERSION = 'two'\n")
        self.fixture._write_manifest()
        self.fixture._git("add", ".")
        self.fixture._git("commit", "--quiet", "-m", "release B")
        self.sha_b = self.fixture._git("rev-parse", "HEAD")
        self.deploy()

    def deploy(self):
        return DeploymentWriter(self.fixture.source, self.destination,
            "update" if self.destination.exists() else "install", ref="HEAD",
            committed_tree=True, installer_path=self.fixture._installed_path(self.destination)).run()

    def install_current_hook(self):
        # The real governed installer writes current/ and the hook block.
        # On current main the Stop rewrite requires an exact hash pair.
        installer = CodexHookInstaller(
            source_root=Path(__file__).resolve().parents[1],
            bus_home=self.root, hooks_path=self.hooks, destination=self.store,
        )
        try:
            return installer.install(
                self.workspace, fixture_ids.builder("floati"),
                hook_timeout_seconds=10, wait_deadline_seconds=2)
        except ProtocolRefusal as exc:
            if exc.code != "stop_hook_definition_change_unacknowledged":
                raise
            detail = exc.refusal_detail
            pair = f"{detail['preimage']}:{detail['target']}"
            return installer.install(
                self.workspace, fixture_ids.builder("floati"),
                hook_timeout_seconds=10, wait_deadline_seconds=2,
                acknowledge_stop_hook_change=pair)

    def rollback(self):
        self.fixture._git("checkout", "--quiet", self.sha_a)
        return self.deploy()

    def assert_refused_without_product_mutation(self, *paths):
        before = self.fixture._tree_bytes(self.destination)
        try:
            self.rollback()
        except ProtocolRefusal as exc:
            self.assertEqual("rollback_host_artifacts_differ", exc.code)
            for path in paths:
                key = path.relative_to(self.codex).as_posix()
                self.assertIn(key, exc.detail)
                self.assertNotIn(str(self.home), exc.detail)
            self.assertNotEqual({"kind": "none", "why": "no action was named for this refusal"}, exc.remedy)
        else:
            self.fail("rollback accepted a partial generation: changed host artifacts were neither restored nor named")
        self.assertEqual(before, self.fixture._tree_bytes(self.destination))

    def test_roll_forward_native_hook_then_rollback_refuses_before_managed_writes(self):
        receipt = self.install_current_hook()
        self.assertIn("/current/", receipt["command"])
        self.assertNotEqual(self.old_hooks, self.hooks.read_bytes())
        self.assertFalse(receipt["hook_armed"])
        self.assert_refused_without_product_mutation(self.hooks, self.store / "current")
        self.assertEqual(self.old_trust, self.trust.read_bytes())

    def test_unchanged_host_wiring_rolls_back_and_reads_back_stop_and_trust(self):
        result = self.rollback()
        self.assertEqual(self.sha_a, result["source_sha"])
        proof = result.get("host_rollback")
        self.assertIsNotNone(proof, "rollback receipt must read back Stop and trust against its pre-update photograph")
        self.assertEqual(proof["before"], proof["readback"])
        self.assertEqual([self.old_block], proof["readback"]["stop"])
        self.assertTrue(proof["readback"]["trust"][0]["hook_armed"])
        self.assertEqual(self.old_trust, self.trust.read_bytes())

    def test_trust_only_change_refuses_and_preserves_operator_choice(self):
        self.trust.write_bytes(self.old_trust + b"enabled = false\n")
        changed = self.trust.read_bytes()
        self.assert_refused_without_product_mutation(self.trust)
        self.assertEqual(changed, self.trust.read_bytes())

    def test_each_separate_gateway_and_registry_artifact_is_named(self):
        for relative in ("bin/codex-fleet-bus", "bin/boardbus.md", "fleet-bus-profiles.json"):
            path = self.codex / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text("changed by its separate writer\n")
        self.assert_refused_without_product_mutation(self.codex / "bin/codex-fleet-bus",
            self.codex / "bin/boardbus.md", self.codex / "fleet-bus-profiles.json")

    def test_forward_update_after_hook_install_is_not_blocked(self):
        self.install_current_hook()
        self.fixture._write_source("floati/__init__.py", "VERSION = 'three'\n")
        self.fixture._write_manifest()
        self.fixture._git("add", ".")
        self.fixture._git("commit", "--quiet", "-m", "release C")
        result = self.deploy()
        self.assertEqual(self.fixture._git("rev-parse", "HEAD"), result["source_sha"])

    def test_retargeted_current_is_fingerprinted_not_followed(self):
        from floati.deploy import _fingerprint

        first = self.store / ("c" * 64)
        second = self.store / ("d" * 64)
        for tree in (first, second):
            (tree / "scripts").mkdir(parents=True)
            (tree / "scripts/floati-codex-wait").write_text("#!/bin/sh\nexit 0\n")
        current = self.store / "current"
        current.symlink_to(first)
        self.deploy()
        current.unlink()
        current.symlink_to(second)
        self.assertEqual(_fingerprint(current), {"kind": "symlink", "target": str(second)})
        self.assertNotEqual(_fingerprint(current)["target"], str(first))
        self.assert_refused_without_product_mutation(current)

    def test_pre_feature_install_without_photograph_is_unverified_legacy(self):
        """R4 RED: fa86-era metadata has no photograph marker; missing sidecar must not refuse."""

        from floati.deploy import _host_wiring_path, _photograph_record_path

        _host_wiring_path(self.destination).unlink()
        record = _photograph_record_path(self.destination)
        if record.exists() or record.is_symlink():
            record.unlink()
        result = self.rollback()
        self.assertEqual(self.sha_a, result["source_sha"])
        self.assertEqual("unverified_legacy", result["host_rollback"])

    def test_missing_garbage_or_symlinked_photograph_refuses_on_reverse(self):
        from floati.deploy import _host_wiring_path

        path = _host_wiring_path(self.destination)
        cases = (
            (lambda: path.unlink(), "missing"),
            (lambda: path.write_text("{not-json", encoding="utf-8"), "garbage"),
            (
                lambda: (path.unlink(), path.symlink_to(self.home / "elsewhere")),
                "symlink",
            ),
        )
        for mutate, label in cases:
            with self.subTest(label):
                self.fixture._git("checkout", "--quiet", self.sha_b)
                self.deploy()
                mutate()
                before = self.fixture._tree_bytes(self.destination)
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.rollback()
                self.assertEqual("rollback_host_photograph_invalid", caught.exception.code)
                self.assertIn(label, caught.exception.detail)
                self.assertEqual(before, self.fixture._tree_bytes(self.destination))


if __name__ == "__main__":
    unittest.main()
