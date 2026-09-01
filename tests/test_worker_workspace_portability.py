from __future__ import annotations

import importlib
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.records import validate_record
from floati.runtruth import RUN_KINDS
from tests import test_approval_suspension as approval_tests
from tests import test_spawn_groups as spawn_tests
from tests.schema_validation import SchemaValidationError, validate_json_schema


def _runtime_accepts(record: dict[str, object]) -> bool:
    try:
        validate_record(
            dict(record),
            "alpha",
            RUN_KINDS | {str(record["kind"])},
            integrity=False,
        )
    except (IntegrityFailure, ProtocolRefusal):
        return False
    return True


def _portable_records() -> tuple[dict[str, object], ...]:
    work_id = "work-" + uuid7_hex()
    work_item = {
        "schema_version": 0,
        "id": work_id,
        "tenant_id": "alpha",
        "timestamp": "2026-09-01T00:00:00.000Z",
        "kind": "work_item",
        "title": "portable worker workspace",
        "owner": "worker-a",
        "artifact_bindings": [],
        "workspace": f"\x2ftmp/floati-work/{work_id}",
    }

    spawn = spawn_tests.SpawnGroupFixtures()
    group = spawn.group()
    amendment = spawn.amendment(group)
    child = dict(
        spawn.admitted_record(group, amendment),
        workspace=f"\x2ftmp/floati-work/{spawn.child}",
    )

    approval = approval_tests.ApprovalSuspensionProjectionTests()
    _, state = approval.started_attempt()
    suspended = approval.suspension_record(
        state, workspace=f"\x2ftmp/floati-work/{state['item_id']}"
    )
    consumed = approval.consumption_record(state, suspended)
    return work_item, child, suspended, consumed


class WorkerWorkspacePortabilityTests(unittest.TestCase):
    def test_capture_temporary_parent_follows_the_patched_host_platform(self) -> None:
        """Catches capture staging that remains pinned to one package manager."""

        module_spec = importlib.util.find_spec("floati.host_paths")
        self.assertIsNotNone(module_spec, "floati.host_paths must own host path derivation")
        host_paths = importlib.import_module("floati.host_paths")
        self.assertTrue(
            hasattr(host_paths, "capture_temporary_parent"),
            "floati.host_paths must derive the capture temporary parent",
        )

        for platform, expected in (
            ("darwin", "\x2fprivate/tmp"),
            ("linux", "\x2ftmp"),
        ):
            with self.subTest(platform=platform), patch.object(
                host_paths.sys, "platform", platform
            ):
                self.assertEqual(expected, str(host_paths.capture_temporary_parent()))

    def test_worker_workspace_root_follows_the_patched_host_platform(self) -> None:
        """Catches a worker root that remains pinned to the developer's host."""

        module_spec = importlib.util.find_spec("floati.host_paths")
        self.assertIsNotNone(module_spec, "floati.host_paths must own host path derivation")
        host_paths = importlib.import_module("floati.host_paths")

        for platform, expected in (
            ("darwin", "\x2fprivate/tmp/floati-work"),
            ("linux", "\x2ftmp/floati-work"),
        ):
            with self.subTest(platform=platform), patch.object(
                host_paths.sys, "platform", platform
            ):
                self.assertEqual(expected, str(host_paths.worker_workspace_root()))

    def test_runtime_workspace_validators_accept_portable_shape_and_keep_refusals(
        self,
    ) -> None:
        """Catches durable records pinning a host prefix or accepting a bad shape."""

        records = _portable_records()
        for record in records:
            item_id = str(record.get("child_item_id", record.get("item_id", record["id"])))
            for prefix in ("\x2fprivate/tmp", "\x2ftmp", "/srv/agent"):
                with self.subTest(kind=record["kind"], prefix=prefix):
                    self.assertTrue(
                        _runtime_accepts(
                            dict(
                                record,
                                workspace=f"{prefix}/floati-work/{item_id}",
                            )
                        )
                    )

            for workspace in (
                f"relative/floati-work/{item_id}",
                f"\x2ftmp/not-floati-work/{item_id}",
                "\x2ftmp/floati-work/work-not-a-uuid7",
                f"\x2ftmp/floati-work/{item_id}\n",
                f"/srv/\x1f/floati-work/{item_id}",
                f"\x2ftmp/../floati-work/{item_id}",
                f"/./floati-work/{item_id}",
                f"/../floati-work/{item_id}",
            ):
                with self.subTest(kind=record["kind"], workspace=workspace):
                    self.assertFalse(
                        _runtime_accepts(dict(record, workspace=workspace))
                    )

        with self.subTest(kind="work_item", shape="wrong-work-id"):
            self.assertFalse(
                _runtime_accepts(
                    dict(
                        records[0],
                        workspace=f"\x2ftmp/floati-work/work-{uuid7_hex()}",
                    )
                )
            )

    def test_workspace_schemas_match_the_portable_runtime_shape(self) -> None:
        """Catches a durable schema retaining a host prefix after runtime ports."""

        schema_paths = {
            "work_item": Path("schemas/v0/work-item-record.schema.json"),
            "child_admitted": Path("schemas/v1/child-admitted-record.schema.json"),
            "attempt_suspended_for_approval": Path(
                "schemas/v1/attempt-suspended-for-approval-record.schema.json"
            ),
            "approval_consumed_for_resume": Path(
                "schemas/v1/approval-consumed-for-resume-record.schema.json"
            ),
        }

        def schema_accepts(record: dict[str, object]) -> bool:
            try:
                validate_json_schema(record, schema_paths[str(record["kind"])])
            except SchemaValidationError:
                return False
            return True

        for record in _portable_records():
            item_id = str(record.get("child_item_id", record.get("item_id", record["id"])))
            for prefix in ("\x2fprivate/tmp", "\x2ftmp", "/srv/agent"):
                with self.subTest(kind=record["kind"], prefix=prefix):
                    self.assertTrue(
                        schema_accepts(
                            dict(
                                record,
                                workspace=f"{prefix}/floati-work/{item_id}",
                            )
                        )
                    )

            for workspace in (
                f"relative/floati-work/{item_id}",
                f"\x2ftmp/not-floati-work/{item_id}",
                "\x2ftmp/floati-work/work-not-a-uuid7",
                f"\x2ftmp/floati-work/{item_id}\n",
                f"/srv/\x1f/floati-work/{item_id}",
                f"\x2ftmp/../floati-work/{item_id}",
                f"/./floati-work/{item_id}",
                f"/../floati-work/{item_id}",
            ):
                with self.subTest(kind=record["kind"], workspace=workspace):
                    self.assertFalse(
                        schema_accepts(dict(record, workspace=workspace))
                    )


if __name__ == "__main__":
    unittest.main()
