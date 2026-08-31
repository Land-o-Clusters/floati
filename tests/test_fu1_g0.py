from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from floati.update_consent import UpdateConsentLedger

try:
    from floati.fleet_update import (
        apply_fleet_update,
        preview_fleet_update,
        rewrite_transport_pins,
    )
except (ImportError, ModuleNotFoundError):
    apply_fleet_update = None
    preview_fleet_update = None
    rewrite_transport_pins = None


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CURRENT_SOURCE_SHA = "a" * 40
TARGET_SOURCE_SHA = "b" * 40


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class FU1G0Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="floati-fu1-g0-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(
            self.base / "fixture-fleet", create=True
        )
        self.actor = "builder-one"
        self.channel = "https://updates.example.invalid/release-index.v0"
        self.version = "2.0.0"

        self.destination = self.base / "installed"
        self._write_install(
            self.destination,
            source_sha=CURRENT_SOURCE_SHA,
            encoder=b"current encoder\n",
        )
        self.target_source = self.base / "target-source"
        self._write_source_bundle(
            self.target_source,
            source_sha=TARGET_SOURCE_SHA,
            encoder=b"target encoder\n",
        )

        self.waiter_store = self.base / "waiters"
        self.named_waiter_digest = "f" * 64
        self.current_waiter = self.waiter_store / self.named_waiter_digest
        launcher = self.current_waiter / "scripts" / "floati-codex-wait"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.current_waiter / "floati").mkdir()
        (self.current_waiter / "floati" / "codex_wait.py").write_bytes(
            b"CURRENT = True\n"
        )
        for relative in ("LICENSE", "floati/events.py", "floati/records.py"):
            target = self.current_waiter / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.target_source / relative).read_bytes())
        from floati.waiter_bundle import waiter_runtime_digest
        self.observed_waiter_digest = waiter_runtime_digest(self.current_waiter)
        self.assertNotEqual(self.named_waiter_digest, self.observed_waiter_digest)

        self.hooks_path = self.base / "hooks.json"
        self.hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            f"/usr/bin/python3 {launcher} --root "
                                            f"{self.root.path}"
                                        ),
                                        "timeout": 900,
                                        "statusMessage": "Watching Floati bus",
                                    }
                                ]
                            }
                        ],
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "true"}]}
                        ],
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.binding_path = self.base / "BINDING.json"
        self.binding_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "bindings": [
                        {
                            "kind": "codex_stop_hook",
                            "configuration": str(self.hooks_path),
                            "store": str(self.waiter_store),
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        current_metadata = (
            self.destination / ".floati-install" / "manifest.v0.json"
        )
        self.current_manifest_digest = _sha256(current_metadata.read_bytes())
        self.registry_path = self.base / "fleet-bus-profiles.json"
        self.registry_path.write_bytes(
            (
                "{\n"
                '  "schema_version": 0,\n'
                '  "transports": {\n'
                '    "floati-installed-v0": {\n'
                f'      "install_root": {json.dumps(str(self.destination))},\n'
                '      "interpreter": "/usr/bin/python3",\n'
                '      "kind": "floati-installed-v0",\n'
                f'      "manifest_path": {json.dumps(str(current_metadata))},\n'
                f'      "manifest_sha256": "{self.current_manifest_digest}",\n'
                '      "max_file_bytes": 1048576,\n'
                '      "max_files": 2048,\n'
                '      "max_total_bytes": 67108864,\n'
                f'      "source_sha": "{CURRENT_SOURCE_SHA}"\n'
                "    }\n"
                "  },\n"
                '  "profiles": {\n'
                '    "fleet-floati-builder-one": {\n'
                '      "identity": "builder-one",\n'
                '      "transport": "floati-installed-v0"\n'
                "    }\n"
                "  },\n"
                '  "unrelated": {"spacing": "must stay byte-identical"}\n'
                "}\n"
            ).encode("utf-8")
        )

    @staticmethod
    def _write_manifest(root: Path, source_sha: str) -> list[dict[str, str]]:
        paths = (
            "floati/events.py",
            "floati/records.py",
            "scripts/floati",
            "scripts/floati-codex-wait",
        )
        files = [
            {
                "path": relative,
                "sha256": _sha256((root / relative).read_bytes()),
            }
            for relative in paths
        ]
        (root / "bundle-manifest.v0.json").write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "protocol_version": "0",
                    "canonical_ref": "refs/heads/lane/hm0",
                    "files": files,
                    "source_sha": source_sha,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return files

    def _write_source_bundle(
        self, root: Path, *, source_sha: str, encoder: bytes
    ) -> None:
        (root / "floati").mkdir(parents=True)
        (root / "LICENSE").write_bytes(b"fixture license\n")
        (root / "scripts").mkdir()
        (root / "floati" / "events.py").write_bytes(b"EVENTS = 1\n")
        (root / "floati" / "records.py").write_bytes(encoder)
        (root / "scripts" / "floati").write_bytes(b"#!/bin/sh\nexit 0\n")
        (root / "scripts" / "floati-codex-wait").write_bytes(
            b"#!/bin/sh\nexit 0\n"
        )
        self._write_manifest(root, source_sha)

    def _write_install(
        self, destination: Path, *, source_sha: str, encoder: bytes
    ) -> None:
        self._write_source_bundle(destination, source_sha=source_sha, encoder=encoder)
        bundle = json.loads(
            (destination / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        metadata = destination / ".floati-install"
        metadata.mkdir()
        (metadata / "manifest.v0.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_ref": "refs/heads/main",
                    "source_sha": source_sha,
                    "files": bundle["files"],
                    "ownership": {
                        "kind": "floati_standalone",
                        "destination": str(destination),
                        "entrypoint": "scripts/floati",
                        "entrypoint_sha256": _sha256(
                            (destination / "scripts" / "floati").read_bytes()
                        ),
                        "manager": None,
                        "remedy": None,
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _require_preview(self):
        self.assertIsNotNone(
            preview_fleet_update,
            "G0 must expose a read-only fleet update planner",
        )
        return preview_fleet_update

    def _require_apply(self):
        self.assertIsNotNone(
            apply_fleet_update,
            "G0 must expose a consent-gated fleet update apply surface",
        )
        return apply_fleet_update

    def _preview(self) -> dict[str, object]:
        planner = self._require_preview()
        return planner(
            root=self.root,
            actor=self.actor,
            destination=self.destination,
            target_source=self.target_source,
            target_source_sha=TARGET_SOURCE_SHA,
            channel=self.channel,
            version=self.version,
            binding_path=self.binding_path,
            transport_registry=self.registry_path,
            transport_name="floati-installed-v0",
        )

    def _apply(self, plan_digest: str, idempotency_key: str) -> dict[str, object]:
        operation = self._require_apply()
        return operation(
            root=self.root,
            actor=self.actor,
            destination=self.destination,
            target_source=self.target_source,
            target_source_sha=TARGET_SOURCE_SHA,
            channel=self.channel,
            version=self.version,
            binding_path=self.binding_path,
            transport_registry=self.registry_path,
            transport_name="floati-installed-v0",
            plan_digest=plan_digest,
            idempotency_key=idempotency_key,
        )

    def test_g0_00_help_names_explicit_preview_and_apply_only(self) -> None:
        """Catches FU-1 growing ambient discovery or an automatic mutation mode."""

        for operation in ("preview", "apply"):
            with self.subTest(operation=operation):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "floati",
                        "update",
                        "fleet",
                        operation,
                        "--help",
                    ],
                    cwd=REPOSITORY_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                for required in (
                    "--root",
                    "--as",
                    "--destination",
                    "--channel",
                    "--version",
                    "--waiter-binding",
                    "--transport-registry",
                    "--transport",
                ):
                    self.assertIn(required, result.stdout)
                self.assertNotIn("--auto", result.stdout)
                self.assertNotIn("--discover", result.stdout)
                if operation == "apply":
                    self.assertIn("--plan-digest", result.stdout)
                    self.assertIn("--idempotency-key", result.stdout)

    def test_g0_01_preview_is_read_only_and_names_moves_and_non_moves(self) -> None:
        """Catches preview taking a lock/write or omitting targets that stay put."""

        before = _tree_bytes(self.base)

        first = self._preview()
        second = self._preview()

        self.assertEqual(before, _tree_bytes(self.base))
        self.assertEqual(first, second)
        self.assertEqual(
            [
                {
                    "kind": "shared_install",
                    "path": str(self.destination),
                    "from": self.current_manifest_digest,
                    "to": first["target_manifest_sha256"],
                },
                {
                    "kind": "transport_pins",
                    "path": str(self.registry_path),
                    "from": first["current_transport_pins"],
                    "to": first["target_transport_pins"],
                },
                {
                    "kind": "waiter_binding",
                    "path": str(self.hooks_path),
                    "store": str(self.waiter_store),
                    "configuration_from_sha256": first["waiter_bindings"][0]["configuration_sha256"],
                    "configuration_to_sha256": first["waiter_bindings"][0]["target_configuration_sha256"],
                    "current_tree_digest": first["waiter_bindings"][0]["current_tree_digest"],
                    "target_tree_digest": first["target_waiter_digest"],
                },
            ],
            first["moves"],
        )
        self.assertEqual(
            [
                {
                    "kind": "waiter_generation",
                    "path": str(self.current_waiter),
                    "named_tree_digest": self.named_waiter_digest,
                    "current_tree_digest": self.observed_waiter_digest,
                    "retained": True,
                }
            ],
            first["unchanged"],
        )

    def test_g0_plan_noop_places_all_targets_in_unchanged(self) -> None:
        """Catches summaries that claim an action when all named facts agree."""

        from floati.codex_hook_install import plan_waiter_rebind, stage_waiter_runtime

        target_digest = str(self._preview()["target_waiter_digest"])
        stage_waiter_runtime(self.target_source, self.waiter_store, target_digest)
        target_tree = self.waiter_store / target_digest
        staged = plan_waiter_rebind(
            self.hooks_path, self.waiter_store, target_digest
        )
        self.hooks_path.write_bytes(staged["after"])
        metadata_path = self.destination / ".floati-install" / "manifest.v0.json"
        target_manifest = json.loads(
            (self.target_source / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        for row in target_manifest["files"]:
            relative = str(row["path"])
            (self.destination / relative).write_bytes(
                (self.target_source / relative).read_bytes()
            )
        from floati.fleet_update import _planned_install_metadata
        current_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata_path.write_bytes(
            _planned_install_metadata(
                current_metadata, target_manifest, self.target_source, self.destination
            )
        )
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        registry["transports"]["floati-installed-v0"].update(
            {
                "manifest_sha256": _sha256(metadata_path.read_bytes()),
                "source_sha": TARGET_SOURCE_SHA,
            }
        )
        self.registry_path.write_text(
            json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        plan = self._preview()

        self.assertEqual([], plan["moves"])
        self.assertEqual(
            [
                {
                    "kind": "shared_install",
                    "path": str(self.destination),
                    "from": plan["current_manifest_sha256"],
                    "to": plan["target_manifest_sha256"],
                },
                {
                    "kind": "transport_pins",
                    "path": str(self.registry_path),
                    "from": plan["current_transport_pins"],
                    "to": plan["target_transport_pins"],
                },
                {
                    "kind": "waiter_binding",
                    "path": str(self.hooks_path),
                    "store": str(self.waiter_store),
                    "configuration_from_sha256": plan["waiter_bindings"][0]["configuration_sha256"],
                    "configuration_to_sha256": plan["waiter_bindings"][0]["target_configuration_sha256"],
                    "current_tree_digest": plan["waiter_bindings"][0]["current_tree_digest"],
                    "target_tree_digest": plan["target_waiter_digest"],
                },
                {
                    "kind": "waiter_generation",
                    "path": str(target_tree),
                    "named_tree_digest": target_digest,
                    "current_tree_digest": target_digest,
                    "retained": True,
                },
            ],
            plan["unchanged"],
        )

    def test_g0_02_inventory_current_digests_are_rederived_from_named_files(self) -> None:
        """Catches the directory name or inventory testimony becoming digest authority."""

        plan = self._preview()

        waiter = plan["waiter_bindings"][0]
        self.assertEqual(self.named_waiter_digest, waiter["named_tree_digest"])
        self.assertEqual(self.observed_waiter_digest, waiter["current_tree_digest"])
        self.assertNotEqual(
            waiter["named_tree_digest"], waiter["current_tree_digest"]
        )

    def test_g0_03_observed_current_digest_drift_invalidates_the_plan(self) -> None:
        """Catches apply treating re-observation as a courtesy rather than plan identity."""

        planned = self._preview()
        trust = self.destination / "trust"
        trust.mkdir(exist_ok=True)
        (trust / "release.pub").write_text("public test key\n", encoding="utf-8")
        (trust / "keys.json").write_text(
            json.dumps({"format": "floati-trust-keys.v1", "keys": [{"key_id": "release", "public_key_file": "release.pub", "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2030-01-01T00:00:00Z", "status": "active", "transition": "initial"}]}, sort_keys=True),
            encoding="utf-8",
        )
        UpdateConsentLedger(self.destination).consent(
            channel=self.channel, epoch=1, idempotency_key="fu1-g0-plan-drift"
        )
        before_apply = _tree_bytes(self.base)
        hooks = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 901
        self.hooks_path.write_text(
            json.dumps(hooks, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        after_drift = _tree_bytes(self.base)

        with self.assertRaises(ProtocolRefusal) as caught:
            self._apply(str(planned["plan_digest"]), "fu1-plan-drift")

        self.assertEqual("fleet_update_plan_drift", caught.exception.code)
        self.assertEqual(after_drift, _tree_bytes(self.base))
        self.assertNotEqual(before_apply, after_drift)

    def test_g0_04_both_stale_transport_pins_refuse_together_with_apply_shape(self) -> None:
        """Catches sequential pin validation revealing one stale field per retry."""

        plan = self._preview()

        self.assertEqual(
            ["manifest_sha256", "source_sha"],
            [row["field"] for row in plan["stale_pins"]],
        )
        for row in plan["stale_pins"]:
            self.assertEqual(str(self.registry_path), row["registry"])
            self.assertEqual("floati-installed-v0", row["transport"])
            self.assertRegex(row["pinned"], r"^[0-9a-f]{40}([0-9a-f]{24})?$\Z")
            self.assertRegex(row["observed"], r"^[0-9a-f]{40}([0-9a-f]{24})?$\Z")
        self.assertEqual(
            ["update", "fleet", "apply"], plan["apply_argv"][:3]
        )
        self.assertIn("--plan-digest", plan["apply_argv"])
        self.assertEqual(64, len(plan["plan_digest"]))

    def test_g0_05_requires_epoch_roll_is_derived_from_encoder_bytes(self) -> None:
        """Catches caller testimony or source-SHA inequality deciding epoch safety."""

        changed = self._preview()
        (self.target_source / "floati" / "records.py").write_bytes(
            (self.destination / "floati" / "records.py").read_bytes()
        )
        self._write_manifest(self.target_source, TARGET_SOURCE_SHA)
        stable = self._preview()

        self.assertTrue(changed["requires_epoch_roll"])
        self.assertFalse(stable["requires_epoch_roll"])
        self.assertEqual(TARGET_SOURCE_SHA, stable["target_source_sha"])
        self.assertNotIn("requires_epoch_roll", stable["inputs"])

    def test_g0_06_missing_consent_refuses_before_any_mutation_or_network(self) -> None:
        """Catches a valid plan digest reaching transport or writes without AU-1 consent."""

        plan = self._preview()
        before = _tree_bytes(self.base)

        with (
            mock.patch.object(
                socket,
                "socket",
                side_effect=AssertionError(
                    "missing consent must refuse before socket construction"
                ),
            ),
            self.assertRaises(ProtocolRefusal) as caught,
        ):
            self._apply(str(plan["plan_digest"]), "fu1-consent-missing")

        self.assertEqual("update_consent_missing", caught.exception.code)
        self.assertEqual(before, _tree_bytes(self.base))

    def test_g0_07_registry_rewrite_changes_only_two_selected_string_literals(self) -> None:
        """Catches JSON reserialization changing unrelated registry bytes."""

        self.assertIsNotNone(
            rewrite_transport_pins,
            "G0 GREEN must expose the surgical two-pin registry writer",
        )
        before = self.registry_path.read_bytes()
        target_manifest_digest = "c" * 64
        expected = before.replace(
            f'"{self.current_manifest_digest}"'.encode("ascii"),
            f'"{target_manifest_digest}"'.encode("ascii"),
            1,
        ).replace(
            f'"{CURRENT_SOURCE_SHA}"'.encode("ascii"),
            f'"{TARGET_SOURCE_SHA}"'.encode("ascii"),
            1,
        )

        result = rewrite_transport_pins(
            self.registry_path,
            "floati-installed-v0",
            manifest_sha256=target_manifest_digest,
            source_sha=TARGET_SOURCE_SHA,
        )

        self.assertEqual(expected, self.registry_path.read_bytes())
        self.assertEqual(_sha256(before), result["registry_before_sha256"])
        self.assertEqual(_sha256(expected), result["registry_after_sha256"])
        self.assertEqual(
            ["manifest_sha256", "source_sha"], result["changed_pins"]
        )


if __name__ == "__main__":
    unittest.main()
