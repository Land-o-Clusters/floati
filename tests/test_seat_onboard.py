"""A seat is provisioned and reversed by one operator command, in scratch only."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from floati.cli import main
from floati.codex_wait_contract import resolve_participant
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_declaration import FleetGovernance, COORDINATOR_AUTHORITIES, OWNER_TIERS
from tests.temp_roots import REAL_TEMP_ROOT
from tests.seat_transport_fixture import create_transport
from tests.seat_toml_fixture import install_fixture_toml
from tests.git_fixture_cache import cached_tree, fixture_git


def _build_source(root):
    """The one-commit source repository every onboarding fixture starts from."""
    def git(*args):
        subprocess.run([fixture_git("/usr/bin/git"), "-C", str(root), *args],
            capture_output=True, text=True, check=True)
    git("init", "--quiet")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.invalid")
    (root / "README.md").write_text("fixture repository\n")
    git("add", "README.md")
    git("commit", "--quiet", "-m", "fixture")


class SeatOnboardingTests(unittest.TestCase):
    def setUp(self):
        install_fixture_toml(self)
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register("architect", "Codex")
        FleetGovernance.create(self.root, topology="star", coordinator="architect",
            coordinator_authority=COORDINATOR_AUTHORITIES, owner_tier=OWNER_TIERS)
        self.source = self.base / "source"
        self.source.mkdir()
        # Built once per process; every test owns an independent copy.
        cached_tree(self.source, ("test_seat_onboard.source",), _build_source,
            git="/usr/bin/git", refresh_index=False)
        self.git("update-index", "-q", "--refresh")  # stays setUp's first git step
        self.home = self.base / "seat"
        self.config = self.base / "config.toml"
        self.config.write_text('model = "fixture"\n')
        from floati.codex_hook_install import format_waiter_stop_command
        from floati.codex_hook_trust import codex_hook_current_hash
        self.hooks = self.base / "hooks.json"
        self.waiter_launcher = self.base / "floati-codex-wait.py"
        self.waiter_launcher.write_text("#!/usr/bin/python3\nraise SystemExit(31)\n")
        self.waiter_launcher.chmod(0o700)
        self.hook_block = {"hooks": [{"type": "command",
            "command": format_waiter_stop_command(self.waiter_launcher, self.root.path),
            "timeout": 300, "statusMessage": "Watching Floati bus"}]}
        self.hooks.write_text(json.dumps({"hooks": {"Stop": [self.hook_block]}}) + "\n")
        self.config.write_text(self.config.read_text() + "\n[hooks.state."
            + json.dumps(f"{self.hooks}:stop:0:0") + "]\n"
            + "enabled = true\ntrusted_hash = "
            + json.dumps(codex_hook_current_hash(self.hook_block)) + "\n")
        self.profiles = self.base / "profiles.json"
        self.transport = create_transport(self.base / "installed")
        self.profiles.write_text(json.dumps({"schema_version": 2,
            "profiles": {}, "transports": {"fixture": self.transport}}))

    def git(self, *args):
        return subprocess.run([fixture_git("/usr/bin/git"), "-C", str(self.source), *args],
            capture_output=True, text=True, check=True).stdout.strip()

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = main(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, output.getvalue(), errors.getvalue()

    def add(self, *extra):
        return self.cli("seat", "add", "--root", str(self.root.path),
            "--home", str(self.home), "--source", str(self.source),
            "--gateway-config", str(self.profiles), "--codex-config", str(self.config),
            "--transport", "fixture", "--coordinator", "architect", "--repo", "fixture",
            "--mode", "desktop", *extra)

    def test_missing_toml_parser_refuses_before_protected_capability_or_home_writes(self):
        def protected_files():
            # First add may create this one operational coordination lock.
            # Epoch coordination locks the existing tenant-home directory.
            return {str(path.relative_to(self.root.path)): (path.stat().st_mode, path.read_bytes())
                    for path in self.root.path.rglob("*") if path.is_file()
                    and path.relative_to(self.root.path) != Path("state/seat-onboarding/operation.lock")}
        before = protected_files()
        configuration = {path: (path.stat().st_mode, path.read_bytes())
                         for path in (self.config, self.profiles)}
        delegate = __import__
        def without_toml(name, *args, **kwargs):
            if name in ("tomllib", "tomli"):
                raise ImportError("fixture interpreter lacks both TOML parsers")
            return delegate(name, *args, **kwargs)
        with mock.patch("builtins.__import__", side_effect=without_toml):
            code, output, errors = self.add()
        self.assertEqual(20, code, errors or output)
        artifact = json.loads(output)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("seat_harness_config_invalid", artifact["evidence"]["code"])
        self.assertIn("TOML validation is unavailable", artifact["evidence"]["detail"])
        self.assertEqual(before, protected_files())
        self.assertEqual(configuration, {path: (path.stat().st_mode, path.read_bytes())
                                        for path in configuration})
        self.assertFalse(self.home.exists())
        self.assertEqual([], list(self.base.glob(".seat-add-*")))
        self.assertEqual([], list((self.root.path / "state/seat-onboarding").glob("*.json")))
        self.assertFalse((self.root.path / "codex-wait/workspaces.v0.json").exists())
        self.assertEqual([], list((self.root.path / "receipts/codex-wait-consent").glob("*.jsonl")))

    def test_one_add_provisions_a_mapped_declared_seat_and_exact_boot_directory(self):
        # Today's register succeeds without a participant: the measured bus half.
        Registry(self.root).register("legacy", "Codex")
        self.assertIsNone(resolve_participant(self.root.path, self.source))
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)
        result = json.loads(output)["evidence"]
        workspace = self.home / "repo"
        self.assertEqual(str(workspace), result["workspace"])
        participant = resolve_participant(self.root.path, workspace)
        self.assertIsNotNone(participant)
        self.assertEqual(result["node_id"], participant.binding.node_id)
        self.assertTrue((workspace / "SEAT.json").is_file())
        self.assertTrue((self.home / "SEAT.md").is_file())
        self.assertTrue((self.home / "wt").is_dir())
        self.assertTrue((self.home / "scratch").is_dir())
        self.assertIn(str(workspace), result["operator_action"])
        self.assertIn("paste", result["operator_action"].lower())

    def test_one_remove_restores_configuration_and_leaves_no_owned_home(self):
        before = {path: path.read_bytes() for path in (self.config, self.profiles)}
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)
        result = json.loads(output)["evidence"]
        code, output, errors = self.cli("seat", "remove", "--root", str(self.root.path),
            "--home", str(self.home))
        self.assertEqual(0, code, errors or output)
        self.assertFalse(self.home.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertIsNone(resolve_participant(self.root.path, self.home / "repo"))
        self.assertEqual("retired", Registry(self.root)._latest(result["node_id"])["state"])

    def test_legacy_retire_cannot_reverse_manually_provisioned_configuration(self):
        # Baseline witness remains: low-level retirement owns registry rows only.
        self.home.mkdir()
        original = self.config.read_bytes()
        Registry(self.root).register("legacy", "Codex")
        Registry(self.root).retire("legacy")
        self.assertTrue(self.home.exists())
        self.assertEqual(original, self.config.read_bytes())

    def test_add_replay_retains_name_and_native_receipts(self):
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)
        first = json.loads(output)["evidence"]
        before = Registry(self.root).path.read_bytes()
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)
        self.assertEqual(first, json.loads(output)["evidence"])
        self.assertEqual(before, Registry(self.root).path.read_bytes())

    def test_remove_refuses_foreign_home_content_before_configuration_mutation(self):
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)
        foreign = self.home / "scratch" / "personal.txt"
        foreign.write_text("preserve this\n")
        before = {path: path.read_bytes() for path in (self.config, self.profiles)}
        code, output, errors = self.cli("seat", "remove", "--root", str(self.root.path),
            "--home", str(self.home))
        self.assertEqual(20, code, errors or output)
        self.assertEqual("preserve this\n", foreign.read_text())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_relinquished_staging_name_never_authorizes_deleting_new_foreign_data(self):
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)
        record = json.loads(Path(json.loads(output)["evidence"]["ownership_record"]).read_text())
        foreign = Path(record["stage"])
        foreign.mkdir()
        (foreign / "personal.txt").write_text("foreign\n")
        code, output, errors = self.cli("seat", "remove", "--root", str(self.root.path), "--home", str(self.home))
        self.assertEqual(0, code, errors or output)
        self.assertEqual("foreign\n", (foreign / "personal.txt").read_text())

    def test_failed_waiter_append_is_still_removed_from_the_workspace_map(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger, WORKSPACE_MAP_RELATIVE
        with mock.patch.object(CodexWaitConsentLedger, "arm", side_effect=OSError("injected append failure")):
            code, output, errors = self.add()
            self.assertEqual(20, code, errors or output)
            self.assertEqual("seat_onboard_io_failed", json.loads(output)["evidence"]["code"])
        self.assertTrue((self.root.path / WORKSPACE_MAP_RELATIVE).exists())
        code, output, errors = self.cli("seat", "remove", "--root", str(self.root.path), "--home", str(self.home))
        self.assertEqual(0, code, errors or output)
        self.assertFalse((self.root.path / WORKSPACE_MAP_RELATIVE).exists())
        self.assertFalse(self.home.exists())

    def test_tracked_symlink_is_inventory_data_and_reverses_without_following_it(self):
        (self.source / "readme-link").symlink_to("README.md")
        self.git("add", "readme-link")
        self.git("commit", "--quiet", "-m", "fixture link")
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)
        self.assertTrue((self.home / "repo/readme-link").is_symlink())
        code, output, errors = self.cli("seat", "remove", "--root", str(self.root.path), "--home", str(self.home))
        self.assertEqual(0, code, errors or output)
        self.assertTrue((self.source / "README.md").is_file())

    def test_cli_mode_starts_a_thread_with_the_exact_clone_directory(self):
        from tests.test_seat_onboard_cli_holding import creation_provider
        executable = creation_provider(self.base)
        code, output, errors = self.add("--mode", "cli", "--codex-executable", str(executable))
        self.assertEqual(0, code, errors or output)
        thread = json.loads(output)["evidence"]["thread"]
        self.assertEqual("fixture-paused-thread", thread["thread_id"])
        self.assertEqual("paused", thread["state"])
        self.assertFalse(thread["turn_started"])

    def test_rehome_retires_stale_adapter_and_marks_prior_mail_as_history(self):
        from floati.events import EventLog
        from floati.waiter_consent import arm_waiter_consent
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
        Registry(self.root).register("legacy", "Codex")
        old = self.base / "prior-life"
        old.mkdir()
        arm_waiter_consent(self.root.path, old, "legacy", "Codex", 30, 25)
        profiles = json.loads(self.profiles.read_text())
        profiles["profiles"]["legacy-profile"] = {"transport": "fixture",
            "git_common_dir": str(old / ".git"), "workspace_root": str(old),
            "bus_root": str(self.root.path), "identity": "legacy",
            "allowed_operations": ["ack", "board", "inbox", "send"],
            "send_repository": "fixture", "send_recipients": ["architect"]}
        self.profiles.write_text(json.dumps(profiles))
        self.config.write_text(self.config.read_text() + '\n[projects.' + json.dumps(str(old)) + ']\ntrust_level = "trusted"\n')
        executable = self.base / "stale-codex"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        coordinate = DaemonCoordinate(self.root, "legacy", "codex")
        store = AdapterBindingStore(self.root)
        store.write(coordinate, session_id="old-session", workspace=old,
            executable=executable, adapter_version="1", adapter_digest="a" * 64, binding_epoch=1)
        stale_bytes = store.path(coordinate).read_bytes()
        executable.unlink()
        message = EventLog(self.root).send("architect", "legacy", "fixture", "a" * 40,
            "README.md", "stale prior-life dispatch", idempotency_key="historical")
        code, output, errors = self.add("--node", "legacy", "--profile", "legacy-profile", "--rehome")
        self.assertEqual(0, code, errors or output)
        result = json.loads(output)["evidence"]
        self.assertEqual([message["id"]], [row["id"] for row in result["prior_life_messages"]])
        self.assertEqual("historical_acknowledged", result["prior_life_disposition"])
        self.assertEqual(stale_bytes, Path(result["rehome"]["adapter"]["archive_path"]).read_bytes())
        self.assertFalse(store.path(coordinate).exists())
        self.assertEqual([], EventLog(self.root).present("legacy")[0])
        code, output, errors = self.cli("seat", "remove", "--root", str(self.root.path), "--home", str(self.home))
        self.assertEqual(0, code, errors or output)
        self.assertNotIn("legacy-profile", json.loads(self.profiles.read_text())["profiles"])
        self.assertNotIn(str(old), self.config.read_text())
        self.assertTrue(old.is_dir(), "an unowned previous home is retained")


if __name__ == "__main__":
    unittest.main()
