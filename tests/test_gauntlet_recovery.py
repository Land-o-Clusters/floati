from __future__ import annotations

import errno
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import FloatiError
from floati.decisions import DecisionRegister, decision_digest
from floati.events import EventLog
from floati.jsonl import read_records
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from tests.hm3i_gauntlet_fixtures import (
    axis_coverage_from_traces,
    assert_physical_projection,
    build_full_run_trace_set,
    build_foc_orphan_trace,
    build_retry_stale_trace,
    canonical_observation_from_records,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _decision_proposal(tenant_id: str, suffix: str) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 0,
        "id": f"decision-record-018f7e9b3c1{suffix}7abc8def0123456789ab",
        "tenant_id": tenant_id,
        "timestamp": "2026-08-08T12:00:00.000Z",
        "kind": "decision_record",
        "repository": "owner/repo",
        "decision_id": f"decision-018f7e9b3d1{suffix}7abc8def0123456789ab",
        "scope": {"kind": "repository"},
        "statement": f"Recovery proposal {suffix}.",
        "status": "proposed",
        "author_authority": "worker",
        "source_artifact_ids": ["run:run-018f7e9b3c137abc8def0123456789ab"],
        "task_contract_id": None,
        "decided_by": "fable",
        "supersedes": None,
    }
    record["decision_digest"] = decision_digest(record)
    return record


def _seed_decision_source(root: FloatiRoot) -> None:
    RunLedger(root).append(
        {
            "schema_version": 0,
            "id": "run-created-018f7e9b3c147abc8def0123456789ab",
            "tenant_id": root.tenant_id,
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "run_created",
            "run_id": "run-018f7e9b3c137abc8def0123456789ab",
            "plan_digest": "a" * 64,
            "item_ids": ["work-018f7e9b3c157abc8def0123456789ab"],
            "dependency_edges": [],
        }
    )


class RecoveryGauntletTests(unittest.TestCase):
    def seeded_root(self, directory: str) -> FloatiRoot:
        root = FloatiRoot.open_direct_home(Path(directory) / "recovery", create=True)
        registry = Registry(root)
        registry.register("alice", "worker")
        registry.register("bob", "worker")
        EventLog(root).send(
            "alice",
            "bob",
            "slipway",
            "a" * 40,
            "docs/evidence/HM3H-GAUNTLET.md",
            "baseline",
            idempotency_key="recovery-baseline",
        )
        return root

    def test_disk_full_after_partial_append_rolls_back_and_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.seeded_root(directory)
            path = root.resolve_relative("events.jsonl")
            before = path.read_bytes()
            real_write = os.write

            def partial_enospc(descriptor: int, data: bytes) -> int:
                real_write(descriptor, data[: len(data) // 2])
                raise OSError(errno.ENOSPC, "gauntlet disk full")

            caught: BaseException | None = None
            try:
                with mock.patch("floati.jsonl.os.write", side_effect=partial_enospc):
                    EventLog(root).send(
                        "alice",
                        "bob",
                        "slipway",
                        "b" * 40,
                        "docs/evidence/HM3H-GAUNTLET.md",
                        "disk full",
                        idempotency_key="recovery-disk-full",
                    )
            except BaseException as exc:
                caught = exc

            self.assertIsInstance(caught, FloatiError)
            self.assertEqual("disk_full", caught.code)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual(
                1,
                len(read_records(root, "events.jsonl", allowed_kinds={"message_envelope"})),
            )

    def test_read_only_root_mid_operation_is_typed_without_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.seeded_root(directory)
            path = root.resolve_relative("events.jsonl")
            before = path.read_bytes()
            lock_path = root.resolve_relative("events.jsonl.lock")
            if lock_path.exists():
                lock_path.unlink()
            root.path.chmod(0o500)
            caught: BaseException | None = None
            try:
                EventLog(root).send(
                    "alice",
                    "bob",
                    "slipway",
                    "b" * 40,
                    "docs/evidence/HM3H-GAUNTLET.md",
                    "read only",
                    idempotency_key="recovery-read-only",
                )
            except BaseException as exc:
                caught = exc
            finally:
                root.path.chmod(0o700)

            self.assertIsInstance(caught, FloatiError)
            self.assertEqual("root_read_only", caught.code)
            self.assertEqual(before, path.read_bytes())

    def test_deleted_root_under_live_watch_exits_typed_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.seeded_root(directory)
            base = Path(directory)
            destination = base / "prospective-install"
            scan_root = base / "enumerated-path"
            scan_root.mkdir()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "floati",
                    "watch",
                    "--root",
                    str(root.path),
                    "--destination",
                    str(destination),
                    "--interval",
                    "0.05",
                    "--iterations",
                    "3",
                ],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "PATH": str(scan_root),
                    "PYTHONPYCACHEPREFIX": "/tmp/slipway-hm3h-recovery-pycache",
                },
            )
            try:
                self.assertIsNotNone(process.stderr)
                ready, _, _ = select.select([process.stderr], [], [], 2)
                self.assertTrue(ready, "watch did not emit its initial frame")
                first = process.stderr.readline()
                initial = json.loads(first)
                self.assertEqual(1, initial["schema_version"])
                self.assertEqual("watch", initial["command"])
                self.assertEqual("ok", initial["status"])
                self.assertEqual("initial", initial["evidence"]["delta"]["kind"])
                self.assertEqual(
                    "affirmative_none",
                    initial["evidence"]["delta"]["snapshot"]["installer_shadow"]["outcome"],
                )
                shutil.rmtree(root.path)
                stdout, stderr = process.communicate(timeout=3)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)

            output = first + stdout + stderr
            self.assertEqual(35, process.returncode, output.decode("utf-8", "replace"))
            self.assertIn(b'"code":"root_deleted"', stderr)
            self.assertNotIn(b"Traceback", output)

    def test_decision_disk_full_and_root_disappearance_are_typed_without_partial_projection(self) -> None:
        """Catches a decision proposal that leaves partial evidence after ENOSPC or yields a stale capsule after its root disappears."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "recovery", create=True)
            _seed_decision_source(root)
            register = DecisionRegister(root, "owner/repo")
            first = _decision_proposal("recovery", "1")
            register.append(first)
            path = root.resolve_relative(register.relative_path)
            before = path.read_bytes()
            real_write = os.write

            def partial_enospc(descriptor: int, data: bytes) -> int:
                real_write(descriptor, data[: len(data) // 2])
                raise OSError(errno.ENOSPC, "decision gauntlet disk full")

            with mock.patch("floati.jsonl.os.write", side_effect=partial_enospc):
                with self.assertRaises(FloatiError) as caught:
                    register.append(_decision_proposal("recovery", "2"))
            self.assertEqual("disk_full", caught.exception.code)
            self.assertEqual(before, path.read_bytes())
            self.assertEqual([first], register.records())

            shutil.rmtree(root.path)
            with self.assertRaises(FloatiError) as disappeared:
                register.capsule()
            self.assertEqual("root_deleted", disappeared.exception.code)

    def test_hm3i_all_literal_run_traces_reopen_with_stable_physical_ids_and_observation(self) -> None:
        """Every literal family reopens from physical frames, not clocks or process state."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            traces = build_full_run_trace_set(base)
            self.assertTrue(all(axis_coverage_from_traces("recovery", traces).values()))
            for trace in traces:
                with self.subTest(run_id=trace.run_id):
                    expected = assert_physical_projection(trace)
                    expected_ids = [record["id"] for record in trace.records]
                    reopened = FloatiRoot.open_direct_home(trace.root.path, create=False)
                    recovered = RunLedger(reopened).records()

                    self.assertEqual(expected_ids, [record["id"] for record in recovered])
                    self.assertEqual(
                        expected,
                        canonical_observation_from_records(trace, recovered),
                    )
                    if trace.claim_id is not None:
                        joins = [
                            record
                            for record in recovered
                            if record["kind"] == "supervisor_orphaned"
                        ]
                        self.assertEqual(3, len(joins))
                        self.assertTrue(
                            all(
                                record["claim_id"] == trace.claim_id
                                and record["lease_id"] == trace.lease_id
                                and record["worker_session_id"] == trace.worker_session_id
                                for record in joins
                            )
                        )


if __name__ == "__main__":
    unittest.main()
