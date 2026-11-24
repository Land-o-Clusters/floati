"""F2-REMOTE-CONTROL-COEXIST-1: Remote Control workers classified against leased worktrees.

Detection half only (rulings on design d60f5cb9): ONE finding code carrying a
classification field (0 silent, 1 counted-only, 2 in-seat named); foreign worker
paths are counted, never named, at every class. The census runs against a real
planted process so the RED exercises doctor end to end; every planted process is
killed by its own process group in tearDown. Injected sources for the unit
controls arrive with the mechanism; this module never references it.
"""
from __future__ import annotations

import tests  # Pin HOME before importing fixture or product code.

from copy import deepcopy
import json
import os
import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.ids import uuid7_hex
from floati.jsonl import transact
from floati.registry import REGISTRY_KINDS
from tests import test_doctor as fixtures
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.readiness import wait_process, wait_readable

FINDING_CODE = "remote_control_workers"


class RemoteControlWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DoctorContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.pids = []

    def tearDown(self):
        for pid in self.pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def plant_lease(self, workspace: Path) -> None:
        record = {
            "schema_version": 0,
            "id": "lease-" + uuid7_hex(),
            "tenant_id": self.fixture.root.tenant_id,
            "timestamp": "2026-10-11T22:00:00.000Z",
            "kind": "node_lease",
            "node_id": "builder" + uuid7_hex()[:8],
            "workspace": str(workspace),
            "expires_at": "2027-10-11T22:00:00.000Z",
            "state": "active",
        }

        def decide(records):
            return record, record

        transact(
            self.fixture.root,
            Path("registry/entries.jsonl"),
            decide,
            allowed_kinds=set(REGISTRY_KINDS),
        )

    def plant_worker(self, worker_dir: Path) -> None:
        """A real process whose argv carries the Am.6 worker spawn shape.

        A python sleeper, not /bin/sleep: restricted runtimes deny the sleep
        executable to spawned children, which made the plant die instantly.
        """
        proc = subprocess.Popen(
            ["/usr/bin/python3", "-B", "-c", "import os,time; os.write(1,b'ready\\n'); time.sleep(300)",
             "worker", "start", "--worker-dir", str(worker_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.pids.append(proc.pid)
        self.addCleanup(wait_process, proc, "planted worker collection")
        self.addCleanup(proc.stdout.close)
        wait_readable(proc.stdout, "planted worker first frame")
        self.assertEqual(b"ready\n", proc.stdout.readline())

    def rows(self, artifact):
        return [r for r in artifact["findings"] if r["code"] == FINDING_CODE]

    def artifact(self):
        try:
            return self.fixture.doctor(no_sandbox=True).artifact()
        except Exception as exc:  # noqa: BLE001 - the assertion IS the message
            self.fail(f"doctor did not produce an artifact: {exc!r}")

    def test_worker_inside_leased_worktree_classifies_two(self):
        """RED: today doctor says nothing when a worker drives a leased worktree."""
        leased = self.fixture.base / "lease-ws"
        leased.mkdir(parents=True)
        self.plant_lease(leased)
        self.plant_worker(leased)
        artifact, _rc = self.artifact()
        rows = self.rows(artifact)
        self.assertEqual(1, len(rows), "the in-seat worker must be one typed finding")
        self.assertEqual(2, rows[0]["remote_control_workers"]["classification"])
        self.assertIn(str(leased), rows[0]["detail"])

    def test_workers_outside_leases_are_counted_never_named(self):
        foreign = self.fixture.base / "foreign-dir"
        foreign.mkdir(parents=True)
        leased = self.fixture.base / "another-lease-ws"
        leased.mkdir(parents=True)
        self.plant_lease(leased)
        self.plant_worker(foreign)
        artifact, _rc = self.artifact()
        rows = self.rows(artifact)
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["remote_control_workers"]["classification"])
        self.assertNotIn("foreign-dir", json.dumps(artifact))

    def test_no_workers_stays_silent(self):
        artifact, _rc = self.artifact()
        self.assertEqual([], self.rows(artifact))

    def test_sibling_prefix_does_not_classify_in_seat(self):
        leased = self.fixture.base / "lease-ws"
        leased.mkdir(parents=True)
        sibling = self.fixture.base / "lease-ws-suffix"
        sibling.mkdir(parents=True)
        self.plant_lease(leased)
        self.plant_worker(sibling)
        artifact, _rc = self.artifact()
        rows = self.rows(artifact)
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["remote_control_workers"]["classification"])
        self.assertNotIn(str(sibling), json.dumps(artifact))


if __name__ == "__main__":
    unittest.main()


class RemoteControlCensusUnitTests(unittest.TestCase):
    """Injected-source controls: no live ps, deterministic rows."""

    def setUp(self):
        self.fixture = fixtures.DoctorContractTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def staged(self, *commands):
        class StagedSource:
            name = "staged"

            def snapshot(self, commands=commands):
                return [{"pid": index, "ppid": 1, "command": command}
                        for index, command in enumerate(commands, start=100)]
        return StagedSource()

    def plant_lease(self, workspace: Path) -> None:
        record = {
            "schema_version": 0,
            "id": "lease-" + uuid7_hex(),
            "tenant_id": self.fixture.root.tenant_id,
            "timestamp": "2026-10-11T22:00:00.000Z",
            "kind": "node_lease",
            "node_id": "builder" + uuid7_hex()[:8],
            "workspace": str(workspace),
            "expires_at": "2027-10-11T22:00:00.000Z",
            "state": "active",
        }

        def decide(records):
            return record, record

        transact(
            self.fixture.root,
            Path("registry/entries.jsonl"),
            decide,
            allowed_kinds=set(REGISTRY_KINDS),
        )

    def test_injected_worker_inside_lease_classifies_two(self):
        from floati.cursor_workers import project_remote_control_worker_findings

        leased = self.fixture.base / "unit-lease-ws"
        leased.mkdir(parents=True)
        self.plant_lease(leased)
        findings = project_remote_control_worker_findings(
            self.fixture.root,
            source=self.staged(f"/usr/bin/cursor-agent worker start --worker-dir {leased}"),
        )
        self.assertEqual(1, len(findings))
        self.assertEqual(2, findings[0]["remote_control_workers"]["classification"])
        self.assertEqual(leased.as_posix(), Path(
            findings[0]["remote_control_workers"]["seats"][0]["workspace"]).as_posix())

    def test_injected_foreign_workers_counted_never_named(self):
        from floati.cursor_workers import project_remote_control_worker_findings

        leased = self.fixture.base / "unit-lease-ws"
        leased.mkdir(parents=True)
        self.plant_lease(leased)
        findings = project_remote_control_worker_findings(
            self.fixture.root,
            source=self.staged(
                "/usr/bin/cursor-agent worker start --worker-dir /somewhere/hidden-a",
                "/usr/bin/cursor-agent worker start --worker-dir /somewhere/hidden-b",
            ),
        )
        self.assertEqual(1, len(findings))
        self.assertEqual(1, findings[0]["remote_control_workers"]["classification"])
        self.assertEqual(2, findings[0]["remote_control_workers"]["workers"])
        self.assertNotIn("hidden-a", json.dumps(findings))
        self.assertNotIn("hidden-b", json.dumps(findings))

    def test_no_workers_is_empty(self):
        from floati.cursor_workers import project_remote_control_worker_findings

        self.assertEqual(
            [], project_remote_control_worker_findings(self.fixture.root,
                                                       source=self.staged()))

    def test_counted_only_native_doctor_artifact_satisfies_its_schema(self):
        foreign = self.fixture.base / "schema-foreign-counted"
        with patch("floati.cursor_workers.DEFAULT_SOURCE", self.staged(
            f"/usr/bin/cursor-agent worker start --worker-dir {foreign}",
        )):
            artifact, _status = self.fixture.doctor(no_sandbox=True).artifact()
        findings = [row for row in artifact["findings"] if row["code"] == FINDING_CODE]
        self.assertEqual(1, len(findings))
        self.assertEqual({"classification": 1, "workers": 1, "in_seat": 0},
                         findings[0]["remote_control_workers"])
        self.assertEqual("info", findings[0]["severity"])
        self.assertNotIn(str(foreign), json.dumps(artifact))
        validate_json_schema(artifact, Path("schemas/v1/doctor-artifact.schema.json"))

    def test_in_seat_native_doctor_artifact_satisfies_its_schema(self):
        leased = self.fixture.base / "schema-leased-workspace"
        leased.mkdir()
        self.plant_lease(leased)
        foreign = self.fixture.base / "schema-foreign-unreported"
        with patch("floati.cursor_workers.DEFAULT_SOURCE", self.staged(
            f"/usr/bin/cursor-agent worker start --worker-dir {leased}",
            f"/usr/bin/cursor-agent worker start --worker-dir {foreign}",
        )):
            artifact, _status = self.fixture.doctor(no_sandbox=True).artifact()
        findings = [row for row in artifact["findings"] if row["code"] == FINDING_CODE]
        self.assertEqual(1, len(findings))
        self.assertEqual(
            {"classification": 2, "workers": 2, "in_seat": 1,
             "seats": [{"workspace": str(leased), "pid": 100}], "foreign_workers": 1},
            findings[0]["remote_control_workers"],
        )
        self.assertEqual("warning", findings[0]["severity"])
        self.assertNotIn(str(foreign), json.dumps(artifact))
        validate_json_schema(artifact, Path("schemas/v1/doctor-artifact.schema.json"))

    def _native_worker_artifact(self, *, in_seat=False):
        """Keep mutation controls anchored to the native Doctor artifact shape."""
        workspace = self.fixture.base / "schema-mutation-worker"
        workspace.mkdir()
        if in_seat:
            self.plant_lease(workspace)
        with patch("floati.cursor_workers.DEFAULT_SOURCE", self.staged(
            f"/usr/bin/cursor-agent worker start --worker-dir {workspace}",
        )):
            artifact, _status = self.fixture.doctor(no_sandbox=True).artifact()
        rows = [row for row in artifact["findings"] if row["code"] == FINDING_CODE]
        self.assertEqual(1, len(rows))
        self.assertEqual(2 if in_seat else 1,
                         rows[0]["remote_control_workers"]["classification"])
        return artifact

    _MISSING = object()

    def _reject_finding_mutations(self, artifact, mutations):
        schema = Path("schemas/v1/doctor-artifact.schema.json")
        # A broken native baseline must not make all malformed variants appear
        # correctly rejected merely because an unrelated field is inadmissible.
        validate_json_schema(artifact, schema)
        index = next(index for index, row in enumerate(artifact["findings"])
                     if row["code"] == FINDING_CODE)
        for path, value in mutations:
            with self.subTest(
                classification=artifact["findings"][index]["remote_control_workers"]["classification"],
                path=path,
                **({"value_kind": "missing"} if value is self._MISSING else
                   {"value_kind": "literal", "value": value}),
            ):
                changed = deepcopy(artifact)
                target = changed["findings"][index]
                for key in path[:-1]:
                    target = target[key]
                if value is self._MISSING:
                    target.pop(path[-1])
                else:
                    target[path[-1]] = value
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(changed, schema)

    def test_schema_rejects_missing_or_open_worker_metadata(self):
        artifact = self._native_worker_artifact()
        field = "remote_control_workers"
        self._reject_finding_mutations(artifact, [
            ((field,), self._MISSING), ((field,), None), ((field,), []), ((field,), {}),
            ((field, "classification"), self._MISSING),
            ((field, "workers"), self._MISSING), ((field, "in_seat"), self._MISSING),
            ((field, "extra"), "unapproved metadata"),
            ((field, "seats"), [{"workspace": "/foreign/private", "pid": 100}]),
            ((field, "foreign_workers"), 1),
            (("unapproved_finding_field",), "unapproved finding metadata"),
        ])
        # Reuse the same owned workspace for the second native observation.
        workspace = self.fixture.base / "schema-mutation-worker"
        self.plant_lease(workspace)
        with patch("floati.cursor_workers.DEFAULT_SOURCE", self.staged(
            f"/usr/bin/cursor-agent worker start --worker-dir {workspace}",
        )):
            seated, _status = self.fixture.doctor(no_sandbox=True).artifact()
        self._reject_finding_mutations(seated, [
            ((field, key), self._MISSING)
            for key in ("classification", "workers", "in_seat", "seats", "foreign_workers")
        ] + [((field, "extra"), "unapproved metadata")])

    def test_schema_binds_worker_metadata_to_exact_code_and_subject(self):
        self._reject_finding_mutations(self._native_worker_artifact(), [
            (("code",), "ledger_lock_timeout"), (("code",), self._MISSING),
            (("subject",), "another-host"), (("subject",), self._MISSING),
        ])

    def test_schema_rejects_invalid_classification_and_worker_counts(self):
        field = "remote_control_workers"
        self._reject_finding_mutations(self._native_worker_artifact(), [
            ((field, "classification"), value) for value in (0, 3, True, "1")
        ] + [
            ((field, "workers"), value) for value in (0, -1, True, 1.5)
        ] + [
            ((field, "in_seat"), value) for value in (1, -1, True)
        ])

    def test_schema_rejects_invalid_in_seat_counts_and_seat_coordinates(self):
        field = "remote_control_workers"
        self._reject_finding_mutations(self._native_worker_artifact(in_seat=True), [
            ((field, "workers"), value) for value in (0, -1, True, 1.5)
        ] + [
            ((field, "in_seat"), value) for value in (0, -1, True, 1.5)
        ] + [
            ((field, "foreign_workers"), value) for value in (-1, True, 1.5)
        ] + [
            ((field, "seats"), value) for value in ([], None, [None], [{}])
        ] + [
            ((field, "seats", 0, "workspace"), value)
            for value in (self._MISSING, "", "relative/workspace", None)
        ] + [
            ((field, "seats", 0, "pid"), value)
            for value in (self._MISSING, 0, -1, True, 1.5, "100", None)
        ] + [((field, "seats", 0, "extra"), "unapproved coordinate")])

    def test_schema_rejects_counted_only_severity_and_remediation_mismatches(self):
        self._reject_finding_mutations(self._native_worker_artifact(), [
            (("severity",), value) for value in ("warning", "error", self._MISSING)
        ] + [
            (("remediation",), value)
            for value in ("change a foreign worker", "", {}, self._MISSING)
        ])

    def test_schema_rejects_in_seat_severity_and_remediation_mismatches(self):
        self._reject_finding_mutations(self._native_worker_artifact(in_seat=True), [
            (("severity",), value) for value in ("info", "error", self._MISSING)
        ] + [
            (("remediation",), value) for value in (None, "", {}, self._MISSING)
        ])

    def test_native_artifact_without_worker_finding_still_satisfies_schema(self):
        with patch("floati.cursor_workers.DEFAULT_SOURCE", self.staged()):
            artifact, _status = self.fixture.doctor(no_sandbox=True).artifact()
        self.assertFalse(any(row["code"] == FINDING_CODE for row in artifact["findings"]))
        validate_json_schema(artifact, Path("schemas/v1/doctor-artifact.schema.json"))

    def test_legacy_artifacts_without_worker_metadata_keep_their_acceptance(self):
        # The pre-extension v1 shape has no worker metadata or requirement.
        artifact = {
            "schema_version": 1, "diagnostic_version": "0", "state": "degraded",
            "root": str(self.fixture.root.path), "source": str(self.fixture.base),
            "ref": "main", "unrecognized_kinds": [],
            "fleet_update": {"state": "absent", "actors": []},
            "findings": [{"code": "ledger_lock_timeout", "severity": "error",
                          "subject": "root", "detail": "The declared ledger lock timed out.",
                          "remediation": None}],
        }
        validate_json_schema(artifact, Path("schemas/v1/doctor-artifact.schema.json"))
        historical = json.loads(Path("tests/fixtures/doctor/v0/healthy.json").read_text(
            encoding="utf-8"))
        validate_json_schema(historical, Path("schemas/v0/doctor-artifact.schema.json"))
