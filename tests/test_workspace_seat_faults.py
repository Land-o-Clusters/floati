from __future__ import annotations

import errno
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import floati.seat_declaration as seat_declaration
from floati import fixture_ids as public_ids
from floati.admin_registry import RegistryAdminBackend
from floati.errors import DurabilityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.node_wizard import NodeAddPlan
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_declaration import FleetGovernance, WorkspaceBinding


NOW = "2026-08-29T14:00:00.000Z"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AUTHORITIES = (
    "dispatch_bounded_work",
    "gate_results_before_merge",
    "decide_non_owner_tier_questions",
)
OWNER_TIERS = ("publishing", "credentials", "key_custody")


class WorkspaceSeatFaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "governed", create=True)
        for relative in ("cursors", "receipts/deliveries"):
            self.root.resolve_relative(relative).mkdir(parents=True, exist_ok=True)
        self.governance = FleetGovernance.create(
            self.root,
            topology="star",
            coordinator="architect-a",
            coordinator_authority=AUTHORITIES,
            owner_tier=OWNER_TIERS,
        )
        self.backend = RegistryAdminBackend(
            self.root, repository=REPOSITORY_ROOT
        )

    def plan(self, node: str = public_ids.builder("a")) -> NodeAddPlan:
        workspace = self.root.path / "nodes" / node
        record = {
            "schema_version": 0,
            "id": "registry-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": NOW,
            "kind": "registry_entry",
            "node_id": node,
            "role": "Codex",
            "state": "active",
        }
        return NodeAddPlan(
            node,
            "Codex",
            "permanent",
            None,
            str(workspace),
            (record,),
            None,
            None,
            self.governance,
        )

    @staticmethod
    def probe_fact(
        coordinate: str,
        path: Path | None,
        verdict: str,
        reason_code: str,
    ) -> dict:
        return {
            "schema_version": 0,
            "coordinate": coordinate,
            "path": None if path is None else str(path),
            "verdict": verdict,
            "reason_code": reason_code,
            "errno_name": None,
            "observed_at": NOW,
            "residue_path": None,
        }

    def test_deaf_declaration_aggregates_every_problem_before_marker_or_lease(self) -> None:
        """C-2: one refusal names the full write set before the seat becomes live."""
        plan = replace(self.plan(), lifetime="temporary", lease_minutes=60)
        cursor_path = self.root.path / "cursors"
        git_path = REPOSITORY_ROOT / ".git"
        facts = (
            self.probe_fact(
                "bus_cursors", cursor_path, "refused", "permission_denied"
            ),
            self.probe_fact(
                "bus_receipts",
                self.root.path / "receipts" / "deliveries",
                "writable",
                "probe_succeeded",
            ),
            self.probe_fact(
                "bus_ledger", self.root.path, "writable", "probe_succeeded"
            ),
            self.probe_fact(
                "git_common_dir", git_path, "unknown", "probe_errno_unmapped"
            ),
            self.probe_fact(
                "git_worktree_admin_dir",
                git_path,
                "writable",
                "probe_succeeded",
            ),
        )

        with mock.patch(
            "floati.admin_registry.probe_write_set", return_value=facts
        ) as probe, mock.patch.object(
            self.backend, "_commit"
        ) as commit:
            with self.assertRaises(ProtocolRefusal) as raised:
                self.backend.commit_add(plan)

        self.assertEqual("seat_declaration_deaf", raised.exception.code)
        self.assertIn(
            f"bus_cursors path={cursor_path} reason=permission_denied",
            raised.exception.detail,
        )
        self.assertIn(
            f"git_common_dir path={git_path} reason=probe_errno_unmapped",
            raised.exception.detail,
        )
        self.assertNotIn("bus_receipts", raised.exception.detail)
        self.assertIn(str(cursor_path), raised.exception.remedy)
        self.assertIn(str(git_path), raised.exception.remedy)
        self.assertIn(".codex/config.toml", raised.exception.remedy)
        probe.assert_called_once_with(
            self.root,
            plan.node_id,
            repository=REPOSITORY_ROOT,
        )
        commit.assert_not_called()
        self.assertFalse((Path(plan.workspace) / "SEAT.json").exists())
        self.assertEqual((), Registry(self.root).active_node_ids())

    def test_deaf_declaration_unknown_path_is_typed_and_uses_harness_remedy(self) -> None:
        """C-2: an underivable coordinate is not silently treated as writable."""
        plan = replace(self.plan(), harness="Cursor")
        facts = tuple(
            self.probe_fact(
                coordinate,
                None if coordinate == "git_common_dir" else self.root.path,
                "unknown" if coordinate == "git_common_dir" else "writable",
                "coordinate_underivable"
                if coordinate == "git_common_dir"
                else "probe_succeeded",
            )
            for coordinate in (
                "bus_cursors",
                "bus_receipts",
                "bus_ledger",
                "git_common_dir",
                "git_worktree_admin_dir",
            )
        )

        with mock.patch(
            "floati.admin_registry.probe_write_set", return_value=facts
        ):
            with self.assertRaises(ProtocolRefusal) as raised:
                self.backend.commit_add(plan)

        self.assertEqual("seat_declaration_deaf", raised.exception.code)
        self.assertIn(
            "git_common_dir path=null reason=coordinate_underivable",
            raised.exception.detail,
        )
        self.assertEqual(
            "no verified remedy is recorded for this harness",
            raised.exception.remedy,
        )
        self.assertFalse((Path(plan.workspace) / "SEAT.json").exists())
        self.assertEqual((), Registry(self.root).active_node_ids())

    def test_parent_fsync_failure_after_visible_registry_frame_retains_seat(self) -> None:
        """Catches rollback deleting a seat whose registry append is already visible."""
        plan = self.plan()
        real_commit = self.backend._commit
        real_fsync = os.fsync

        def commit_with_parent_fsync_failure(records, decide):
            calls = 0

            def fail_second_fsync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(errno.EIO, "injected registry parent fsync failure")
                real_fsync(descriptor)

            with mock.patch("floati.jsonl.os.fsync", side_effect=fail_second_fsync):
                return real_commit(records, decide)

        with mock.patch.object(
            self.backend, "_commit", side_effect=commit_with_parent_fsync_failure
        ):
            with self.assertRaises(DurabilityFailure):
                self.backend.commit_add(plan)

        workspace = Path(plan.workspace)
        self.assertEqual((public_ids.builder("a"),), Registry(self.root).active_node_ids())
        self.assertTrue(workspace.is_dir())
        self.assertTrue((workspace / "SEAT.json").is_file())

    def test_competing_matching_marker_is_not_owned_by_failed_registration(self) -> None:
        """Catches rollback deleting a matching marker published by another writer."""
        plan = self.plan()
        workspace = Path(plan.workspace)
        marker = workspace / "SEAT.json"
        competing_identity: list[tuple[int, int]] = []

        def competing_publication(source, destination, **kwargs) -> None:
            if Path(source).is_absolute():
                encoded = Path(source).read_bytes()
            else:
                descriptor = os.open(source, os.O_RDONLY, dir_fd=kwargs["src_dir_fd"])
                try:
                    encoded = os.read(descriptor, 16 * 1024)
                finally:
                    os.close(descriptor)
            marker.write_bytes(encoded)
            identity = os.stat(marker, follow_symlinks=False)
            competing_identity.append((identity.st_dev, identity.st_ino))
            raise FileExistsError(errno.EEXIST, "competing marker publication")

        with mock.patch(
            "floati.seat_declaration.os.link", side_effect=competing_publication
        ), mock.patch.object(
            self.backend,
            "_commit",
            side_effect=ProtocolRefusal(
                "registry_preview_invalid", "injected definitely-uncommitted add"
            ),
        ):
            with self.assertRaises(ProtocolRefusal):
                self.backend.commit_add(plan)

        self.assertTrue(marker.is_file(), "rollback deleted the foreign marker")
        identity = os.stat(marker, follow_symlinks=False)
        self.assertEqual(competing_identity, [(identity.st_dev, identity.st_ino)])
        self.assertEqual((), Registry(self.root).active_node_ids())

    def test_nodes_ancestor_symlink_swap_refuses_without_external_marker(self) -> None:
        """Catches authorization following a swapped nodes ancestor outside the root."""
        plan = self.plan()
        nodes = self.root.path / "nodes"
        parked_nodes = self.root.path / "nodes-before-swap"
        external_nodes = self.base / "external-nodes"
        external_workspace = external_nodes / plan.node_id
        real_prepare = self.backend._prepare_workspace

        def prepare_then_swap(node_id: str):
            prepared = real_prepare(node_id)
            nodes.rename(parked_nodes)
            external_workspace.mkdir(parents=True)
            nodes.symlink_to(external_nodes, target_is_directory=True)
            return prepared

        with mock.patch.object(
            self.backend, "_prepare_workspace", side_effect=prepare_then_swap
        ):
            with self.assertRaises(ProtocolRefusal) as raised:
                self.backend.commit_add(plan)

        self.assertEqual("node_workspace_invalid", raised.exception.code)
        self.assertFalse((external_workspace / "SEAT.json").exists())
        self.assertEqual((), Registry(self.root).active_node_ids())

    def test_workspace_inode_swap_refuses_without_replacement_marker(self) -> None:
        """Catches publication into a workspace that replaced the prepared directory."""
        plan = self.plan()
        workspace = Path(plan.workspace)
        parked_workspace = self.root.path / (public_ids.builder("a") + "-before-swap")
        real_prepare = self.backend._prepare_workspace

        def prepare_then_swap(node_id: str):
            prepared = real_prepare(node_id)
            workspace.rename(parked_workspace)
            workspace.mkdir(mode=0o700)
            return prepared

        with mock.patch.object(
            self.backend, "_prepare_workspace", side_effect=prepare_then_swap
        ):
            with self.assertRaises(ProtocolRefusal) as raised:
                self.backend.commit_add(plan)

        self.assertEqual("node_workspace_invalid", raised.exception.code)
        self.assertFalse((workspace / "SEAT.json").exists())
        self.assertEqual((), Registry(self.root).active_node_ids())

    def test_rollback_marker_replacement_after_identity_check_is_retained(self) -> None:
        """Catches compare-then-unlink deleting a non-cooperating marker replacement."""
        plan = self.plan()
        workspace = Path(plan.workspace)
        marker = workspace / "SEAT.json"
        parked_marker = self.root.path / "owned-seat-before-rollback"
        real_stat_at = seat_declaration._stat_at
        marker_observations = 0
        replacement_identity: list[tuple[int, int]] = []

        def replace_on_rollback_stat(descriptor: int, name: str):
            nonlocal marker_observations
            identity = real_stat_at(descriptor, name)
            if name == "SEAT.json":
                marker_observations += 1
                if marker_observations == 2:
                    marker.rename(parked_marker)
                    marker.write_bytes(b"foreign marker at rollback boundary\n")
                    replacement = os.stat(marker, follow_symlinks=False)
                    replacement_identity.append(
                        (replacement.st_dev, replacement.st_ino)
                    )
            return identity

        with mock.patch(
            "floati.seat_declaration._stat_at",
            side_effect=replace_on_rollback_stat,
        ), mock.patch.object(
            self.backend,
            "_commit",
            side_effect=ProtocolRefusal(
                "registry_preview_invalid", "injected definitely-uncommitted add"
            ),
        ):
            with self.assertRaises(ProtocolRefusal):
                self.backend.commit_add(plan)

        self.assertTrue(marker.is_file(), "rollback deleted the foreign marker")
        identity = os.stat(marker, follow_symlinks=False)
        self.assertEqual(replacement_identity, [(identity.st_dev, identity.st_ino)])
        self.assertEqual(b"foreign marker at rollback boundary\n", marker.read_bytes())
        self.assertTrue(parked_marker.is_file())

    def test_rollback_workspace_replacement_after_identity_check_is_retained(self) -> None:
        """Catches compare-then-rmdir deleting a replacement workspace coordinate."""
        plan = replace(self.plan(), governance=None)
        workspace = Path(plan.workspace)
        parked_workspace = self.root.path / "owned-workspace-before-rollback"
        real_stat_at = seat_declaration._stat_at
        workspace_observations = 0
        replacement_identity: list[tuple[int, int]] = []

        def replace_on_rollback_stat(descriptor: int, name: str):
            nonlocal workspace_observations
            identity = real_stat_at(descriptor, name)
            if name == plan.node_id:
                workspace_observations += 1
                if workspace_observations == 2:
                    workspace.rename(parked_workspace)
                    workspace.mkdir(mode=0o700)
                    replacement = os.stat(workspace, follow_symlinks=False)
                    replacement_identity.append(
                        (replacement.st_dev, replacement.st_ino)
                    )
            return identity

        with mock.patch(
            "floati.seat_declaration._stat_at",
            side_effect=replace_on_rollback_stat,
        ), mock.patch.object(
            self.backend,
            "_commit",
            side_effect=ProtocolRefusal(
                "registry_preview_invalid", "injected definitely-uncommitted add"
            ),
        ):
            with self.assertRaises(ProtocolRefusal):
                self.backend.commit_add(plan)

        self.assertTrue(workspace.is_dir(), "rollback deleted the foreign workspace")
        identity = os.stat(workspace, follow_symlinks=False)
        self.assertEqual(replacement_identity, [(identity.st_dev, identity.st_ino)])
        self.assertTrue(parked_workspace.is_dir())

    def test_rollback_nodes_replacement_after_identity_check_is_retained(self) -> None:
        """Catches compare-then-rmdir deleting a replacement nodes coordinate."""
        plan = replace(self.plan(), governance=None)
        workspace = Path(plan.workspace)
        nodes = workspace.parent
        parked_workspace = self.root.path / "owned-workspace-before-nodes-swap"
        parked_nodes = self.root.path / "owned-nodes-before-rollback"
        real_stat_at = seat_declaration._stat_at
        nodes_observations = 0
        replacement_identity: list[tuple[int, int]] = []

        def replace_on_rollback_stat(descriptor: int, name: str):
            nonlocal nodes_observations
            identity = real_stat_at(descriptor, name)
            if name == "nodes":
                nodes_observations += 1
                if nodes_observations == 2:
                    if workspace.exists():
                        workspace.rename(parked_workspace)
                    nodes.rename(parked_nodes)
                    nodes.mkdir(mode=0o700)
                    replacement = os.stat(nodes, follow_symlinks=False)
                    replacement_identity.append(
                        (replacement.st_dev, replacement.st_ino)
                    )
            return identity

        with mock.patch(
            "floati.seat_declaration._stat_at",
            side_effect=replace_on_rollback_stat,
        ), mock.patch.object(
            self.backend,
            "_commit",
            side_effect=ProtocolRefusal(
                "registry_preview_invalid", "injected definitely-uncommitted add"
            ),
        ):
            with self.assertRaises(ProtocolRefusal):
                self.backend.commit_add(plan)

        self.assertTrue(nodes.is_dir(), "rollback deleted the foreign nodes directory")
        identity = os.stat(nodes, follow_symlinks=False)
        self.assertEqual(replacement_identity, [(identity.st_dev, identity.st_ino)])
        self.assertTrue(parked_nodes.is_dir())

    def test_prepare_failure_retains_workspace_replaced_after_lock_release(self) -> None:
        """Catches preparation cleanup deleting a coordinate installed after close."""
        plan = self.plan()
        workspace = Path(plan.workspace)
        parked_workspace = self.root.path / "owned-workspace-before-prepare-cleanup"
        real_close = os.close
        replacement_identity: list[tuple[int, int]] = []

        def close_then_replace(descriptor: int) -> None:
            real_close(descriptor)
            if not replacement_identity and workspace.is_dir():
                workspace.rename(parked_workspace)
                workspace.mkdir(mode=0o700)
                replacement = os.stat(workspace, follow_symlinks=False)
                replacement_identity.append((replacement.st_dev, replacement.st_ino))

        with mock.patch.object(
            WorkspaceBinding,
            "verify",
            side_effect=ProtocolRefusal(
                "node_workspace_invalid", "injected preparation failure"
            ),
        ), mock.patch(
            "floati.seat_declaration.os.close", side_effect=close_then_replace
        ):
            with self.assertRaises(ProtocolRefusal):
                self.backend.commit_add(plan)

        self.assertTrue(
            workspace.is_dir(), "preparation cleanup deleted the foreign workspace"
        )
        identity = os.stat(workspace, follow_symlinks=False)
        self.assertEqual(replacement_identity, [(identity.st_dev, identity.st_ino)])
        self.assertTrue(parked_workspace.is_dir())


if __name__ == "__main__":
    unittest.main()
