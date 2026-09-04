from __future__ import annotations

from tests.test_cli import LAUNCHER

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.schema_validation import SchemaValidationError, validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HARNESS = (
    REPOSITORY_ROOT
    / "tests/fixtures/codex-thread-observer/reference_harness.py"
)
SCHEMA = REPOSITORY_ROOT / "schemas/v1/thread-observation-status-artifact.schema.json"
NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
THREAD_ID = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5678"


def tree_snapshot(path: Path) -> dict[str, tuple[str, bytes]]:
    return {
        child.relative_to(path).as_posix(): (
            "directory" if child.is_dir() else "file",
            b"" if child.is_dir() else child.read_bytes(),
        )
        for child in sorted(path.rglob("*"))
    }


class ThreadCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "thread-cli"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        registry.register("owner-node", "Codex")
        registry.register("observer-node", "Codex")
        self.item = WorkLog(self.root).add("observe one thread", "owner-node", [])
        self.source_counter = 0

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def artifact(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
        assert result.stderr == ""
        return json.loads(result.stdout)

    def observer(self, mode: str = "idle"):
        from floati.thread_observations import ThreadObserver
        from floati.thread_source import CodexLocalThreadSource

        self.source_counter += 1
        prefix = Path(self.temp.name) / f"source-{self.source_counter}"
        source = CodexLocalThreadSource._for_test(
            [
                sys.executable,
                str(HARNESS),
                mode,
                str(prefix) + "-methods",
                str(prefix) + "-params.json",
                str(prefix) + "-diagnostic.json",
            ]
        )
        return ThreadObserver._for_test(self.root, source)

    def test_status_artifact_schema_attention_and_coordinate_redaction(self) -> None:
        from floati.projection import ThreadObservationStatusProjection
        from floati.thread_source import ThreadReadResult
        from tests.test_thread_observations import provider_uuid7

        observer = self.observer("active-approval")
        attachment = observer.register_work_item(
            str(self.item["id"]), THREAD_ID, "observer-node", now=NOW
        )
        observer.observe(str(attachment["id"]), now=NOW)

        for attention, result in (
            (
                "waiting_on_user_input",
                ThreadReadResult(
                    "active", ("waiting_on_user_input",), 1786622401,
                    "observed", "exact_thread_read",
                ),
            ),
            (
                "multiple",
                ThreadReadResult(
                    "active",
                    ("waiting_on_approval", "waiting_on_user_input"),
                    1786622402,
                    "observed", "exact_thread_read",
                ),
            ),
            (
                "none",
                ThreadReadResult(
                    "idle", (), 1786622403, "observed", "exact_thread_read"
                ),
            ),
        ):
            item = WorkLog(self.root).add(
                f"observe {attention}", "owner-node", []
            )
            selected = self.observer()
            registered = selected.register_work_item(
                str(item["id"]), provider_uuid7(), "observer-node", now=NOW
            )
            with mock.patch.object(selected.source, "read", return_value=result):
                selected.observe(str(registered["id"]), now=NOW)
        unknown_item = WorkLog(self.root).add("observe unknown", "owner-node", [])
        self.observer().register_work_item(
            str(unknown_item["id"]), provider_uuid7(), "observer-node", now=NOW
        )

        listed = ThreadObservationStatusProjection(self.root).artifact(NOW)
        envelope = {
            "schema_version": 1,
            "artifact_version": 0,
            "command": "threads",
            "status": "ok",
            "evidence": listed,
        }
        validate_json_schema(envelope, SCHEMA)
        nonactive_flags = json.loads(json.dumps(envelope))
        flagged = next(
            row
            for row in nonactive_flags["evidence"]["attachments"]
            if row["active_flags"]["value"]
        )
        flagged["provider_status"]["value"] = "idle"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(nonactive_flags, SCHEMA)
        half_coordinate = json.loads(json.dumps(envelope))
        half_coordinate["evidence"]["attachments"][0]["provider"] = "codex_local"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(half_coordinate, SCHEMA)
        self.assertEqual(
            [
                "waiting_on_approval",
                "waiting_on_user_input",
                "multiple",
                "unknown",
                "none",
            ],
            [row["attention"]["value"] for row in listed["attachments"]],
        )
        self.assertEqual(
            ("measured", "derived"),
            (
                listed["attachments"][0]["provider_status"]["evidence_class"],
                listed["attachments"][0]["attention"]["evidence_class"],
            ),
        )
        unknown = next(
            row
            for row in listed["attachments"]
            if row["observation_outcome"] == "unknown"
        )
        self.assertEqual(
            ("unknown", "unknown"),
            (
                unknown["provider_status"]["evidence_class"],
                unknown["attention"]["evidence_class"],
            ),
        )
        self.assertNotIn("provider_thread_id", listed["attachments"][0])

        exact = ThreadObservationStatusProjection(self.root).artifact(
            NOW, attachment_id=str(attachment["id"])
        )
        self.assertEqual(THREAD_ID, exact["attachments"][0]["provider_thread_id"])
        exact_envelope = {
            "schema_version": 1,
            "artifact_version": 0,
            "command": "thread",
            "status": "ok",
            "evidence": exact,
        }
        validate_json_schema(exact_envelope, SCHEMA)

        leaked_list = json.loads(json.dumps(envelope))
        leaked_list["evidence"]["attachments"][0].update(
            {"provider": "codex_local", "provider_thread_id": THREAD_ID}
        )
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(leaked_list, SCHEMA)

        malformed_subject = json.loads(json.dumps(envelope))
        malformed_subject["evidence"]["attachments"][0]["subject_kind"] = "attempt"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(malformed_subject, SCHEMA)

        forged_unknown = json.loads(json.dumps(envelope))
        forged_unknown["evidence"]["attachments"][0].update(
            {"observation_outcome": "unknown", "observation_reason": "provider_timeout"}
        )
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(forged_unknown, SCHEMA)

        forged_attention = json.loads(json.dumps(envelope))
        forged_attention["evidence"]["attachments"][0]["attention"]["value"] = "unknown"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(forged_attention, SCHEMA)

        empty_list = json.loads(json.dumps(envelope))
        empty_list["status"] = "no_result"
        empty_list["evidence"]["attachments"] = []
        validate_json_schema(empty_list, SCHEMA)

    def test_cli_attach_show_threads_and_detach_use_exact_read_write_boundaries(self) -> None:
        attached = self.run_cli(
            "thread", "attach", "--root", str(self.home),
            "--as", "observer-node", "--thread", THREAD_ID,
            "--work-item", str(self.item["id"]),
        )
        self.assertEqual(0, attached.returncode, attached.stderr)
        attached_artifact = self.artifact(attached)
        validate_json_schema(attached_artifact, SCHEMA)
        row = attached_artifact["evidence"]["attachments"][0]
        attachment_id = str(row["attachment_id"])

        before = tree_snapshot(self.home)
        listed = self.run_cli("threads", "--root", str(self.home))
        shown = self.run_cli(
            "thread", "show", "--root", str(self.home),
            "--attachment", attachment_id,
        )
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual(0, shown.returncode, shown.stderr)
        validate_json_schema(self.artifact(listed), SCHEMA)
        validate_json_schema(self.artifact(shown), SCHEMA)
        self.assertEqual(before, tree_snapshot(self.home))
        self.assertNotIn(
            "provider_thread_id",
            self.artifact(listed)["evidence"]["attachments"][0],
        )
        self.assertEqual(
            THREAD_ID,
            self.artifact(shown)["evidence"]["attachments"][0]["provider_thread_id"],
        )

        detached = self.run_cli(
            "thread", "detach", "--root", str(self.home),
            "--as", "observer-node", "--attachment", attachment_id,
        )
        self.assertEqual(0, detached.returncode, detached.stderr)
        detached_artifact = self.artifact(detached)
        validate_json_schema(detached_artifact, SCHEMA)
        self.assertTrue(detached_artifact["evidence"]["attachments"][0]["detached"])

    def test_invalid_cli_shapes_refuse_before_root_or_provider_launch(self) -> None:
        new_root = Path(self.temp.name) / "must-not-exist"
        invalid = self.run_cli(
            "thread", "attach", "--root", str(new_root),
            "--as", "observer-node", "--thread", THREAD_ID,
            "--work-item", "work-018f3a2b4c5d7e8f9a0b1c2d3e4f5678",
            "--run", "run-018f3a2b4c5d7e8f9a0b1c2d3e4f5678",
        )
        raw = self.run_cli(
            "thread", "observe", "--root", str(new_root),
            "--attachment", "thread-attachment-018f3a2b4c5d7e8f9a0b1c2d3e4f5678",
            "--status", "active",
        )
        self.assertEqual(20, invalid.returncode)
        self.assertEqual(20, raw.returncode)
        self.assertFalse(new_root.exists())

    def test_fleet_summary_and_snapshot_source_follow_thread_ledger(self) -> None:
        from floati.projection import FleetProjection

        projection = FleetProjection(self.root)
        before = projection.status_artifact(NOW)
        self.assertEqual(0, before["threads"]["registered_total"])
        self.observer().register_work_item(
            str(self.item["id"]), THREAD_ID, "observer-node", now=NOW
        )
        after = projection.status_artifact(NOW)
        self.assertEqual(1, after["threads"]["registered_total"])
        self.assertNotIn(THREAD_ID, json.dumps(after["threads"]))

    def test_fleet_thread_summary_schema_requires_exact_attention_order(self) -> None:
        from floati.projection import FleetProjection

        evidence = FleetProjection(self.root).status_artifact(NOW)
        evidence["status_schema_version"] = 1
        evidence["scope"] = {
            "root": str(self.root.path),
            "tenant": self.root.tenant_id,
            "root_source": "explicit",
        }
        evidence["installer_shadow"] = {
            "outcome": "affirmative_none",
            "enumerated_roots": [],
            "found": [],
            "reason": "Every PATH entry was checked; the installed floati answers first.",
        }
        artifact = {
            "artifact_version": 0,
            "command": "status",
            "status": "ok",
            "evidence": evidence,
        }
        schema = REPOSITORY_ROOT / "schemas/v1/fleet-status-artifact.schema.json"
        validate_json_schema(artifact, schema)
        duplicate = json.loads(json.dumps(artifact))
        first = duplicate["evidence"]["threads"]["attention"][0]
        duplicate["evidence"]["threads"]["attention"] = [first] * 5
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(duplicate, schema)

    def test_help_and_copy_state_registered_pull_only_non_task_contract(self) -> None:
        for arguments in (("threads", "--help"), ("thread", "--help")):
            result = self.run_cli(*arguments)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("registered threads only", result.stdout)
            self.assertIn("pull-only", result.stdout)
            self.assertIn("provider status is not task state", result.stdout)

        generated = subprocess.run(
            ["python3", "-m", "floati.copy"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, generated.returncode, generated.stderr)
        self.assertIn("`help.threads`", generated.stdout)
        self.assertIn("`help.thread`", generated.stdout)


if __name__ == "__main__":
    unittest.main()
