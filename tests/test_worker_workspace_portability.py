from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati import host_paths
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.records import validate_record
from floati.root import FloatiRoot
from floati.runtruth import RUN_KINDS
from floati.sequencer import sequencer_socket_path
from tests import test_approval_suspension as approval_tests
from tests import test_spawn_groups as spawn_tests
from tests.schema_validation import SchemaValidationError, validate_json_schema

FLOATI_ENTRY = Path(__file__).resolve().parents[1] / "scripts" / "floati"
#: The product input that moves the fixed roots; the suite sets it in tests/__init__.py.
HOST_TEMP_ROOT_VARIABLE = "FLOATI_HOST_TEMP_ROOT"


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


def _host_temp_root(value):
    """Set or clear the host temporary root override for one block."""
    environment = {key: item for key, item in os.environ.items()
                   if key != HOST_TEMP_ROOT_VARIABLE}
    if value is not None:
        environment[HOST_TEMP_ROOT_VARIABLE] = str(value)
    return patch.dict(os.environ, environment, clear=True)


class HostTempRootTests(unittest.TestCase):
    """The fixed product roots under the native temp root move only on request."""

    def setUp(self) -> None:
        # Use the canonical short root so the sequencer path fits on macOS.
        temporary = tempfile.TemporaryDirectory(dir=host_paths.capture_temporary_parent())
        self.addCleanup(temporary.cleanup)
        self.owned = Path(temporary.name).resolve()
        self.root = FloatiRoot.open(self.owned / "fleet", "alpha")

    def assert_refused(self, operation) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            operation()
        self.assertEqual("host_temp_root_invalid", caught.exception.code)

    def test_unset_override_keeps_the_native_sequencer_endpoint(self) -> None:
        """Catches the seam moving a real user's endpoint, which live clients already use."""
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), patch.object(
                host_paths.sys, "platform", platform
            ), _host_temp_root(None):
                endpoint = sequencer_socket_path(self.root)
                self.assertEqual(Path("\x2ftmp"), endpoint.parent.parent)
                self.assertEqual("sequencer.sock", endpoint.name)

    def test_override_moves_both_roots_beneath_the_owned_directory(self) -> None:
        """Catches a product root that ignores the seam and lands beside a real fleet."""
        with _host_temp_root(self.owned):
            self.assertEqual(self.owned / "floati-work", host_paths.worker_workspace_root())
            endpoint = sequencer_socket_path(self.root)
        self.assertEqual(self.owned, endpoint.parent.parent)
        with _host_temp_root(None):
            native = sequencer_socket_path(self.root)
        self.assertEqual(native.parent.name, endpoint.parent.name)
        self.assertEqual(native.name, endpoint.name)

    def test_invalid_override_refuses_and_never_falls_back(self) -> None:
        """Catches a bad value silently returning the native default roots."""
        (self.owned / "real" / "child").mkdir(parents=True, mode=0o700)
        (self.owned / "link").symlink_to(self.owned / "real")
        (self.owned / "file").write_text("not a directory", encoding="utf-8")
        shared = self.owned / "shared"
        shared.mkdir(mode=0o700)
        shared.chmod(0o770)
        cases = {
            "empty": "",
            "relative": "relative/root",
            "not-normalized": f"{self.owned}/real/../real",
            "missing": str(self.owned / "absent"),
            "symlink-leaf": str(self.owned / "link"),
            "symlink-ancestor": str(self.owned / "link" / "child"),
            "regular-file": str(self.owned / "file"),
            "group-writable": str(shared),
        }
        if os.getuid() != 0:
            cases["foreign-owner"] = "/"
        for case, value in cases.items():
            with self.subTest(case=case), _host_temp_root(value):
                self.assert_refused(host_paths.worker_workspace_root)
                self.assert_refused(lambda: sequencer_socket_path(self.root))

    def test_override_too_long_for_a_socket_refuses_only_the_endpoint(self) -> None:
        """Catches an endpoint the kernel would refuse at bind time, after the epoch opened."""
        deep = self.owned
        while len(os.fsencode(str(deep))) <= 108:
            deep = deep / ("d" * 16)
        deep.mkdir(parents=True, mode=0o700)
        with _host_temp_root(deep):
            self.assertEqual(deep / "floati-work", host_paths.worker_workspace_root())
            self.assert_refused(lambda: sequencer_socket_path(self.root))

    def test_suite_and_cli_children_share_the_suite_owned_root(self) -> None:
        """Catches a child that cannot see the seam, so a test server and client never meet."""
        inherited = os.environ.get(HOST_TEMP_ROOT_VARIABLE)
        self.assertIsNotNone(inherited, "the suite must own its host temporary root")
        suite_root = Path(str(inherited))
        self.assertEqual(suite_root / "floati-work", host_paths.worker_workspace_root())
        home = self.owned / "cli-fleet"
        initialized = subprocess.run([str(FLOATI_ENTRY), "init", "--root", str(home)],
                                     capture_output=True, text=True, check=False)
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        status = subprocess.run([str(FLOATI_ENTRY), "sequencer", "status", "--root", str(home)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(0, status.returncode, status.stderr)
        endpoint = Path(json.loads(status.stdout)["evidence"]["socket_path"])
        self.assertEqual(suite_root, endpoint.parent.parent)

        environment = dict(os.environ, **{HOST_TEMP_ROOT_VARIABLE: "relative/root"})
        refused = subprocess.run([str(FLOATI_ENTRY), "sequencer", "status", "--root", str(home)],
                                 capture_output=True, text=True, check=False, env=environment)
        self.assertEqual(20, refused.returncode, refused.stderr)
        artifact = json.loads(refused.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("host_temp_root_invalid", artifact["evidence"]["code"])


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
            ), _host_temp_root(None):
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

            for case_index, workspace in enumerate((
                f"relative/floati-work/{item_id}",
                f"\x2ftmp/not-floati-work/{item_id}",
                "\x2ftmp/floati-work/work-not-a-uuid7",
                f"\x2ftmp/floati-work/{item_id}\n",
                f"/srv/\x1f/floati-work/{item_id}",
                f"\x2ftmp/../floati-work/{item_id}",
                f"/./floati-work/{item_id}",
                f"/../floati-work/{item_id}",
            )):
                with self.subTest(kind=record["kind"], workspace_case=(
                    "relative-prefix",
                    "wrong-work-directory",
                    "non-UUID7-work-id",
                    "trailing-newline",
                    "root-control-U001F",
                    "internal-parent-traversal",
                    "root-dot-component",
                    "root-parent-component",
                )[case_index]):
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

            for case_index, workspace in enumerate((
                f"relative/floati-work/{item_id}",
                f"\x2ftmp/not-floati-work/{item_id}",
                "\x2ftmp/floati-work/work-not-a-uuid7",
                f"\x2ftmp/floati-work/{item_id}\n",
                f"/srv/\x1f/floati-work/{item_id}",
                f"\x2ftmp/../floati-work/{item_id}",
                f"/./floati-work/{item_id}",
                f"/../floati-work/{item_id}",
            )):
                with self.subTest(kind=record["kind"], workspace_case=(
                    "relative-prefix",
                    "wrong-work-directory",
                    "non-UUID7-work-id",
                    "trailing-newline",
                    "root-control-U001F",
                    "internal-parent-traversal",
                    "root-dot-component",
                    "root-parent-component",
                )[case_index]):
                    self.assertFalse(
                        schema_accepts(dict(record, workspace=workspace))
                    )


if __name__ == "__main__":
    unittest.main()
