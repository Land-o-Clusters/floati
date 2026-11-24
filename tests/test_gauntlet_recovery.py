from __future__ import annotations

from floati import fixture_ids as public_ids

import errno
import io
import json
import os
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
from tests.test_process_atomicity import assert_send_companions
from tests.readiness import communicate, wait_process, wait_readable
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
        "decided_by": public_ids.reviewer(),
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
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        EventLog(root).send(
            public_ids.worker('alpha'),
            "bob",
            "floati",
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
                        public_ids.worker('alpha'),
                        "bob",
                        "floati",
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
            physical = read_records(root, "events.jsonl",
                allowed_kinds={"message_envelope", "send_readiness_observation"})
            self.assertEqual(2, len(physical))
            self.assertEqual(1, len(assert_send_companions(self, physical)))

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
                    public_ids.worker('alpha'),
                    "bob",
                    "floati",
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
                    # Keep the fixture live until the deletion event. A
                    # delayed parent must not miss a three-tick lifetime.
                ],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    "PATH": str(scan_root),
                    "PYTHONPYCACHEPREFIX": "\x2ftmp/floati-hm3h-recovery-pycache",
                },
            )
            try:
                self.assertIsNotNone(process.stdout)
                wait_readable(process.stdout, "watch did not emit its initial frame")
                first = process.stdout.readline()
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
                stdout, stderr = communicate(process, "watch termination after root deletion")
            finally:
                if process.poll() is None:
                    process.terminate()
                    wait_process(process, "owned recovery watch cleanup")
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

            output = first + stdout + stderr
            self.assertEqual(35, process.returncode, output.decode("utf-8", "replace"))
            self.assertIn(b'"code":"root_deleted"', stdout)
            self.assertEqual(b"", stderr)
            self.assertNotIn(b"Traceback", output)

    def test_status_root_deleted_after_resolution_exits_typed_without_traceback(self) -> None:
        from floati import cli

        for flags in ([], ["--json"]):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                root = self.seeded_root(directory)
                resolve = cli.resolve_command_scope

                def delete_after_resolution(*args, **kwargs):
                    resolved = resolve(*args, **kwargs)
                    shutil.rmtree(root.path)
                    return resolved

                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(cli, "resolve_command_scope", delete_after_resolution), (
                    mock.patch("sys.stdout", stdout)
                ), mock.patch("sys.stderr", stderr):
                    exit_code = cli.main(["status", "--root", str(root.path), *flags])

                artifact = json.loads(stdout.getvalue())
                self.assertEqual(35, exit_code, artifact)
                self.assertEqual("degraded", artifact["status"])
                self.assertEqual("root_deleted", artifact["evidence"]["code"])
                self.assertEqual("", stderr.getvalue())
                self.assertNotIn("Traceback", stdout.getvalue())
                self.assertFalse(root.path.exists())

    def test_guarded_readers_report_deleted_root_without_recreating_it(self) -> None:
        from floati import cut_prefix, mail_summary, registry_prefix, send_prefix

        with tempfile.TemporaryDirectory() as directory:
            root = self.seeded_root(directory)
            send_prefix.warm(root)
            observation = mail_summary.observe(root)
            self.assertIsNotNone(observation)
            shutil.rmtree(root.path)
            readers = {
                "cut warm": lambda: cut_prefix.warm(root),
                "certified event source": lambda: cut_prefix.certified_source(root, "bob"),
                "retained mail observation": observation.current,
                "fresh mail observation": lambda: mail_summary.observe(root),
                "registry cut": lambda: registry_prefix.read_cut(root),
                "archive head": lambda: send_prefix.current_epoch_has_no_roll_head(root),
                "optimistic event references": lambda: send_prefix.event_references(
                    root, [], optimistic=True),
            }
            for reader, query in readers.items():
                with self.subTest(reader=reader):
                    with self.assertRaises(FloatiError) as caught:
                        query()
                    self.assertEqual("root_deleted", caught.exception.code, reader)
                    self.assertFalse(root.path.exists(), reader)

    def test_existing_root_epoch_open_failure_stays_a_lock_failure(self) -> None:
        from floati.bus_epoch import epoch_guard

        with tempfile.TemporaryDirectory() as directory:
            root = self.seeded_root(directory)
            with mock.patch("floati.bus_epoch.os.open", side_effect=PermissionError(errno.EACCES, "denied")):
                with self.assertRaises(FloatiError) as caught, epoch_guard(root):
                    self.fail("unavailable lock admitted a reader")
            self.assertEqual("epoch_lock_unavailable", caught.exception.code)
            self.assertTrue(root.path.is_dir())

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
                with self.subTest(trace_family=trace.root.path.relative_to(base.resolve(strict=False)).as_posix()):
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
