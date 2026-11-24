"""Installed-reader currency is measured from declared fixtures, never host discovery."""
from __future__ import annotations

import hashlib
import importlib
import json
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def source_clone(self, name, *, shallow=False):
        clone = self.base / name
        command = ["/usr/bin/git", "clone", "--quiet", "--no-local"]
        if shallow:
            command += ["--depth", "1"]
        subprocess.run(command + [str(self.source), str(clone)], check=True)
        result = subprocess.run(["/usr/bin/git", "-C", str(clone), "rev-parse",
                                 "--is-shallow-repository"], check=True,
                                capture_output=True, text=True)
        self.assertEqual("true" if shallow else "false", result.stdout.strip())
        return clone

    def assert_fetch_only(self, row, clone, *, shallow=False):
        expected = ["git", "-C", str(clone), "fetch"]
        if shallow:
            expected += ["--unshallow"]
        expected += ["origin"]
        self.assertEqual("installed_reader_source_unavailable", row["code"])
        self.assertEqual("unavailable", row["installed_reader"]["state"])
        self.assertIsNone(row["installed_reader"]["commits_behind"])
        self.assertEqual(expected, shlex.split(row["remediation"]))
        self.assertNotIn("floati update", row["remediation"])
        self.assertNotIn("--profile-registry", row["remediation"])
        self.assertNotIn("--fleet-profile", row["remediation"])
        self.assertNotIn("attach", row["remediation"])

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

    def test_unfetched_transport_and_waiter_name_fetch_without_profile_flags(self):
        clone = self.base / "unfetched clone"
        subprocess.run(["/usr/bin/git", "clone", "--quiet", "--no-local",
                        str(self.source), str(clone)], check=True)
        self.fixture._write_source("floati/__init__.py", "VERSION = 'third'\n")
        self.fixture._git("add", ".")
        self.fixture._git("commit", "--quiet", "-m", "installed after clone")
        installed = self.fixture._git("rev-parse", "HEAD")
        absent = subprocess.run(["/usr/bin/git", "-C", str(clone), "cat-file",
                                 "-e", installed + "^{commit}"], capture_output=True)
        self.assertNotEqual(0, absent.returncode)
        metadata = self.destination / ".floati-install/manifest.v0.json"
        document = json.loads(metadata.read_text())
        document["source_sha"] = installed
        metadata.write_text(json.dumps(document))
        module = self.currency_module()
        for kind in ("transport", "waiter"):
            with self.subTest(kind=kind):
                row = module.installed_reader_finding(clone, "HEAD", self.destination, kind, metadata)
                self.assertEqual("installed_reader_source_unavailable", row["code"])
                self.assertIsNone(row["installed_reader"]["commits_behind"])
                self.assertEqual(["git", "-C", str(clone), "fetch", "origin"],
                                 shlex.split(row["remediation"]))
                self.assertIn(str(clone), row["detail"])
                self.assertIn(installed, row["detail"])
        rows = module.governed_install_findings(clone, "HEAD", profile_registry=self.registry)
        row = next(row for row in rows if row["code"] == "installed_reader_source_unavailable")
        self.assertEqual(["git", "-C", str(clone), "fetch", "origin"],
                         shlex.split(row["remediation"]))

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
        clone = self.source_clone("shallow source clone", shallow=True)
        rows = self.currency_module().governed_install_findings(clone, "HEAD", profile_registry=self.registry)
        row = next(row for row in rows if row.get("installed_reader"))
        self.assert_fetch_only(row, clone, shallow=True)
        self.assertEqual(self.old_sha, row["installed_reader"]["source_sha"])
        self.assertIsNone(row["installed_reader"]["reference_sha"])

    def test_missing_origin_main_names_fetch_for_both_declared_reader_kinds(self):
        clone = self.source_clone("missing reference clone")
        missing = subprocess.run(["/usr/bin/git", "-C", str(clone), "rev-parse",
                                  "--verify", "origin/main^{commit}"], capture_output=True)
        self.assertNotEqual(0, missing.returncode)
        metadata = self.destination / ".floati-install/manifest.v0.json"
        for kind in ("transport", "waiter"):
            with self.subTest(kind=kind):
                row = self.currency_module().installed_reader_finding(
                    clone, "origin/main", self.destination, kind, metadata)
                self.assert_fetch_only(row, clone)
                self.assertEqual(self.old_sha, row["installed_reader"]["source_sha"])
                self.assertIsNone(row["installed_reader"]["reference_sha"])

    def test_hooks_only_doctor_names_fetch_for_unavailable_declared_source(self):
        from floati.codex_hook_install import format_waiter_stop_command
        from floati.doctor import Doctor
        from floati.root import FloatiRoot
        from tests.schema_validation import validate_json_schema

        fleet = self.base / "doctor-fleet"
        FloatiRoot.open_direct_home(fleet, create=True)
        for index, condition in enumerate(("shallow", "missing reference", "missing commit")):
            with self.subTest(condition=condition):
                clone = self.source_clone(condition + " doctor clone", shallow=condition == "shallow")
                installed = self.old_sha
                ref = "origin/main"
                reference_sha = None
                if condition == "missing commit":
                    reference_sha = subprocess.run(["/usr/bin/git", "-C", str(clone),
                        "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                    self.fixture._write_source("floati/__init__.py", "VERSION = 'after clone'\n")
                    self.fixture._git("add", "floati/__init__.py")
                    self.fixture._git("commit", "--quiet", "-m", "installed after doctor clone")
                    installed = self.fixture._git("rev-parse", "HEAD")
                    ref = "HEAD"
                elif condition == "missing reference":
                    absent = subprocess.run(["/usr/bin/git", "-C", str(clone), "rev-parse",
                        "--verify", "origin/main^{commit}"], capture_output=True)
                    self.assertNotEqual(0, absent.returncode)
                coordinate = self.base / (condition + " doctor coordinates")
                bundle = coordinate / "floati-wake" / ("abc"[index] * 64)
                (bundle / "scripts").mkdir(parents=True)
                launcher = bundle / "scripts/floati-codex-wait"
                launcher.write_text("fixture\n")
                (bundle / ".floati-installed-reader.json").write_text(json.dumps({
                    "schema_version": 0, "source_sha": installed, "source_state": "measured"}))
                config = coordinate / "config.toml"
                config.write_text("")
                hooks = coordinate / "hooks.json"
                hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
                    "command": format_waiter_stop_command(launcher, fleet)}]}]}}))
                self.assertFalse(config.with_name("fleet-bus-profiles.json").exists())
                artifact, _ = Doctor(clone, fleet, ref=ref, codex_config=config,
                    codex_hooks=hooks, codex_gateway_host=coordinate / "codex-fleet-bus",
                    no_sandbox=True).artifact()
                readers = [row for row in artifact["findings"] if row.get("installed_reader")]
                self.assertEqual(1, len(readers))
                row = readers[0]
                self.assertEqual(str(bundle), row["subject"])
                self.assertEqual("warning", row["severity"])
                self.assertEqual("waiter", row["installed_reader"]["kind"])
                self.assertEqual(installed, row["installed_reader"]["source_sha"])
                self.assertEqual(reference_sha, row["installed_reader"]["reference_sha"])
                self.assert_fetch_only(row, clone, shallow=condition == "shallow")
                validate_json_schema(artifact, Path(__file__).resolve().parents[1] /
                                     "schemas/v1/doctor-artifact.schema.json")

    def test_invalid_metadata_keeps_installer_remedy_before_history_inspection(self):
        metadata = self.destination / ".floati-install/manifest.v0.json"
        document = json.loads(metadata.read_text())
        document["source_sha"] = "not a commit"
        metadata.write_text(json.dumps(document))
        module = self.currency_module()
        for kind in ("transport", "waiter"):
            with self.subTest(kind=kind), mock.patch.object(module, "is_shallow_repository",
                    side_effect=AssertionError("invalid metadata must not inspect source history")):
                row = module.installed_reader_finding(self.source, "origin/main", self.destination,
                                                      kind, metadata)
                self.assertEqual("installed_reader_source_unavailable", row["code"])
                self.assertIsNone(row["installed_reader"]["source_sha"])
                self.assertIn("floati update --source " + shlex.quote(str(self.source)), row["remediation"])
                self.assertNotIn("fetch", row["remediation"])

    def test_unavailable_git_inspection_keeps_installer_remedy_and_typed_shallow_refusal(self):
        module = self.currency_module()
        for failure in (OSError("scratch inspection unavailable"), subprocess.TimeoutExpired("git", 15)):
            for kind in ("transport", "waiter"):
                with self.subTest(failure=type(failure).__name__, kind=kind),\
                        mock.patch.object(module, "_git", side_effect=failure):
                    row = module.installed_reader_finding(self.source, "HEAD", self.destination, kind)
                    self.assertEqual("installed_reader_source_unavailable", row["code"])
                    self.assertIn("floati update --source " + shlex.quote(str(self.source)), row["remediation"])
                    self.assertNotIn("fetch", row["remediation"])
        refusal = ProtocolRefusal("git_shallow_state_unavailable", "scratch shallow inspection refused")
        with mock.patch.object(module, "is_shallow_repository", side_effect=refusal),\
                self.assertRaises(ProtocolRefusal) as caught:
            module.installed_reader_finding(self.source, "HEAD", self.destination, "transport")
        self.assertIs(refusal, caught.exception)

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

    def _plant_waiter(self, *, link_name="current", target_name=None, chain=False, escape=False):
        import shutil

        store = self.base / "floati-wake"
        if store.exists():
            shutil.rmtree(store)
        digest = store / ("b" * 64)
        (digest / "scripts").mkdir(parents=True)
        (digest / "scripts/floati-codex-wait").write_text("fixture\n")
        (digest / ".floati-installed-reader.json").write_text(json.dumps({
            "schema_version": 0, "source_sha": self.old_sha, "source_state": "measured"}))
        link = store / link_name
        if escape:
            link.symlink_to("../outside")
        elif chain:
            inner = store / "inner"
            inner.symlink_to(digest.name)
            link.symlink_to(inner.name)
        else:
            link.symlink_to(digest.name if target_name is None else target_name)
            if target_name is not None and target_name != digest.name:
                decoy = store / target_name
                if not decoy.exists():
                    decoy.mkdir()
        hooks = self.base / "hooks.json"
        hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command":
            f"/usr/bin/python3 {link}/scripts/floati-codex-wait wait --for fresh-work --root /fixture"}]}]}}))
        return hooks, link, digest

    def test_current_alias_waiter_is_measured_through_the_digest(self):
        """Product Stop hooks name current/; a symlink root must not refuse before history."""

        hooks, _, digest = self._plant_waiter()
        rows = self.currency_module().governed_install_findings(self.source, "HEAD", hooks=hooks)
        row = next(row for row in rows if row.get("installed_reader"))
        self.assertEqual("installed_reader_behind", row["code"])
        self.assertEqual(1, row["installed_reader"]["commits_behind"])
        self.assertEqual("waiter", row["installed_reader"]["kind"])
        self.assertEqual(self.old_sha, row["installed_reader"]["source_sha"])
        self.assertTrue(digest.is_dir())

    def test_current_alias_refuses_escape_chain_and_non_digest(self):
        module = self.currency_module()
        cases = (
            ("escapes", dict(escape=True), "installed_reader_alias_escapes"),
            ("chained", dict(chain=True), "installed_reader_alias_chained"),
            ("non-digest", dict(target_name="latest"), "installed_reader_alias_non_digest"),
        )
        for label, plant, code in cases:
            with self.subTest(label):
                hooks, link, _ = self._plant_waiter(**plant)
                row = module.installed_reader_finding(self.source, "HEAD", link, "waiter")
                self.assertEqual(code, row["code"])
                self.assertEqual("unavailable", row["installed_reader"]["state"])
                self.assertIsNone(row["installed_reader"]["commits_behind"])
                hooks.unlink()
                if link.exists() or link.is_symlink():
                    link.unlink()

    def test_non_current_symlink_root_stays_unavailable(self):
        """A symlink not named current, even to a sibling digest, is not a reviewed alias."""

        _, link, _ = self._plant_waiter(link_name="alias")
        row = self.currency_module().installed_reader_finding(self.source, "HEAD", link, "waiter")
        self.assertEqual("installed_reader_unavailable", row["code"])
        self.assertEqual("unavailable", row["installed_reader"]["state"])
        self.assertIsNone(row["installed_reader"]["commits_behind"])
        self.assertEqual("alias", link.name)
