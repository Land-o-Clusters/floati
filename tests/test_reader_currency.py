"""Installed-reader currency is measured from declared fixtures, never host discovery."""
from __future__ import annotations

import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from floati import cli
from floati.errors import ProtocolRefusal
from tests import test_deploy as deploy_fixtures


class ReaderCurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = deploy_fixtures.DeploymentWriterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.source = self.fixture.source.resolve()
        self.base = self.fixture.base.resolve()
        self.destination = self.base / "installed"
        self.old_sha = self.fixture._git("rev-parse", "HEAD")
        self.fixture._writer(self.destination, committed_tree=True).run()
        self.registry = self.base / "fleet-bus-profiles.json"
        manifest = self.destination / ".floati-install/manifest.v0.json"
        self.registry.write_text(json.dumps({
            "schema_version": 2,
            "profiles": {"fixture": {"transport": "installed", "allowed_operations": ["attach", "board", "inbox"]}},
            "transports": {"installed": {"install_root": str(self.destination), "manifest_path": str(manifest),
                "source_sha": self.old_sha, "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()}},
        }, indent=2) + "\n")
        self.fixture._advance_source_without_the_schema()

    def currency_module(self):
        try:
            return importlib.import_module("floati.reader_currency")
        except ImportError:
            self.fail("governed install currency observation is missing")

    def test_declared_transport_reports_exact_age_and_update_remedy(self):
        rows = self.currency_module().governed_install_findings(
            self.source, "HEAD", profile_registry=self.registry)
        row = next(row for row in rows if row["code"] == "installed_reader_behind")
        self.assertEqual("warning", row["severity"])
        self.assertEqual(self.old_sha, row["installed_reader"]["source_sha"])
        self.assertEqual(1, row["installed_reader"]["commits_behind"])
        self.assertIn(str(self.source), row["remediation"])
        self.assertIn(str(self.destination), row["remediation"])
        self.assertIn("--profile-registry", row["remediation"])
        self.assertIn("--fleet-profile fixture", row["remediation"])

    def test_missing_install_reports_absence_without_inventing_age(self):
        document = json.loads(self.registry.read_text())
        document["transports"]["installed"]["install_root"] = str(self.base / "absent")
        document["transports"]["installed"]["manifest_path"] = str(self.base / "absent/.floati-install/manifest.v0.json")
        self.registry.write_text(json.dumps(document))
        rows = self.currency_module().governed_install_findings(self.source, "HEAD", profile_registry=self.registry)
        row = next(row for row in rows if row["code"] == "installed_reader_absent")
        self.assertIsNone(row["installed_reader"]["commits_behind"])

    def test_hook_bound_waiter_is_measured_even_without_transport(self):
        bundle = self.base / "floati-wake" / ("a" * 64)
        (bundle / "scripts").mkdir(parents=True)
        (bundle / "scripts/floati-codex-wait").write_text("fixture\n")
        (bundle / ".floati-installed-reader.json").write_text(json.dumps({
            "schema_version": 0, "source_sha": self.old_sha, "source_state": "measured"}))
        hooks = self.base / "hooks.json"
        hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command":
            f"/usr/bin/python3 {bundle}/scripts/floati-codex-wait wait --for fresh-work --root /fixture"}]}]}}))
        rows = self.currency_module().governed_install_findings(self.source, "HEAD", hooks=hooks)
        row = next(row for row in rows if row["code"] == "installed_reader_behind")
        self.assertEqual(1, row["installed_reader"]["commits_behind"])
        self.assertEqual("waiter", row["installed_reader"]["kind"])

    def test_doctor_wires_declared_profile_registry(self):
        from floati.doctor import Doctor
        from floati.root import FloatiRoot
        from tests.schema_validation import validate_json_schema
        root = self.base / "fleet"
        FloatiRoot.open_direct_home(root, create=True)
        config = self.base / "config.toml"
        config.write_text("")
        artifact, _ = Doctor(self.source, root, ref="HEAD", codex_config=config,
                             codex_hooks=self.base / "hooks.json", no_sandbox=True).artifact()
        self.assertTrue(any(row["code"] == "installed_reader_behind" for row in artifact["findings"]))
        validate_json_schema(artifact, Path(__file__).resolve().parents[1] / "schemas/v1/doctor-artifact.schema.json")

    def test_profile_bound_update_repins_only_selected_transport(self):
        from unittest.mock import patch
        before = json.loads(self.registry.read_text())
        args = cli._parser().parse_args(["update", "--source", str(self.source), "--destination", str(self.destination),
            "--ref", "HEAD", "--committed-tree", "--profile-registry", str(self.registry), "--fleet-profile", "fixture"])
        with patch.dict("os.environ", {"PATH": str(self.destination / "scripts") + ":/usr/bin:/bin"}):
            status, evidence, code = cli._update(args)
        self.assertEqual(("ok", 0), (status, code))
        after = json.loads(self.registry.read_text())
        self.assertEqual(before["profiles"], after["profiles"])
        self.assertEqual(self.fixture._git("rev-parse", "HEAD"), after["transports"]["installed"]["source_sha"])
        metadata = self.destination / ".floati-install/manifest.v0.json"
        self.assertEqual(hashlib.sha256(metadata.read_bytes()).hexdigest(), after["transports"]["installed"]["manifest_sha256"])
        self.assertIn("profile_transport", evidence)

    def test_unsupported_profile_operation_refuses_before_install_mutation(self):
        from unittest.mock import patch
        document = json.loads(self.registry.read_text())
        document["profiles"]["fixture"]["allowed_operations"].append("turnover")
        self.registry.write_text(json.dumps(document))
        before = self.fixture._tree_bytes(self.destination)
        args = cli._parser().parse_args(["update", "--source", str(self.source), "--destination", str(self.destination),
            "--ref", "HEAD", "--committed-tree", "--profile-registry", str(self.registry), "--fleet-profile", "fixture"])
        with self.assertRaises(ProtocolRefusal) as caught:
            cli._update(args)
        self.assertEqual("profile_operations_unsupported", caught.exception.code)
        self.assertIn("turnover", caught.exception.detail)
        self.assertIsInstance(caught.exception.remedy, str)
        self.assertEqual(before, self.fixture._tree_bytes(self.destination))

    def test_repin_failure_keeps_completed_install_evidence_and_reports_degraded(self):
        from unittest.mock import patch
        args = cli._parser().parse_args(["update", "--source", str(self.source), "--destination", str(self.destination),
            "--ref", "HEAD", "--committed-tree", "--profile-registry", str(self.registry), "--fleet-profile", "fixture"])
        with patch.dict("os.environ", {"PATH": str(self.destination / "scripts") + ":/usr/bin:/bin"}), patch(
            "floati.profile_update.finish_profile_update", side_effect=ProtocolRefusal("fleet_update_transport_registry_drift", "changed", "retry")):
            status, evidence, code = cli._update(args)
        self.assertEqual(("degraded", 35), (status, code))
        self.assertEqual("updated", evidence["status"])
        self.assertEqual("profile_update_repin_incomplete", evidence["profile_transport"]["code"])
        self.assertEqual(self.fixture._git("rev-parse", "HEAD"), evidence["source_sha"])

    def test_instruction_drift_is_reported_against_current_source(self):
        asset_source = self.source / "tools/codex/boardbus.md"
        asset_source.parent.mkdir(parents=True)
        asset_source.write_text("current boarding contract\n")
        host = self.base / "bin/boardbus.md"
        host.parent.mkdir()
        host.write_text("stale turnover instruction\n")
        receipt = self.base / "gateway-receipt.json"
        receipt.write_text(json.dumps({"evidence": {"instruction_assets": [{
            "destination": str(host), "source": str(asset_source),
            "source_sha256": hashlib.sha256(host.read_bytes()).hexdigest(),
            "installed_sha256": hashlib.sha256(host.read_bytes()).hexdigest(),
        }]}}))
        rows = self.currency_module().governed_install_findings(self.source, "HEAD", gateway_receipt=receipt)
        self.assertTrue(any(row["code"] == "host_codex_instruction_vendored_source_drift" for row in rows))

    def test_shallow_history_never_emits_a_commit_distance(self):
        (self.source / ".git/shallow").write_text(self.old_sha + "\n")
        rows = self.currency_module().governed_install_findings(self.source, "HEAD", profile_registry=self.registry)
        row = next(row for row in rows if row.get("installed_reader"))
        self.assertEqual("installed_reader_source_unavailable", row["code"])
        self.assertIsNone(row["installed_reader"]["commits_behind"])

    def test_reader_newer_than_named_ref_is_not_diverged(self):
        module = self.currency_module()
        metadata = self.destination / ".floati-install/manifest.v0.json"
        document = json.loads(metadata.read_text())
        document["source_sha"] = self.fixture._git("rev-parse", "HEAD")
        metadata.write_text(json.dumps(document))
        row = module.installed_reader_finding(self.source, self.old_sha, self.destination, "transport", metadata)
        self.assertEqual("installed_reader_ahead", row["code"])
        self.assertEqual("ahead", row["installed_reader"]["state"])
        self.assertIsNone(row["installed_reader"]["commits_behind"])
