"""A fleet list uses one live registry cut, without a lifetime cache."""
from __future__ import annotations

import io
import json
import shutil
from pathlib import Path
import tempfile
import unittest

from floati.admin_cli import _LiveNodeProjectionSource
from floati.admin_registry import RegistryAdminBackend
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.role_assignment import RoleStepWizard
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from tests.bus_scale_io import IOProbe
from tests.temp_roots import REAL_TEMP_ROOT


class NodeProjectionCutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        self.registry = Registry(self.root)
        self.backend = RegistryAdminBackend(self.root)
        self.templates = load_shipped_role_templates(Path("roles/shipped"))
        self.source = _LiveNodeProjectionSource(self.root,
            str(Path(temporary.name) / "declared.json"), "/unused/managed", "unused")
        for node in ("gamma", "alpha", "beta"):
            self.add_node(node)

    def add_node(self, node):
        self.registry.register(node, "Codex")
        RoleStepWizard(self.root, self.backend, self.templates,
            id_factory=uuid7_hex).assign_from_keys(
                [node, "builder", "floati", "foreign-project", "coordinator"], io.StringIO())

    def test_fleet_list_read_budget_does_not_multiply_by_member_count(self):
        ledger_bytes = self.registry.path.stat().st_size
        with IOProbe(self.root.tenant_home) as probe:
            actual = self.source.active_nodes()
        probe.assert_complete()
        self.assertEqual(["alpha", "beta", "gamma"], [row["node_id"] for row in actual])
        self.assertLessEqual(probe.read_bytes_by_path.get("registry/entries.jsonl", 0),
            2 * ledger_bytes, "one fleet list repeatedly reads the complete registry for each member")

    def test_certified_projection_reads_preserve_complete_current_fleet_without_history_replay(self):
        from floati.registry_prefix import warm
        from floati.role_assignment import RoleAssignmentPlan

        self.source.active_nodes()
        for repository in ("superseded-repository", "current-repository"):
            predecessor = self.backend.role_record("alpha")
            record = dict(predecessor, id="registry-role-" + uuid7_hex(),
                answers=dict(predecessor["answers"], repo=repository),
                predecessor_role_record_id=predecessor["id"])
            self.backend.commit_role(RoleAssignmentPlan(node_id="alpha",
                template_role="builder", record=record))
        self.add_node("retired")
        self.registry.retire("retired")
        expected_fleet = self.source.active_nodes()
        self.assertEqual(["alpha", "beta", "gamma"], [row["node_id"] for row in expected_fleet])
        expected_role = self.backend.role_record("alpha")
        self.assertEqual("current-repository", expected_role["answers"]["repo"])
        expected_node = next(row for row in expected_fleet if row["node_id"] == "alpha")
        expected_managed = self.source.managed_verbs("alpha", "Codex")
        self.assertIsNotNone(warm(self.root))
        for label, query, expected in (
                ("complete fleet", self.source.active_nodes, expected_fleet),
                ("selected node", lambda: self.source.active_node("alpha"), expected_node),
                ("current role", lambda: self.source.role_record("alpha"), expected_role),
                ("wake posture", lambda: self.source.wake_status("alpha"), "none"),
                ("managed verbs", lambda: self.source.managed_verbs("alpha", "Codex"), expected_managed)):
            with self.subTest(query=label):
                with IOProbe(self.root.tenant_home) as probe:
                    actual = query()
                probe.assert_complete()
                self.assertEqual(expected, actual)
                self.assertEqual(0, probe.read_bytes_by_path.get("registry/entries.jsonl", 0),
                    "a certified current projection replayed superseded registry history")

    def test_next_public_call_reobserves_registration_and_retirement(self):
        self.source.active_nodes()
        self.add_node("delta")
        self.registry.retire("beta")
        actual = self.source.active_nodes()
        self.assertEqual(["alpha", "delta", "gamma"], [row["node_id"] for row in actual])
        for row in actual:
            with self.subTest(node=row["node_id"]):
                self.assertEqual(self.source.active_node(row["node_id"]), row)

    def test_boot_holds_one_registry_load_independently_of_roster_size(self):
        from floati.admin_cli import _node_boot
        from floati.cli import _parser
        from floati.registry_prefix import warm
        # Nearest property: the complete certified projection above. Read work
        # while the lock is held is deterministic; elapsed-time ratios are not.
        self.registry.register("architect-a", "Codex")
        RoleStepWizard(self.root, self.backend, self.templates,
            id_factory=uuid7_hex).assign_from_keys(
                ["architect-a", "architect", "floati", "foreign-project", "owner-tier"], io.StringIO())
        declarations = self.root.tenant_home.parent / "declared.json"
        declarations.write_text(json.dumps({"schema_version": 0, "roots": [{
            "bus_id": "fleet", "root": str(self.root.tenant_home),
            "architect_node": "architect-a", "downstream": []}]}))
        args = _parser().parse_args(["node", "boot", "--root", str(self.root.tenant_home),
            "--node", "alpha", "--declared-roots", str(declarations),
            "--managed-executable", "/unused/managed", "--profile", "unused", "--json"])
        for size in (5, 50):
            with self.subTest(nodes=size):
                bank = self.root.tenant_home / ".registry-prefix"
                if bank.exists():
                    shutil.rmtree(bank)
                for index in range(len(self.registry.active_node_ids()), size):
                    self.add_node("member-" + str(index))
                warm(self.root)
                with IOProbe(self.root.tenant_home) as probe:
                    held_members = []
                    count = probe._count
                    def counted(entry, length):
                        if (entry is not None and entry["path"] is not None
                                and entry["path"].startswith(".registry-prefix/")
                                and any(hold["path"] == "registry/entries.jsonl.lock"
                                        for hold in probe._open_holds.values())
                                and ("/nodes/" in entry["path"] or "/buckets/" in entry["path"])):
                            held_members.append((entry["path"], length))
                        count(entry, length)
                    probe._count = counted
                    status, artifact, exit_code = _node_boot(args)
                probe.assert_complete()
                self.assertEqual(("ok", 0, size), (status, exit_code, len(artifact["fleet_map"]["nodes"])))
                holds = [h for h in probe.holds if h["path"] == "registry/entries.jsonl.lock"]
                self.assertEqual(1, len(holds), "one boot must capture one registry cut")
                self.assertEqual([], held_members, "roster and selected members must be checked after releasing the writer lock")

    def test_missing_role_keeps_the_native_refusal(self):
        self.registry.register("unassigned", "Codex")
        with self.assertRaises(ProtocolRefusal) as native:
            self.backend.role_record("unassigned")
        with self.assertRaises(ProtocolRefusal) as projected:
            self.source.active_nodes()
        self.assertEqual(vars(native.exception), vars(projected.exception))
