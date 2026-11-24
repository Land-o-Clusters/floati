"""Scratch-only journal validation; no harness execution or real configuration."""
from __future__ import annotations

import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path

from floati import seat_harness_config, seat_onboarding
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.registry import utc_now
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.seat_transport_fixture import create_transport
from tests.seat_toml_fixture import install_fixture_toml


class SeatOnboardRecordTests(unittest.TestCase):
    def setUp(self):
        install_fixture_toml(self)
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.home = self.base / "seat"
        self.gateway = self.base / "profiles.json"
        self.codex = self.base / "config.toml"
        self.transport = create_transport(self.base / "install")
        self.gateway.write_text(json.dumps({"schema_version": 2, "profiles": {}, "transports": {"fixture": self.transport}}))
        self.codex.write_text('model = "fixture"\n')
        workspace = self.home / "repo"
        profile = {"transport": "fixture", "git_common_dir": str(workspace / ".git"),
            "workspace_root": str(workspace), "bus_root": str(self.root.path), "identity": "seat-a",
            "allowed_operations": ["ack", "attach", "board", "inbox", "send"],
            "send_repository": "fixture", "send_recipients": ["architect"]}
        plan = seat_harness_config.prepare(self.gateway, self.codex, "fixture-seat-a", profile)
        timestamp = utc_now()
        self.record = {"schema_version": 1, "root": str(self.root.path), "home": str(self.home),
            "node_id": "seat-a", "profile": "fixture-seat-a", "state": "preparing", "timestamp": timestamp,
            "config_plan": plan, "map_before": None, "steps": [], "rehome": False,
            "previous_registry_entry": None,
            "registry_row": {"schema_version": 0, "id": "registry-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": timestamp, "kind": "registry_entry",
                "node_id": "seat-a", "role": "Codex", "state": "active"},
            "request": {"source": str(self.base / "source"), "source_sha": "c" * 40,
                "gateway_config": str(self.gateway), "codex_config": str(self.codex),
                "codex_hooks": str(self.base / "hooks.json"),
                "transport": "fixture", "coordinator": "architect", "repo": "fixture", "mode": "desktop",
                "requested_node": "seat-a", "requested_profile": "fixture-seat-a",
                "codex_executable": None, "warm_build_script": None, "rehome": False}}
        self.path = seat_onboarding._record_path(self.root, self.home)

    def load(self, record):
        seat_onboarding._save(self.path, record)
        return seat_onboarding._load(self.path, self.root, self.home)

    def assert_refused_without_configuration_write(self, record):
        before = {path: path.read_bytes() for path in (self.gateway, self.codex)}
        with self.assertRaises(ProtocolRefusal):
            self.load(record)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertFalse(self.home.exists())

    def test_effect_free_preparing_record_without_stage_is_readable(self):
        self.assertEqual(self.record, self.load(self.record))

    def test_valid_planned_stage_without_identity_is_readable(self):
        record = {**self.record, "stage": str(self.base / (".seat-add-" + uuid7_hex())), "stage_identity": None}
        self.assertEqual(record, self.load(record))

    def test_effect_free_legacy_removal_can_be_replayed_without_stage(self):
        result = {"state": "removed", "node_id": "seat-a", "home": str(self.home),
            "before_inventory": {}, "after_inventory": {}}
        record = {**self.record, "state": "removed", "remove_result": result}
        self.assertEqual(record, self.load(record))

    def test_configuration_path_cannot_redirect_removal_to_foreign_file(self):
        record = copy.deepcopy(self.record)
        foreign = self.base / "foreign.toml"
        foreign.write_bytes(self.codex.read_bytes())
        record["config_plan"]["codex"]["path"] = str(foreign)
        self.assert_refused_without_configuration_write(record)
        self.assertEqual(self.codex.read_bytes(), foreign.read_bytes())

    def test_valid_foreign_configuration_plan_cannot_change_seat_identity(self):
        record = copy.deepcopy(self.record)
        profile = {**record["config_plan"]["profile"], "identity": "foreign-seat"}
        record["config_plan"] = seat_harness_config.prepare(self.gateway, self.codex, "foreign-profile", profile)
        self.assert_refused_without_configuration_write(record)

    def test_registry_ownership_cannot_name_another_node(self):
        record = copy.deepcopy(self.record)
        record["registry_row"]["node_id"] = "foreign-seat"
        self.assert_refused_without_configuration_write(record)

    def test_map_backup_requires_valid_base64_and_matching_tenant(self):
        for backup in ("not-base64", base64.b64encode(json.dumps({"schema_version": 0,
                "tenant_id": "foreign", "mappings": []}).encode()).decode()):
            with self.subTest(backup=backup):
                self.assert_refused_without_configuration_write({**self.record, "map_before": backup})

    def test_effect_prefix_cannot_skip_home_ownership(self):
        self.assert_refused_without_configuration_write({**self.record, "steps": ["config"]})

    def test_staging_and_removal_paths_cannot_escape_derived_coordinates(self):
        for field, value in (("stage", str(self.base / "foreign")), ("removal_stage", str(self.base / "foreign"))):
            with self.subTest(field=field):
                self.assert_refused_without_configuration_write({**self.record, field: value})

    def test_inventory_cannot_authorize_a_parent_path_or_special_file(self):
        for value in ({"../foreign": {"type": "file", "mode": 0o600, "sha256": "a" * 64}},
                {"repo/socket": {"type": "socket"}}):
            with self.subTest(inventory=value):
                self.assert_refused_without_configuration_write({**self.record, "inventory": value})

    def test_missing_fields_wrong_types_and_duplicate_json_are_typed(self):
        for field, value in (("schema_version", True), ("steps", None), ("request", []), ("registry_row", None)):
            with self.subTest(field=field):
                self.assert_refused_without_configuration_write({**self.record, field: value})
        incomplete = copy.deepcopy(self.record)
        del incomplete["registry_row"]
        self.assert_refused_without_configuration_write(incomplete)
        seat_onboarding._save(self.path, self.record)
        encoded = self.path.read_text().rstrip()
        self.path.write_text(encoded[:-1] + ', "steps": []}')
        with self.assertRaises(ProtocolRefusal):
            seat_onboarding._load(self.path, self.root, self.home)

    def test_output_journal_cannot_invent_ownership_or_a_finalized_cut(self):
        cases = [
            {"kind": "warm", "state": "finalized", "before_inventory": {}, "output_inventory": {}},
            {"kind": "clone", "state": "pending", "before_inventory": {}, "output_inventory": {}},
            {"kind": "clone", "state": "finalized", "before_inventory": {},
                "output_inventory": {"scratch/personal.txt": {"type": "file", "mode": 0o600, "sha256": "a" * 64}}},
            {"kind": "clone", "state": "finalized", "before_inventory": {},
                "output_inventory": {"repo": {"type": "directory", "mode": 0o700}}},
        ]
        for child in cases:
            with self.subTest(child=child):
                self.assert_refused_without_configuration_write({**self.record, "inventory": {}, "child_outputs": [child]})


if __name__ == "__main__":
    unittest.main()
