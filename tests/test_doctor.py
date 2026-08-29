from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import LivenessPresenceStore
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema

try:
    from floati.doctor import Doctor
except (ImportError, ModuleNotFoundError):
    Doctor = None


NOW = datetime(2026, 8, 1, 14, 0, 0, tzinfo=timezone.utc)


def root_entries(root: Path) -> dict[Path, tuple[str, bytes]]:
    return {
        path.relative_to(root): (
            "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
            b"" if path.is_symlink() or path.is_dir() else path.read_bytes(),
        )
        for path in root.rglob("*")
    }


class DoctorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        for relative, content in (
            ("floati/core.py", "VALUE = 1\n"),
            ("schemas/v0/example.json", '{"schema_version":0}\n'),
            ("scripts/floati", "#!/bin/sh\nexit 0\n"),
        ):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.source / "scripts/floati").chmod(0o755)
        self._write_manifest()
        self._git("init", "--quiet", "--initial-branch=lane/hm0")
        self._git("config", "user.name", "Floati Test")
        self._git("config", "user.email", "floati-test@example.invalid")
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "source")
        self._git("update-ref", "refs/remotes/origin/lane/hm0", "HEAD")

        self.home = self.base / "alpha"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        Registry(self.root).register("lane-a", "Codex")
        LivenessPresenceStore(self.root).observe("lane-a", 120, NOW)
        self.destination = self.base / "installed"
        destination_scripts = self.destination / "scripts"
        destination_scripts.mkdir(parents=True)
        (destination_scripts / "floati").write_bytes(b"installed\n")
        self.shadow = self.base / "shadow"
        self.shadow.mkdir()
        (self.shadow / "floati").write_bytes(b"shadow\n")
        git_directory = Path(shutil.which("git") or "").parent
        self.assertTrue((git_directory / "git").is_file())
        self._path_patch = patch.dict(
            os.environ,
            {
                "PATH": os.pathsep.join((
                    str(self.shadow),
                    str(self.source / "scripts"),
                    str(destination_scripts),
                    str(git_directory),
                )),
            },
            clear=False,
        )
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.source, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def _write_manifest(self) -> None:
        paths = [
            "floati/core.py",
            "schemas/v0/example.json",
            "scripts/floati",
        ]
        payload = {
            "schema_version": 0,
            "protocol_version": "0",
            "canonical_ref": "refs/heads/lane/hm0",
            "files": [
                {
                    "path": relative,
                    "sha256": hashlib.sha256((self.source / relative).read_bytes()).hexdigest(),
                }
                for relative in paths
            ],
        }
        (self.source / "bundle-manifest.v0.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def doctor(
        self,
        root: Path | None = None,
        gateway_config: Path | None = None,
        profile: str | None = None,
        codex_hooks: Path | None = None,
        codex_config: Path | None = None,
    ):
        self.assertIsNotNone(Doctor, "typed doctor implementation must exist")
        return Doctor(
            self.source,
            self.home if root is None else root,
            ref="origin/lane/hm0",
            gateway_config=gateway_config,
            destination=self.destination,
            profile=profile,
            codex_hooks=codex_hooks,
            codex_config=codex_config,
        )

    def _remove_presence(self) -> None:
        self.root.resolve_relative("liveness-presence/lane-a.jsonl").unlink()

    def _codes(self, artifact: dict) -> list[str]:
        return [row["code"] for row in artifact["findings"]]

    def test_absent_presence_still_degrades_when_no_profile_is_named(self) -> None:
        """No silent default change: absence without a declared profile still warns."""
        self._remove_presence()

        artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        self.assertEqual("degraded", artifact["state"])
        self.assertIn("registry_live_dirs_mismatch", self._codes(artifact))
        self.assertNotIn("registry_live_dirs_expected_absent", self._codes(artifact))

    def test_absent_presence_still_degrades_when_the_directory_never_existed(self) -> None:
        """Catches doctor inferring a bus-only fleet from a missing directory."""
        self._remove_presence()
        shutil.rmtree(self.root.resolve_relative("liveness-presence"))

        artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        self.assertIn("registry_live_dirs_mismatch", self._codes(artifact))

    def test_declared_bus_only_renders_expected_absence_as_an_ok_row(self) -> None:
        """The chartered fix: declared session-based fleets reach GREEN honestly."""
        self._remove_presence()

        artifact, rc = self.doctor(profile="bus-only").artifact()

        self.assertEqual(0, rc)
        self.assertEqual("healthy", artifact["state"])
        self.assertNotIn("registry_live_dirs_mismatch", self._codes(artifact))
        row = next(
            item for item in artifact["findings"]
            if item["code"] == "registry_live_dirs_expected_absent"
        )
        self.assertEqual("ok", row["severity"])
        self.assertIsNone(row["remediation"])

    def test_doctor_accepts_a_node_lease_written_to_the_registry_ledger(self) -> None:
        """Catches doctor declaring a narrower vocabulary than the registry writer."""
        append_record(
            self.root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "lease-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-08-01T14:00:00.000Z",
                "kind": "node_lease",
                "node_id": "lane-a",
                "workspace": str(self.root.path / "nodes" / "lane-a"),
                "expires_at": "2026-08-01T14:01:00.000Z",
                "state": "active",
            },
            allowed_kinds=set(REGISTRY_KINDS),
        )

        artifact, rc = self.doctor().artifact()

        self.assertEqual(0, rc)
        self.assertEqual("healthy", artifact["state"])
        self.assertNotIn("record_kind_invalid", self._codes(artifact))

    def test_declared_bus_only_keeps_warning_when_presence_is_only_partly_absent(self) -> None:
        """Honest-absence law: absence is stated, never guessed from a partial set."""
        Registry(self.root).register("lane-b", "Codex")

        artifact, rc = self.doctor(profile="bus-only").artifact()

        self.assertEqual(35, rc)
        self.assertIn("registry_live_dirs_mismatch", self._codes(artifact))
        self.assertNotIn("registry_live_dirs_expected_absent", self._codes(artifact))

    def test_declared_bus_only_does_not_mask_a_genuine_presence_match(self) -> None:
        """A bus-only declaration must not rewrite testimony that already agrees."""
        artifact, rc = self.doctor(profile="bus-only").artifact()

        self.assertEqual(0, rc)
        self.assertIn("registry_live_dirs_match", self._codes(artifact))
        self.assertNotIn("registry_live_dirs_expected_absent", self._codes(artifact))

    def test_declared_orchestration_profile_keeps_the_warning(self) -> None:
        """Orchestration-mode fleets keep the existing degraded testimony."""
        self._remove_presence()

        artifact, rc = self.doctor(profile="orchestration").artifact()

        self.assertEqual(35, rc)
        self.assertIn("registry_live_dirs_mismatch", self._codes(artifact))

    def test_an_unruled_profile_refuses_before_any_artifact_is_produced(self) -> None:
        """Catches a typo silently selecting a lenient aggregate."""
        for invalid in ("bus only", "BUS-ONLY", "session", ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.doctor(profile=invalid)
                self.assertEqual("doctor_profile_invalid", caught.exception.code)

    def test_explicit_codex_hook_trust_is_reported_per_hook(self) -> None:
        from floati.codex_hook_trust import codex_hook_current_hash

        hooks_path = self.base / ".codex" / "hooks.json"
        hooks_path.parent.mkdir()
        block = {
            "hooks": [{
                "type": "command",
                "command": "/usr/bin/python3 /opt/floati-codex-wait --root /tmp/fleet",
                "timeout": 1800,
                "statusMessage": "Watching Floati bus",
            }]
        }
        hooks_path.write_text(
            json.dumps({"hooks": {"Stop": [block]}}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = hooks_path.with_name("config.toml")
        key = f"{hooks_path}:stop:0:0"
        current_hash = codex_hook_current_hash(block)
        self.assertEqual(
            "sha256:ac3a5c59887f923baa872af6eb031650bfcfd4b4d27461c746c2af530f7645ca",
            current_hash,
        )
        config_path.write_text(
            "[hooks.state." + json.dumps(key) + "]\n"
            "trusted_hash = " + json.dumps(current_hash) + "\n",
            encoding="utf-8",
        )

        artifact, rc = self.doctor(
            codex_hooks=hooks_path,
            codex_config=config_path,
        ).artifact()

        self.assertEqual(0, rc)
        row = next(
            item for item in artifact["findings"]
            if item["code"] == "codex_wait_hook_trust"
        )
        self.assertEqual("trusted", row["hook_trust"])
        self.assertEqual(key, row["hook_trust_key"])

        config_path.write_text(
            "[hooks.state." + json.dumps(key) + "]\n"
            "trusted_hash = \"sha256:" + "0" * 64 + "\"\n",
            encoding="utf-8",
        )
        before = config_path.read_bytes()
        degraded, degraded_rc = self.doctor(
            codex_hooks=hooks_path,
            codex_config=config_path,
        ).artifact()
        degraded_row = next(
            item for item in degraded["findings"]
            if item["code"] == "codex_wait_hook_trust"
        )
        self.assertEqual(35, degraded_rc)
        self.assertEqual("untrusted_pending_user", degraded_row["hook_trust"])
        self.assertFalse(degraded_row["hook_armed"])
        self.assertTrue(degraded_row["remediation"])
        self.assertNotIn("DRAFT - ", degraded_row["remediation"])
        self.assertEqual(before, config_path.read_bytes())

    def test_cli_requires_the_explicit_profile_flag_and_refuses_an_unruled_value(self) -> None:
        self._remove_presence()
        base = [
            "python3", "-m", "floati", "doctor",
            "--root", str(self.home),
            "--source", str(self.source),
            "--ref", "origin/lane/hm0",
            "--destination", str(self.destination),
        ]

        degraded = subprocess.run(base, capture_output=True, text=True, check=False)
        self.assertEqual(35, degraded.returncode)

        healthy = subprocess.run(
            [*base, "--profile", "bus-only"], capture_output=True, text=True, check=False
        )
        self.assertEqual(0, healthy.returncode, healthy.stderr)
        evidence = json.loads(healthy.stdout)["evidence"]
        self.assertEqual("healthy", evidence["state"])
        self.assertIn(
            "registry_live_dirs_expected_absent",
            [row["code"] for row in evidence["findings"]],
        )
        validate_json_schema(evidence, Path("schemas/v1/doctor-artifact.schema.json"))

        refused = subprocess.run(
            [*base, "--profile", "nonsense"], capture_output=True, text=True, check=False
        )
        self.assertEqual(20, refused.returncode)
        self.assertEqual(
            "doctor_profile_invalid",
            json.loads(refused.stderr)["evidence"]["code"],
        )

    def test_registered_set_folds_latest_row_wins_and_excludes_retired_nodes(self) -> None:
        """Catches doctor counting a retired node because its old active row remains."""
        registry = Registry(self.root)
        registry.register("lane-b", "Codex")
        registry.retire("lane-b")

        artifact, rc = self.doctor().artifact()

        self.assertEqual(("lane-a",), registry.active_node_ids())
        self.assertEqual(0, rc, "a retired node with no presence file is not a mismatch")
        self.assertIn("registry_live_dirs_match", self._codes(artifact))

    def test_wake_namespace_must_be_a_physically_read_only_subset_of_registry_lineage(self) -> None:
        """Catches an unregistered spelling minting a healthy-looking wake coordinator."""
        canonical = self.root.resolve_relative(
            "receipts/wake-coordination/lane-a/lane.lock"
        )
        alias = self.root.resolve_relative(
            "receipts/wake-coordination/lane_alias/lane.lock"
        )
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(b"")
        alias.parent.mkdir(parents=True)
        alias.write_bytes(b"")
        before = root_entries(self.home)

        artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        self.assertEqual(before, root_entries(self.home))
        finding = next(
            row for row in artifact["findings"]
            if row["code"] == "wake_namespace_registry_mismatch"
        )
        self.assertEqual("warning", finding["severity"])
        self.assertIn("lane_alias", finding["detail"])
        self.assertNotIn("lane-a]", finding["detail"])

    def test_bus_only_visible_copy_is_the_architects_strings(self) -> None:
        """Catches the architect's strings regressing to placeholder keys."""
        from floati import copy as copy_module

        for key in ("doctor.profile.invalid", "doctor.live_dirs.expected_absent"):
            with self.subTest(key=key):
                self.assertNotIn("[[", copy_module._ENTRIES[key][0])

    def _gateway_config(self) -> Path:
        path = self.base / "gateway.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "kind": "local_gateway_config",
                    "transport": "stdio",
                    "network": "disabled",
                    "workspace_root": str(self.base / "gateway-work"),
                    "approval_mode": "forward_fail_closed",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_healthy_artifact_reports_every_named_diagnostic_family(self) -> None:
        artifact, rc = self.doctor().artifact()

        self.assertEqual(0, rc)
        self.assertEqual("healthy", artifact["state"])
        self.assertEqual(
            [
                "root_valid",
                "registry_live_dirs_match",
                "wake_namespace_registry_subset",
                "delivery_health",
                "manifest_exact_set",
                "deploy_currency_current",
                "symlink_identity_valid",
                "consumption_coordinate_valid",
                "installer_shadow",
            ],
            [finding["code"] for finding in artifact["findings"]],
        )
        self.assertTrue(all(
            finding["severity"] == "ok"
            for finding in artifact["findings"]
            if finding["code"] != "installer_shadow"
        ))
        self.assertTrue(all(finding["remediation"] is None for finding in artifact["findings"]))
        shadow = next(row for row in artifact["findings"] if row["code"] == "installer_shadow")
        self.assertEqual("warning", shadow["severity"])
        self.assertEqual("found", shadow["installer_shadow"]["outcome"])

    def test_root_registry_and_symlink_failures_have_typed_return_codes(self) -> None:
        missing, missing_rc = self.doctor(self.base / "missing").artifact()
        self.assertEqual(20, missing_rc)
        self.assertIn("direct_home_missing", [row["code"] for row in missing["findings"]])

        (self.root.resolve_relative("liveness-presence/lane-a.jsonl")).unlink()
        mismatch, mismatch_rc = self.doctor().artifact()
        self.assertEqual(35, mismatch_rc)
        finding = next(row for row in mismatch["findings"] if row["code"] == "registry_live_dirs_mismatch")
        self.assertIsNotNone(finding["remediation"])

        alias = self.base / "alias"
        os.symlink(self.home, alias)
        symlinked, symlinked_rc = self.doctor(alias).artifact()
        self.assertEqual(20, symlinked_rc)
        finding = next(row for row in symlinked["findings"] if row["code"] == "direct_home_symlinked_entry")
        self.assertIsNone(finding["remediation"])

    def test_manifest_currency_and_consumption_corruption_are_not_repair_claims(self) -> None:
        (self.source / "floati/core.py").write_text("DIRTY = 1\n", encoding="utf-8")
        invalid, invalid_rc = self.doctor().artifact()
        self.assertEqual(33, invalid_rc)
        manifest = next(row for row in invalid["findings"] if row["code"] == "manifest_invalid")
        self.assertIsNone(manifest["remediation"])
        self.assertIn("deploy_currency_unavailable", [row["code"] for row in invalid["findings"]])

        self._git("checkout", "--", "floati/core.py")
        work = self.root.resolve_relative("work/items.jsonl")
        work.parent.mkdir(parents=True, exist_ok=True)
        work.write_text('{"kind":"work_item"}\n', encoding="utf-8")
        corrupted, corrupted_rc = self.doctor().artifact()
        self.assertEqual(33, corrupted_rc)
        self.assertIn("consumption_state_unavailable", [row["code"] for row in corrupted["findings"]])

    def test_alternate_work_coordinate_is_degraded_not_consumed(self) -> None:
        alternate = self.root.resolve_relative("work/queue.jsonl")
        alternate.parent.mkdir(parents=True, exist_ok=True)
        alternate.write_text("", encoding="utf-8")

        artifact, rc = self.doctor().artifact()

        self.assertEqual(35, rc)
        finding = next(row for row in artifact["findings"] if row["code"] == "consumption_coordinate_ambiguous")
        self.assertIn("work/items.jsonl", finding["detail"])

    def test_schema_fixture_and_cli_artifact_return_code(self) -> None:
        fixture = json.loads(Path("tests/fixtures/doctor/v0/healthy.json").read_text(encoding="utf-8"))
        validate_json_schema(fixture, Path("schemas/v0/doctor-artifact.schema.json"))

        result = subprocess.run(
            [
                "scripts/floati", "doctor", "--root", str(self.home),
                "--source", str(self.source), "--ref", "origin/lane/hm0",
                "--destination", str(self.destination),
            ],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertEqual("doctor", envelope["command"])
        self.assertEqual("healthy", envelope["evidence"]["state"])
        validate_json_schema(envelope["evidence"], Path("schemas/v1/doctor-artifact.schema.json"))

    def test_explicit_gateway_config_is_checked_without_discovery_or_activation(self) -> None:
        config = self._gateway_config()

        artifact, rc = self.doctor(gateway_config=config).artifact()

        self.assertEqual(0, rc)
        finding = next(row for row in artifact["findings"] if row["code"] == "gateway_config_valid")
        self.assertEqual("ok", finding["severity"])
        self.assertEqual(str(config), finding["subject"])
        self.assertIsNone(finding["remediation"])
        self.assertFalse((self.base / "gateway-work").exists())

        without_config, without_rc = self.doctor().artifact()
        self.assertEqual(0, without_rc)
        self.assertFalse(any(row["code"].startswith("gateway_") for row in without_config["findings"]))

    def test_invalid_gateway_config_is_typed_and_currency_conditional(self) -> None:
        missing = self.base / "missing-gateway.json"
        artifact, rc = self.doctor(gateway_config=missing).artifact()
        self.assertEqual(20, rc)
        finding = next(row for row in artifact["findings"] if row["code"] == "gateway_config_missing")
        self.assertIsNotNone(finding["remediation"])

        malformed = self._gateway_config()
        malformed.write_text('{"transport":"https"}\n', encoding="utf-8")
        artifact, rc = self.doctor(gateway_config=malformed).artifact()
        self.assertEqual(20, rc)
        self.assertIn("gateway_config_malformed", [row["code"] for row in artifact["findings"]])

        self._git("checkout", "--", "floati/core.py")
        (self.source / "floati/core.py").write_text("DIRTY = 1\n", encoding="utf-8")
        artifact, _ = self.doctor(gateway_config=missing).artifact()
        finding = next(row for row in artifact["findings"] if row["code"] == "gateway_config_missing")
        self.assertIsNone(finding["remediation"])

    def test_doctor_cli_accepts_only_an_explicit_gateway_config_path(self) -> None:
        config = self._gateway_config()
        result = subprocess.run(
            [
                "scripts/floati", "doctor", "--root", str(self.home),
                "--source", str(self.source), "--ref", "origin/lane/hm0",
                "--gateway-config", str(config),
                "--destination", str(self.destination),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        evidence = json.loads(result.stdout)["evidence"]
        self.assertIn("gateway_config_valid", [row["code"] for row in evidence["findings"]])


if __name__ == "__main__":
    unittest.main()
