"""Seat configuration ownership is reversible without consuming foreign edits."""
from __future__ import annotations

import base64
import importlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from floati.errors import ProtocolRefusal
from tests.temp_roots import REAL_TEMP_ROOT
from tests.seat_transport_fixture import create_transport
from tests.seat_toml_fixture import install_fixture_toml

try:
    config = importlib.import_module("floati.seat_harness_config")
except ModuleNotFoundError:
    config = None


class SeatHarnessConfigTests(unittest.TestCase):
    def setUp(self):
        install_fixture_toml(self)
        self.assertIsNotNone(config, "seat harness configuration helper is not implemented")
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.gateway = self.root / "profiles.json"
        self.codex = self.root / "config.toml"
        self.foreign = {"transport": "local", "git_common_dir": str(self.root / "foreign/.git"),
                        "bus_root": str(self.root / "bus"), "identity": "foreign",
                        "allowed_operations": ["inbox"], "send_repository": "fixture",
                        "send_recipients": ["architect"], "workspace_root": str(self.root / "foreign")}
        self.profile = {**self.foreign, "identity": "new-seat",
                        "git_common_dir": str(self.root / "seat/repo/.git"),
                        "workspace_root": str(self.root / "seat/repo")}
        self.transport = create_transport(self.root / "install")
        self.gateway_bytes = (json.dumps({"schema_version": 2, "transports": {"local": self.transport},
                                          "profiles": {"foreign": self.foreign}}, indent=4) + "\n").encode()
        self.codex_bytes = b'# operator comment\nmodel = "fixture"\n[hooks.state."stop-key"]\nenabled = true\ntrusted_hash = "fixture"\n'
        self.gateway.write_bytes(self.gateway_bytes)
        self.codex.write_bytes(self.codex_bytes)
        self.gateway.chmod(0o600)
        self.codex.chmod(0o640)

    def prepare(self):
        return config.prepare(self.gateway, self.codex, "new-profile", self.profile)

    def assert_refuses(self, operation):
        before = (self.gateway.read_bytes(), self.codex.read_bytes())
        with self.assertRaises(ProtocolRefusal) as caught:
            operation()
        self.assertEqual("seat_harness_config_invalid", caught.exception.code)
        self.assertIsInstance(caught.exception.remedy, str)
        self.assertTrue(caught.exception.remedy.strip())
        self.assertEqual(before, (self.gateway.read_bytes(), self.codex.read_bytes()))
        return caught.exception

    def test_prepare_is_read_only_and_plan_survives_json_roundtrip(self):
        plan = json.loads(json.dumps(self.prepare()))
        self.assertEqual(self.gateway_bytes, self.gateway.read_bytes())
        self.assertEqual(self.codex_bytes, self.codex.read_bytes())
        self.assertEqual(self.gateway_bytes, base64.b64decode(plan["gateway"]["baseline_base64"]))
        self.assertEqual(self.codex_bytes, base64.b64decode(plan["codex"]["baseline_base64"]))
        config.apply(plan)
        self.assertEqual(self.profile, json.loads(self.gateway.read_bytes())["profiles"]["new-profile"])
        self.assertEqual(self.foreign, json.loads(self.gateway.read_bytes())["profiles"]["foreign"])
        self.assertTrue(self.codex.read_bytes().startswith(self.codex_bytes))

    def test_exact_baseline_bytes_and_modes_return_on_restore(self):
        plan = self.prepare()
        config.apply(plan)
        config.preflight_restore(plan)
        config.restore(plan)
        self.assertEqual(self.gateway_bytes, self.gateway.read_bytes())
        self.assertEqual(self.codex_bytes, self.codex.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(self.gateway.stat().st_mode))
        self.assertEqual(0o640, stat.S_IMODE(self.codex.stat().st_mode))

    def test_sorted_plan_json_roundtrip_applies_and_restores(self):
        plan = json.loads(json.dumps(self.prepare(), sort_keys=True))
        config.apply(plan)
        config.restore(plan)
        self.assertEqual(self.gateway_bytes, self.gateway.read_bytes())
        self.assertEqual(self.codex_bytes, self.codex.read_bytes())

    def test_restore_preserves_foreign_profile_and_toml_edits(self):
        plan = self.prepare()
        config.apply(plan)
        document = json.loads(self.gateway.read_bytes())
        document["profiles"]["later"] = {**self.foreign, "identity": "later"}
        self.gateway.write_text(json.dumps(document) + "\n")
        self.codex.write_bytes(self.codex.read_bytes() + b'\n[projects."/fixture/another"]\ntrust_level = "trusted"\n# later operator note\n')
        config.restore(plan)
        document = json.loads(self.gateway.read_bytes())
        self.assertNotIn("new-profile", document["profiles"])
        self.assertIn("later", document["profiles"])
        self.assertIn(b'[projects."/fixture/another"]', self.codex.read_bytes())
        self.assertIn(b'# later operator note', self.codex.read_bytes())
        self.assertIn(b'[hooks.state."stop-key"]', self.codex.read_bytes())
        self.assertNotIn(str(self.root / "seat/repo").encode(), self.codex.read_bytes())

    def test_restore_replay_preserves_foreign_edits_when_owned_entries_are_already_absent(self):
        for rehome in (False, True):
            with self.subTest(rehome=rehome):
                self.gateway.write_bytes(self.gateway_bytes)
                self.codex.write_bytes(self.codex_bytes)
                self.gateway.chmod(0o600)
                if rehome:
                    self.rehome_baseline()
                plan = config.prepare(self.gateway, self.codex, "new-profile", self.profile,
                                      replace_profile=rehome)
                config.apply(plan)
                document = json.loads(self.gateway.read_bytes())
                document["profiles"]["later"] = {**self.foreign, "identity": "later"}
                self.gateway.write_text(json.dumps(document))
                self.gateway.chmod(0o640)
                self.codex.write_bytes(self.codex.read_bytes() + b'\n[projects."/fixture/later"]\ntrust_level = "trusted"\n')
                config.restore(plan)
                before = (self.gateway.read_bytes(), self.codex.read_bytes(), self.gateway.stat().st_ino,
                          self.codex.stat().st_ino)
                config.preflight_restore(plan)
                config.restore(plan)
                after = (self.gateway.read_bytes(), self.codex.read_bytes(), self.gateway.stat().st_ino,
                         self.codex.stat().st_ino)
                self.assertEqual(before, after)
                self.assertEqual(0o640, stat.S_IMODE(self.gateway.stat().st_mode))
                self.assertIn("later", json.loads(self.gateway.read_bytes())["profiles"])
                self.assertNotIn("new-profile", json.loads(self.gateway.read_bytes())["profiles"])

    def test_owned_profile_edit_refuses_restore_before_either_write(self):
        plan = self.prepare()
        config.apply(plan)
        document = json.loads(self.gateway.read_bytes())
        document["profiles"]["new-profile"]["identity"] = "changed"
        self.gateway.write_text(json.dumps(document))
        self.assert_refuses(lambda: config.restore(plan))

    def test_owned_trust_edit_refuses_preflight_and_restore(self):
        plan = self.prepare()
        config.apply(plan)
        self.codex.write_bytes(self.codex.read_bytes().replace(b'trust_level = "trusted"', b'trust_level = "untrusted"'))
        self.assert_refuses(lambda: config.preflight_restore(plan))
        self.assert_refuses(lambda: config.restore(plan))

    def test_changed_baseline_refuses_apply_before_either_write(self):
        plan = self.prepare()
        self.codex.write_bytes(self.codex_bytes + b'# later edit\n')
        self.assert_refuses(lambda: config.apply(plan))

    def test_tampered_backup_refuses_without_mutation(self):
        plan = self.prepare()
        plan["gateway"]["baseline_base64"] = base64.b64encode(b"{}").decode()
        self.assert_refuses(lambda: config.apply(plan))

    def test_duplicate_json_key_refuses_prepare(self):
        self.gateway.write_bytes(b'{"schema_version":2,"profiles":{},"profiles":{},"transports":{}}')
        self.assert_refuses(self.prepare)

    def test_malformed_toml_refuses_prepare(self):
        self.codex.write_bytes(b'[projects."broken"\n')
        self.assert_refuses(self.prepare)

    def test_existing_profile_workspace_and_identity_each_refuse(self):
        for case_index, (name, profile) in enumerate((("foreign", self.profile),
                              ("new-profile", {**self.profile, "workspace_root": self.foreign["workspace_root"]}),
                              ("new-profile", {**self.profile, "identity": "foreign"}))):
            with self.subTest(case=(
                "existing-profile-name",
                "conflicting-profile-workspace",
                "conflicting-profile-identity",
            )[case_index], name=name):
                self.assert_refuses(lambda: config.prepare(self.gateway, self.codex, name, profile))

    def test_existing_target_trust_table_refuses_even_untrusted(self):
        header = '[projects.' + json.dumps(self.profile["workspace_root"]) + ']\n'
        self.codex.write_bytes(self.codex_bytes + (header + 'trust_level = "untrusted"\n').encode())
        self.assert_refuses(self.prepare)

    def test_unknown_transport_or_profile_field_refuses(self):
        for profile in ({**self.profile, "transport": "absent"}, {**self.profile, "extra": True}):
            self.assert_refuses(lambda: config.prepare(self.gateway, self.codex, "new-profile", profile))

    def test_entire_transport_registry_is_validated_before_mutation(self):
        malformed = ({"kind": "floati-install-manifest-v0"},
                     {**self.transport, "source_sha": "wrong"},
                     {**self.transport, "manifest_sha256": "c" * 63},
                     {**self.transport, "manifest_path": str(self.root / "outside.json")},
                     {**self.transport, "interpreter": "relative"},
                     {**self.transport, "max_files": True},
                     {**self.transport, "max_file_bytes": 64 * 1024 * 1024 + 1},
                     {**self.transport, "max_total_bytes": 1})
        for case_index, transport in enumerate(malformed):
            with self.subTest(case=(
                "missing-transport-fields",
                "invalid-source-sha",
                "manifest-digest-length-63",
                "foreign-manifest-path",
                "relative-interpreter",
                "boolean-max-files",
                "max-file-bytes-64MiB-plus-one",
                "max-total-bytes-one",
            )[case_index]):
                document = json.loads(self.gateway_bytes)
                document["transports"]["other"] = transport
                self.gateway.write_text(json.dumps(document))
                self.assert_refuses(self.prepare)

    def test_entire_existing_profile_registry_is_validated_before_mutation(self):
        for case_index, profile in enumerate(({**self.foreign, "extra": True},
                        {**self.foreign, "transport": "absent"},
                        {**self.foreign, "allowed_operations": ["send", "ack"]},
                        {**self.foreign, "workspace_root": "relative"})):
            with self.subTest(case=(
                "profile-extra-field",
                "profile-absent-transport",
                "profile-send-and-ack-operations",
                "profile-relative-workspace",
            )[case_index]):
                document = json.loads(self.gateway_bytes)
                document["profiles"]["foreign"] = profile
                self.gateway.write_text(json.dumps(document))
                self.assert_refuses(self.prepare)

    def test_profile_lists_match_gateway_sorted_unique_contract(self):
        for profile in ({**self.profile, "allowed_operations": ["send", "ack"]},
                        {**self.profile, "send_recipients": ["worker", "architect"]}):
            self.assert_refuses(lambda: config.prepare(self.gateway, self.codex, "new-profile", profile))

    def test_profile_paths_must_be_json_strings(self):
        profile = {**self.profile, "workspace_root": Path(self.profile["workspace_root"])}
        self.assert_refuses(lambda: config.prepare(self.gateway, self.codex, "new-profile", profile))

    def test_non_owned_configuration_refuses_before_writing(self):
        with mock.patch.object(config.os, "getuid", return_value=os.getuid() + 1):
            self.assert_refuses(self.prepare)

    def test_private_backup_readback_failure_leaves_both_configs_untouched(self):
        plan = self.prepare()
        reader = config._read_at
        def bad_staging_readback(parent, name, label):
            content, mode = reader(parent, name, label)
            return (content + b"corrupt", mode) if name.startswith(".seat-config-") else (content, mode)
        with mock.patch.object(config, "_read_at", side_effect=bad_staging_readback):
            self.assert_refuses(lambda: config.apply(plan))
        self.assertEqual([], list(self.root.glob(".seat-config-*")))

    def test_symlink_file_and_parent_refuse_without_touching_target(self):
        link = self.root / "link.json"
        link.symlink_to(self.gateway)
        self.assert_refuses(lambda: config.prepare(link, self.codex, "new-profile", self.profile))
        directory = self.root / "linked"
        directory.symlink_to(self.root, target_is_directory=True)
        self.assert_refuses(lambda: config.prepare(directory / self.gateway.name, self.codex, "new-profile", self.profile))

    def test_relative_missing_and_oversized_configs_refuse(self):
        self.assert_refuses(lambda: config.prepare(Path("relative.json"), self.codex, "new-profile", self.profile))
        self.assert_refuses(lambda: config.prepare(self.root / "absent.json", self.codex, "new-profile", self.profile))
        self.codex.write_bytes(b"#" + b"x" * (1024 * 1024))
        self.assert_refuses(self.prepare)

    def test_nul_configuration_path_is_a_typed_refusal(self):
        self.assert_refuses(lambda: config.prepare(self.root / "nul\x00.json", self.codex, "new-profile", self.profile))

    def test_missing_optional_toml_parser_names_an_executable_remedy(self):
        def inventory():
            return {str(path.relative_to(self.root)): (path.stat().st_ino, stat.S_IMODE(path.stat().st_mode),
                    path.read_bytes() if path.is_file() else None) for path in self.root.rglob("*")}
        before = inventory()
        real_import = __import__
        def without_toml(name, *args, **kwargs):
            if name in ("tomllib", "tomli"):
                raise ImportError("fixture interpreter lacks TOML parser")
            return real_import(name, *args, **kwargs)
        with mock.patch("builtins.__import__", side_effect=without_toml):
            refusal = self.assert_refuses(self.prepare)
        self.assertIn("TOML validation is unavailable", refusal.detail)
        self.assertEqual(before, inventory())

    def test_atomic_readback_failure_preserves_foreign_changed_config(self):
        plan = self.prepare()
        real_replace = os.replace
        corrupted = []
        def corrupt_second(source, destination, **kwargs):
            real_replace(source, destination, **kwargs)
            if destination == self.codex.name and not corrupted:
                corrupted.append(True)
                self.codex.write_bytes(b"# corrupted publication\n")
        with mock.patch.object(config.os, "replace", side_effect=corrupt_second):
            with self.assertRaises(ProtocolRefusal) as caught:
                config.apply(plan)
        self.assertEqual(self.gateway_bytes, self.gateway.read_bytes())
        self.assertEqual(b"# corrupted publication\n", self.codex.read_bytes())
        self.assertIn("rollback incomplete", caught.exception.detail)
        self.assertEqual(self.codex_bytes, base64.b64decode(plan["codex"]["baseline_base64"]))
        self.assertEqual([], list(self.root.glob(".seat-config-*")))

    def test_publication_interruption_preserves_foreign_replacement(self):
        plan = self.prepare()
        saved_plan = json.dumps(plan, sort_keys=True)
        real_replace = os.replace
        foreign = b'{"operator": "foreign replacement after publication"}\n'
        injected = []
        def replace_then_foreign_write(source, destination, **kwargs):
            real_replace(source, destination, **kwargs)
            if destination == self.gateway.name and not injected:
                injected.append(True)
                self.gateway.write_bytes(foreign)
                self.gateway.chmod(0o640)
                raise OSError("fixture foreign write after first publication")
        with mock.patch.object(config.os, "replace", side_effect=replace_then_foreign_write):
            with self.assertRaises(ProtocolRefusal) as caught:
                config.apply(plan)
        self.assertEqual("seat_harness_config_invalid", caught.exception.code)
        self.assertIn("rollback incomplete", caught.exception.detail)
        self.assertIn(str(self.gateway), caught.exception.detail)
        self.assertEqual(foreign, self.gateway.read_bytes())
        self.assertEqual(0o640, stat.S_IMODE(self.gateway.stat().st_mode))
        self.assertEqual(self.codex_bytes, self.codex.read_bytes())
        self.assertEqual(saved_plan, json.dumps(plan, sort_keys=True))
        self.assertEqual(self.gateway_bytes, base64.b64decode(plan["gateway"]["baseline_base64"]))

    def test_rollback_preserves_foreign_mode_change_on_published_bytes(self):
        plan = self.prepare()
        added = base64.b64decode(plan["gateway"]["added_base64"])
        real_replace = os.replace
        injected = []
        def replace_then_chmod(source, destination, **kwargs):
            real_replace(source, destination, **kwargs)
            if destination == self.gateway.name and not injected:
                injected.append(True)
                self.gateway.chmod(0o640)
                raise OSError("fixture foreign chmod after publication")
        with mock.patch.object(config.os, "replace", side_effect=replace_then_chmod):
            with self.assertRaises(ProtocolRefusal) as caught:
                config.apply(plan)
        self.assertIn("rollback incomplete", caught.exception.detail)
        self.assertEqual(added, self.gateway.read_bytes())
        self.assertEqual(0o640, stat.S_IMODE(self.gateway.stat().st_mode))
        self.assertEqual(self.codex_bytes, self.codex.read_bytes())

    def test_interrupt_after_first_replace_rolls_back_both_configs(self):
        plan = self.prepare()
        real_replace = os.replace
        interrupted = []
        def interrupt_first(source, destination, **kwargs):
            real_replace(source, destination, **kwargs)
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt("fixture interruption after publication")
        with mock.patch.object(config.os, "replace", side_effect=interrupt_first):
            with self.assertRaises(KeyboardInterrupt):
                config.apply(plan)
        self.assertEqual(self.gateway_bytes, self.gateway.read_bytes())
        self.assertEqual(self.codex_bytes, self.codex.read_bytes())
        self.assertEqual([], list(self.root.glob(".seat-config-*")))

    def test_interrupt_during_private_staging_cleans_backup(self):
        plan = self.prepare()
        reader = config._read_at
        def interrupt_staging(parent, name, label):
            if name.startswith(".seat-config-"):
                raise KeyboardInterrupt("fixture interruption during backup readback")
            return reader(parent, name, label)
        with mock.patch.object(config, "_read_at", side_effect=interrupt_staging):
            with self.assertRaises(KeyboardInterrupt):
                config.apply(plan)
        self.assertEqual(self.gateway_bytes, self.gateway.read_bytes())
        self.assertEqual(self.codex_bytes, self.codex.read_bytes())
        self.assertEqual([], list(self.root.glob(".seat-config-*")))

    def rehome_baseline(self):
        old = {**self.profile, "workspace_root": str(self.root / "old/repo"),
               "git_common_dir": str(self.root / "old/repo/.git")}
        document = json.loads(self.gateway_bytes)
        document["profiles"]["new-profile"] = old
        self.gateway.write_bytes((json.dumps(document, indent=4) + "\n").encode())
        table = '[projects.' + json.dumps(old["workspace_root"]) + ']\ntrust_level = "trusted"\n'
        self.codex.write_bytes(self.codex_bytes + b"\n" + table.encode())
        return old

    def test_explicit_rehome_removal_retires_old_and_new_capabilities(self):
        old = self.rehome_baseline()
        baseline = self.gateway.read_bytes()
        plan = json.loads(json.dumps(config.prepare(self.gateway, self.codex, "new-profile", self.profile,
                                                   replace_profile=True), sort_keys=True))
        self.assertEqual(baseline, base64.b64decode(plan["gateway"]["baseline_base64"]))
        config.apply(plan)
        self.assertEqual(self.profile, json.loads(self.gateway.read_bytes())["profiles"]["new-profile"])
        self.assertNotIn(old["workspace_root"].encode(), self.codex.read_bytes())
        config.restore(plan)
        self.assertNotIn("new-profile", json.loads(self.gateway.read_bytes())["profiles"])
        self.assertEqual(self.foreign, json.loads(self.gateway.read_bytes())["profiles"]["foreign"])
        self.assertNotIn(old["workspace_root"].encode(), self.codex.read_bytes())
        self.assertNotIn(self.profile["workspace_root"].encode(), self.codex.read_bytes())
        self.assertIn(b'[hooks.state."stop-key"]', self.codex.read_bytes())

    def test_rehome_preserves_later_foreign_edits(self):
        old = self.rehome_baseline()
        plan = config.prepare(self.gateway, self.codex, "new-profile", self.profile, replace_profile=True)
        config.apply(plan)
        document = json.loads(self.gateway.read_bytes())
        document["profiles"]["later"] = {**self.foreign, "identity": "later"}
        self.gateway.write_text(json.dumps(document))
        self.codex.write_bytes(self.codex.read_bytes() + b'\n[projects."/fixture/later"]\ntrust_level = "trusted"\n')
        config.restore(plan)
        self.assertIn("later", json.loads(self.gateway.read_bytes())["profiles"])
        self.assertNotIn("new-profile", json.loads(self.gateway.read_bytes())["profiles"])
        self.assertIn(b'/fixture/later', self.codex.read_bytes())
        self.assertNotIn(old["workspace_root"].encode(), self.codex.read_bytes())
        self.assertNotIn(self.profile["workspace_root"].encode(), self.codex.read_bytes())

    def test_rehome_refuses_foreign_profile_identity_bus_and_schema(self):
        self.rehome_baseline()
        for replacement in ({**self.profile, "identity": "other"},
                            {**self.profile, "bus_root": str(self.root / "other-bus")}):
            self.assert_refuses(lambda: config.prepare(self.gateway, self.codex, "new-profile", replacement,
                                                       replace_profile=True))
        document = json.loads(self.gateway.read_bytes())
        del document["profiles"]["new-profile"]["allowed_operations"]
        self.gateway.write_text(json.dumps(document))
        self.assert_refuses(lambda: config.prepare(self.gateway, self.codex, "new-profile", self.profile,
                                                   replace_profile=True))

    def test_rehome_refuses_richer_or_ambiguous_old_trust_table(self):
        self.rehome_baseline()
        for suffix in (b'extra = "foreign"\n', b'# foreign annotation\n'):
            self.codex.write_bytes(self.codex.read_bytes() + suffix)
            self.assert_refuses(lambda: config.prepare(self.gateway, self.codex, "new-profile", self.profile,
                                                       replace_profile=True))
            self.codex.write_bytes(self.codex.read_bytes()[:-len(suffix)])

    def test_rehome_refuses_collisions_with_another_profile(self):
        self.rehome_baseline()
        document = json.loads(self.gateway.read_bytes())
        document["profiles"]["other"] = {**self.foreign, "workspace_root": self.profile["workspace_root"]}
        self.gateway.write_text(json.dumps(document))
        self.assert_refuses(lambda: config.prepare(self.gateway, self.codex, "new-profile", self.profile,
                                                   replace_profile=True))


if __name__ == "__main__":
    unittest.main()
