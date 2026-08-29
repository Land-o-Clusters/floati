from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.node_wizard import NodeAddPlan, NodeRetirePlan
from floati.provider_switch import ProviderSwitchWizard
from floati.registry import Registry
from floati.root import FloatiRoot


NOW = "2026-08-28T01:00:00.000Z"


class RegistryAdminBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open(Path(self.temporary.name), "alpha")

        from floati.admin_registry import RegistryAdminBackend

        self.backend = RegistryAdminBackend(self.root)

    def registry_record(self, node: str, role: str, state: str = "active") -> dict:
        return {
            "schema_version": 0,
            "id": "registry-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "registry_entry",
            "node_id": node,
            "role": role,
            "state": state,
        }

    def lease_record(self, node: str) -> dict:
        return {
            "schema_version": 0,
            "id": "lease-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "node_lease",
            "node_id": node,
            "workspace": str(self.root.path / "nodes" / node),
            "expires_at": "2026-08-28T02:00:00.000Z",
            "state": "active",
        }

    def test_add_commits_the_exact_previewed_registry_and_lease_rows(self) -> None:
        """Catches an adapter regenerating ids/timestamps or omitting the previewed lease."""
        registry = self.registry_record("lane-a", "Codex")
        lease = self.lease_record("lane-a")
        plan = NodeAddPlan(
            node_id="lane-a",
            harness="Codex",
            lifetime="temporary",
            lease_minutes=60,
            workspace=str(self.root.path / "nodes" / "lane-a"),
            records=(registry, lease),
            boot_command="floati node boot",
            teardown_command="floati node teardown",
        )

        result = self.backend.commit_add(plan)

        self.assertEqual((registry, lease), tuple(result["records"]))
        self.assertEqual(registry, self.backend.active_node("lane-a"))
        self.assertEqual(lease, self.backend.active_lease("lane-a"))

    def test_invalid_second_preview_row_appends_nothing(self) -> None:
        """Catches a partial registry mutation when a later preview row is malformed."""
        registry = self.registry_record("lane-a", "Codex")
        invalid_lease = dict(self.lease_record("lane-a"))
        invalid_lease.pop("expires_at")
        plan = NodeAddPlan(
            node_id="lane-a",
            harness="Codex",
            lifetime="temporary",
            lease_minutes=60,
            workspace=str(self.root.path / "nodes" / "lane-a"),
            records=(registry, invalid_lease),
            boot_command="floati node boot",
            teardown_command="floati node teardown",
        )

        with self.assertRaises(ProtocolRefusal):
            self.backend.commit_add(plan)

        self.assertEqual((), Registry(self.root).active_node_ids())
        self.assertFalse(Registry(self.root).path.exists())

    def test_retire_closes_the_exact_active_lease_and_retains_workspace(self) -> None:
        """Catches retirement deleting a workspace or closing another lease."""
        workspace = self.root.path / "nodes" / "lane-a"
        workspace.mkdir(parents=True)
        registry = self.registry_record("lane-a", "Codex")
        lease = self.lease_record("lane-a")
        self.backend.commit_add(
            NodeAddPlan(
                "lane-a", "Codex", "temporary", 60, str(workspace),
                (registry, lease), "floati node boot", "floati node teardown",
            )
        )
        retired_registry = self.registry_record("lane-a", "Codex", "retired")
        retired_lease = {
            "schema_version": 0,
            "id": "lease-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "node_lease",
            "node_id": "lane-a",
            "predecessor_lease_id": lease["id"],
            "workspace": str(workspace),
            "state": "retired",
        }

        result = self.backend.commit_retire(
            NodeRetirePlan("lane-a", str(workspace), (retired_registry, retired_lease))
        )

        self.assertEqual((retired_registry, retired_lease), tuple(result["records"]))
        self.assertIsNone(self.backend.active_lease("lane-a"))
        self.assertTrue(workspace.is_dir())

    def test_provider_switch_folds_the_receipted_model_into_active_assignment(self) -> None:
        """Catches a switch receipt landing without changing the projected model."""
        first = self.registry_record("grok", "opencode-grok")
        self.backend.commit_add(
            NodeAddPlan(
                "grok", "opencode-grok", "permanent", None,
                str(self.root.path / "nodes" / "grok"), (first,), None, None,
            )
        )
        preview = io.StringIO()
        wizard = ProviderSwitchWizard(
            self.root,
            self.backend,
            id_factory=uuid7_hex,
            now=lambda: datetime(2026, 8, 28, 1, 0, tzinfo=timezone.utc),
        )

        result = wizard.switch_from_keys(["grok", "Cursor", "gpt-5.6"], preview)

        active = self.backend.active_assignment("grok")
        self.assertEqual("Cursor", active["role"])
        self.assertEqual("gpt-5.6", active["model"])
        self.assertEqual(result["records"][0]["id"], active["id"])
        self.assertIn(result["records"][1]["id"], preview.getvalue())


if __name__ == "__main__":
    unittest.main()
