"""Doctor aggregates keep captured refusal evidence without changing outcomes."""
from __future__ import annotations

import tests  # Pin HOME before importing fixture or product code.

import json
import unittest
from unittest.mock import patch

from floati.doctor import Doctor, project_seat_declaration_findings, project_wake_daemon_health
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.storage_identity import INSTALL_METADATA_DIRECTORY
from tests import test_doctor as fixtures


def lock_refusal(holder="unknown"):
    return ProtocolRefusal("ledger_lock_timeout", "fixture ledger lock contended", {
        "kind": "transient", "action": "retry_once", "delay_seconds": 1,
        "holder": holder,
    })


class DoctorTransientRemedyTests(unittest.TestCase):
    def setUp(self):
        # Reuse the established scratch source/root fixture, not its test methods.
        self.fixture = fixtures.DoctorContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def doctor(self, **kwargs):
        return self.fixture.doctor(no_sandbox=True, **kwargs)

    def artifact(self, **kwargs):
        try:
            return self.doctor(**kwargs).artifact()
        except ProtocolRefusal as exc:
            self.fail(f"aggregate was raised away: {exc.code}")

    def assert_remedy(self, row, refusal):
        self.assertEqual(refusal.remedy, row.get("remedy"))
        if isinstance(refusal.remedy, dict):
            self.assertIsNot(refusal.remedy, row["remedy"])

    def test_two_transient_captured_findings_keep_each_remedy_and_outcome(self):
        """Dropping one finding's typed retry destroys aggregate classification."""
        workspace = lock_refusal("workspace-holder")
        currency = lock_refusal("currency-holder")
        with patch("floati.workspace_layout.inspect_workspace_layout", side_effect=workspace),\
             patch("floati.doctor._git", side_effect=currency):
            artifact, rc = self.artifact()
        rows = [r for r in artifact["findings"] if r["code"] == "ledger_lock_timeout"]
        self.assertEqual(2, len(rows))
        self.assert_remedy(rows[0], workspace)
        self.assert_remedy(rows[1], currency)
        self.assertEqual(35, rc)
        self.assertEqual("degraded", artifact["state"])
        workspace.remedy["delay_seconds"] = 2
        self.assertEqual(1, rows[0]["remedy"]["delay_seconds"])

    def test_transient_plus_fatal_keeps_fatal_outcome_and_both_remedies(self):
        transient = lock_refusal()
        fatal = ProtocolRefusal("gateway_config_invalid", "fatal fixture config", "provide exact config")
        with patch("floati.workspace_layout.inspect_workspace_layout", side_effect=transient),\
             patch("floati.doctor.GatewayConfig.load", side_effect=fatal):
            artifact, rc = self.artifact(gateway_config=self.fixture.base / "gateway.json")
        rows = {r["code"]: r for r in artifact["findings"]}
        self.assert_remedy(rows[transient.code], transient)
        self.assert_remedy(rows[fatal.code], fatal)
        self.assertEqual(20, rc)
        self.assertEqual("refused", artifact["state"])
        self.assertEqual("provide an exact local stdio gateway v0 config, then rerun doctor",
                         rows[fatal.code]["remediation"])

    def test_swallowed_seat_refusal_becomes_a_finding(self):
        refusal = lock_refusal()
        with patch("floati.registry.Registry.active_node_ids", side_effect=refusal):
            rows = project_seat_declaration_findings(self.fixture.root)
        self.assertEqual(1, len(rows))
        self.assertEqual(refusal.code, rows[0]["code"])
        self.assert_remedy(rows[0], refusal)

    def test_transformed_wake_coordinate_finding_keeps_diagnostic_and_original_code(self):
        directory = self.fixture.root.path / "state/wake-daemon/adapters" / self.fixture.node
        directory.mkdir(parents=True)
        (directory / "codex.json").write_text(json.dumps({"session_id": "fixture"}))
        refusal = lock_refusal()
        with patch("floati.wake_daemon_contract.DaemonCoordinate", side_effect=refusal):
            rows = project_wake_daemon_health(self.fixture.root, currency_current=False)
        self.assertEqual("wake_daemon_health", rows[0]["code"])
        self.assertEqual(refusal.code, rows[0].get("refusal_code"))
        self.assert_remedy(rows[0], refusal)
        self.assertIsNone(rows[0]["remediation"])

    def test_wrapped_fleet_update_refusal_keeps_diagnostic_and_remedy(self):
        refusal = lock_refusal()
        directory = self.fixture.root.path / "receipts/fleet-update"
        directory.mkdir(parents=True)
        (directory / "fixture.jsonl").write_text("{}\n")
        with patch("floati.fleet_update_receipts.FleetUpdateReceiptLedger.rows", side_effect=refusal):
            artifact, rc = self.artifact()
        rows = [r for r in artifact["findings"] if r["code"] == "fleet_update_receipt_invalid"]
        self.assertEqual(1, len(rows))
        self.assertEqual(refusal.code, rows[0].get("refusal_code"))
        self.assert_remedy(rows[0], refusal)
        self.assertEqual({
            "state": "integrity_failure", "code": "fleet_update_receipt_invalid",
            "refusal_code": refusal.code, "remedy": refusal.remedy,
        }, artifact["fleet_update"])
        self.assertIsNot(rows[0]["remedy"], artifact["fleet_update"]["remedy"])
        refusal.remedy["delay_seconds"] = 2
        self.assertEqual(1, artifact["fleet_update"]["remedy"]["delay_seconds"])
        self.assertEqual(33, rc)

    def test_other_captured_paths_keep_typed_remedies(self):
        (self.fixture.root.path / "state").mkdir(parents=True, exist_ok=True)
        (self.fixture.root.path / "state/lanes-root.json").write_text("{}")
        metadata = self.fixture.destination / INSTALL_METADATA_DIRECTORY / "manifest.v0.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("{}")
        hooks = self.fixture.base / "hooks.json"
        for target, kwargs in (
            ("floati.bus_epoch.reconcile_epoch_roll", {}),
            ("floati.doctor.read_records_snapshot", {}),
            ("floati.lane_workspaces.LaneWorkspaces.doctor", {}),
            ("floati.doctor.project_update_findings", {}),
            ("floati.codex_hook_trust.observe_codex_waiter_hooks", {"codex_hooks": hooks}),
        ):
            with self.subTest(target=target):
                refusal = lock_refusal()
                with patch(target, side_effect=refusal):
                    artifact, _rc = self.artifact(**kwargs)
                rows = [r for r in artifact["findings"] if r.get("code") == refusal.code]
                self.assertTrue(rows, target)
                for row in rows:
                    self.assert_remedy(row, refusal)

    def test_registry_and_delivery_readers_keep_aggregate_instead_of_raising(self):
        for target in ("floati.doctor.read_registry_compatible",
                       "floati.events.EventLog._compatible_event_records_with_skew"):
            with self.subTest(target=target):
                refusal = lock_refusal()
                with patch(target, side_effect=refusal):
                    artifact, rc = self.artifact()
                rows = [r for r in artifact["findings"] if r["code"] == refusal.code]
                self.assertTrue(rows)
                for row in rows:
                    self.assert_remedy(row, refusal)
                self.assertEqual(20, rc)

    def test_transient_with_nested_integrity_failure_lists_both_members(self):
        transient = lock_refusal()
        fatal = IntegrityFailure("fleet_update_receipt_invalid", "fatal receipt")
        with patch("floati.workspace_layout.inspect_workspace_layout", side_effect=transient),\
             patch("floati.doctor.project_fleet_update", side_effect=fatal):
            artifact, rc = self.artifact()
        rows = {r["code"]: r for r in artifact["findings"]}
        self.assertIn(fatal.code, rows)
        self.assertEqual("error", rows[fatal.code]["severity"])
        self.assertEqual(fatal.detail, rows[fatal.code]["detail"])
        self.assertNotIn("remedy", rows[fatal.code])
        self.assert_remedy(rows[transient.code], transient)
        self.assertEqual({"state": "integrity_failure", "code": fatal.code}, artifact["fleet_update"])
        self.assertEqual(33, rc)
        self.assertEqual("malformed_evidence", artifact["state"])

    def test_later_transient_does_not_replace_an_earlier_integrity_outcome(self):
        transient = lock_refusal()
        fatal = IntegrityFailure("workspace_evidence_invalid", "fatal workspace")
        with patch("floati.workspace_layout.inspect_workspace_layout", side_effect=fatal),\
             patch("floati.doctor.read_records_snapshot", side_effect=transient):
            artifact, rc = self.artifact()
        rows = {r["code"]: r for r in artifact["findings"]}
        self.assertIn(fatal.code, rows)
        self.assert_remedy(rows[transient.code], transient)
        self.assertEqual(33, rc)
        self.assertEqual("malformed_evidence", artifact["state"])

    def test_successful_projection_without_captured_refusals_stays_unchanged(self):
        # Wake health stamps observation time; disk watch measures live free
        # space. Freeze both so the rest of the artifact is the comparison.
        frozen_disk = {
            "exit": 0,
            "floor_bytes": 1 << 30,
            "free_bytes": 1 << 31,
            "build_bytes": 0,
            "trees": [],
            "partial": False,
            "reason": None,
            "build_names": [],
        }
        with patch("floati.doctor._utc_now", return_value=fixtures.NOW), patch(
            "floati.doctor_disk_watch.observe_disk_watch", return_value=frozen_disk
        ):
            first, first_rc = self.artifact()
            second, second_rc = self.artifact()
        self.assertEqual(first_rc, second_rc)
        self.assertEqual(first, second)
        self.assertFalse(any("remedy" in r or "refusal_code" in r for r in first["findings"]))

    def test_probe_root_refusal_keeps_code_remedy_and_error_finding(self):
        doctor = self.doctor()
        refusal = lock_refusal()
        with patch("floati.doctor.FloatiRoot.open_direct_home", side_effect=refusal):
            artifact, rc = doctor.probe()
        self.assertEqual(20, rc)
        self.assertEqual("refused", artifact["state"])
        self.assertEqual(refusal.detail, artifact["detail"])
        self.assertEqual(refusal.code, artifact.get("code"))
        self.assert_remedy(artifact, refusal)
        self.assertEqual(1, len(artifact["findings"]))
        self.assertEqual("error", artifact["findings"][0]["severity"])
        self.assert_remedy(artifact["findings"][0], refusal)
        refusal.remedy["delay_seconds"] = 2
        self.assertEqual(1, artifact["remedy"]["delay_seconds"])
        self.assertEqual(1, artifact["findings"][0]["remedy"]["delay_seconds"])
