"""WD-3 · the #15 mechanism (fq-6-2026-09-08): dead holder, typed refusal.

Deciding question (constructed clean-room, asked first): on a root where the
only thing keeping delivered-but-unacknowledged mail out of the daemon's hands
is a participation claim whose holder process no longer exists, does the FIRST
evaluation after that death classify the holder released and the held work
wakeable again; while a claim whose holder still lives (same pid AND same
process start) keeps the held backpressure exactly as today; and does the
codex wake surface, bound to a thread whose holder is gone, return the typed
refusal ``wake_target_thread_dead`` without ever spawning ``codex queue``, so
no ``queued`` receipt can exist?

Liveness is judged by pid AND process start time, never by name. Every zero
has a control that makes it non-zero: a live holder holds; a legacy claim
with no holder testimony holds; absent or foreign testimony queues exactly
as today. The fixtures measure holder start times through ``/bin/ps`` as an
independent witness rather than importing the product instrument they judge.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids
from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


def _ps_start_epoch(pid: int) -> float:
    """Independent ground-truth witness: the process start `/bin/ps` reports."""

    out = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not out:
        raise AssertionError(f"/bin/ps reports no start time for pid {pid}")
    return datetime.strptime(out, "%a %b %d %H:%M:%S %Y").timestamp()


def _write_holder_testimony(
    root: FloatiRoot, node_id: str, session_id: str, *, pid: int, start_epoch: float,
) -> Path:
    """Write the closed holder-testimony shape without the product's writer.

    The tests own these bytes: whatever the product reads must accept the
    shape this helper writes, or the compatibility contract fails loudly.
    """

    relative = Path("state/codex-wait") / node_id / "holder.json"
    path = root.resolve_relative(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema_version": 1,
        "kind": "codex_wait_holder_testimony",
        "tenant_id": root.tenant_id,
        "node_id": node_id,
        "session_id": session_id,
        "pid": int(pid),
        "process_start_epoch": float(start_epoch),
        "timestamp": "2026-09-10T21:00:00.000Z",
    }
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _persist_armed_claim(root: FloatiRoot, node_id: str, session_id: str) -> dict:
    """Persist one legal armed participation claim row for the fixture world."""

    from floati.framing import encode_frame
    from floati.records import validate_record

    row = {
        "schema_version": 1,
        "id": "codex-wait-session-018f7e9b3c117abc8def0123456789ab",
        "tenant_id": root.tenant_id,
        "timestamp": "2026-09-10T21:00:00.000Z",
        "kind": "codex_wait_session_receipt",
        "node_id": node_id,
        "workspace": "\x2ftmp/wd3-fixture-workspace",
        "workspace_map_digest": "a" * 64,
        "acting_session_id": session_id,
        "operation": "claim",
        "state": "armed",
        "predecessor_receipt_id": None,
        "consent_receipt_id": "codex-wait-consent-018f7e9b3c117abc8def0123456789ab",
        "idempotency_key": "wd-3-fixture-claim",
    }
    validate_record(row, root.tenant_id, {"codex_wait_session_receipt"}, integrity=False)
    relative = Path("receipts/codex-wait-session") / f"{node_id}.jsonl"
    path = root.resolve_relative(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(encode_frame(row))
    return row


class _Runner:
    """Spy runner: records every argv it is handed and never spawns."""

    def __init__(self, *, returncode: int = 0, stdout: str = "ok\n") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append((argv, cwd, timeout))
        from subprocess import CompletedProcess

        return CompletedProcess(argv, self.returncode, self.stdout, "")


class _HolderWorld:
    """One seeded hold world: registered seat, one envelope, one claim."""

    def __init__(self, testcase: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        testcase.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open(Path(temporary.name) / "fleet-alpha", "alpha")
        registry = Registry(self.root)
        registry.register(public_ids.worker("alpha"), "worker")
        registry.register("bob", "worker")
        from floati.events import EventLog

        self.item_id = EventLog(self.root, registry).send(
            public_ids.worker("alpha"), "bob", "floati", "a" * 40,
            "docs/evidence/wd-3.md", "wd-3 held mail",
            idempotency_key="wd-3-envelope",
        )["id"]
        self.session = "thread-fq6"

    def present(self) -> dict:
        """The lawful first presentation: the waiter picks the mail up."""

        from floati.wake_hold import WakeHoldController

        artifact = WakeHoldController(self.root).evaluate(
            "bob", idempotency_key="wd-3-present"
        )
        assert artifact["state"] == "fresh_work", artifact
        return artifact

    def evaluate(self, key: str) -> dict:
        from floati.wake_hold import WakeHoldController

        return WakeHoldController(self.root).evaluate("bob", idempotency_key=key)


class DeadHolderReleaseTests(unittest.TestCase):
    """RED (a): a dead holder releases within one evaluation."""

    def test_dead_holder_releases_held_work_within_one_evaluation(self) -> None:
        world = _HolderWorld(self)
        world.present()
        _persist_armed_claim(world.root, "bob", world.session)
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _write_holder_testimony(
                world.root, "bob", world.session,
                pid=child.pid, start_epoch=_ps_start_epoch(child.pid),
            )
        finally:
            child.kill()
            child.wait()
        held = world.evaluate("wd-3-after-death")
        self.assertEqual("fresh_work", held["state"], held)

    def test_live_holder_keeps_the_held_backpressure(self) -> None:
        """Control for the zero: the release must not loosen a live hold."""

        world = _HolderWorld(self)
        world.present()
        _persist_armed_claim(world.root, "bob", world.session)
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _write_holder_testimony(
                world.root, "bob", world.session,
                pid=child.pid, start_epoch=_ps_start_epoch(child.pid),
            )
            held = world.evaluate("wd-3-live-holder")
        finally:
            child.kill()
            child.wait()
        self.assertEqual("held_only", held["state"], held)

    def test_pid_reuse_is_not_liveness(self) -> None:
        """Liveness by pid AND start-time: a matching pid with the wrong
        process start is a dead holder wearing its pid."""

        world = _HolderWorld(self)
        world.present()
        _persist_armed_claim(world.root, "bob", world.session)
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _write_holder_testimony(
                world.root, "bob", world.session,
                pid=child.pid, start_epoch=_ps_start_epoch(child.pid) - 3600.0,
            )
            released = world.evaluate("wd-3-pid-reuse")
        finally:
            child.kill()
            child.wait()
        self.assertEqual("fresh_work", released["state"], released)

    def test_legacy_claim_without_testimony_stays_held(self) -> None:
        """Control: release requires proof; a claim with no holder testimony
        keeps the conservative held backpressure."""

        world = _HolderWorld(self)
        world.present()
        _persist_armed_claim(world.root, "bob", world.session)
        held = world.evaluate("wd-3-legacy")
        self.assertEqual("held_only", held["state"], held)

    def test_malformed_testimony_stays_held(self) -> None:
        """Control: unparseable testimony proves nothing, so it never releases."""

        world = _HolderWorld(self)
        world.present()
        _persist_armed_claim(world.root, "bob", world.session)
        path = world.root.resolve_relative(
            Path("state/codex-wait") / "bob" / "holder.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        held = world.evaluate("wd-3-malformed")
        self.assertEqual("held_only", held["state"], held)


class HolderInstrumentTests(unittest.TestCase):
    """The pid AND start-time instrument itself, against ground truth.

    Platform-parameterised with no skip (WD-3 Am.2): every test here runs
    on every CI leg, exercising the road that host actually has -- the
    proc_pidinfo proc_bsdinfo layout on darwin, /proc/<pid>/stat plus the
    /proc/stat btime line on Linux."""

    def test_start_epoch_matches_ps_ground_truth(self) -> None:
        from floati.codex_wait_liveness import process_start_epoch

        child = subprocess.Popen(["/bin/sleep", "5"])
        try:
            measured = process_start_epoch(child.pid)
            truth = _ps_start_epoch(child.pid)
        finally:
            child.kill()
            child.wait()
        self.assertIsInstance(measured, float)
        self.assertLess(abs(measured - truth), 1.0)

    def test_own_pid_is_measurable_and_self_consistent(self) -> None:
        """No skip (WD-3 Am.2): the host's own road must measure one live
        pid and return the identical start time on a second read."""

        from floati.codex_wait_liveness import process_start_epoch

        mine = process_start_epoch(os.getpid())
        again = process_start_epoch(os.getpid())
        self.assertIsInstance(mine, float)
        self.assertEqual(mine, again)

    def test_dead_pid_reads_absent(self) -> None:
        from floati.codex_wait_liveness import process_start_epoch

        child = subprocess.Popen(["/bin/sleep", "5"])
        child.kill()
        child.wait()
        self.assertIsNone(process_start_epoch(child.pid))


class DeadThreadRefusalTests(unittest.TestCase):
    """RED (b): the codex wake surface refuses a dead thread typed, and
    never spawns `codex queue` into it, so no `queued` receipt can exist."""

    def _world(self) -> tuple:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        root = FloatiRoot.open_direct_home(base / "fleet-alpha", create=True)
        node = public_ids.builder("a")
        Registry(root).register(node, "codex")
        workspace = base / "workspace"
        workspace.mkdir()
        target = base / "codex-target"
        target.write_bytes(b"#!/bin/sh\nexit 0\n")
        target.chmod(0o700)
        link = base / "codex"
        link.symlink_to(target)
        from floati import wake_daemon_adapters as adapters
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate

        prior_executable = adapters.CODEX_EXECUTABLE
        adapters.CODEX_EXECUTABLE = link
        self.addCleanup(setattr, adapters, "CODEX_EXECUTABLE", prior_executable)
        coordinate = DaemonCoordinate(root, node, "codex")
        binding = AdapterBindingStore(root).write(
            coordinate,
            session_id="thread-fq6",
            workspace=workspace,
            executable=link.resolve(strict=True),
            adapter_version=adapters._ADAPTER_VERSIONS["codex"],
            adapter_digest=adapters.adapter_contract_digest("codex"),
            binding_epoch=time.time_ns(),
        )
        runner = _Runner()
        adapter = adapters.wake_adapter_for(root, node, "codex", runner=runner)
        return adapters, adapter, binding, runner, root, node, workspace

    def test_dead_thread_refuses_typed_without_spawning(self) -> None:
        adapters, adapter, binding, runner, root, node, _ = self._world()
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _write_holder_testimony(
                root, node, "thread-fq6",
                pid=child.pid, start_epoch=_ps_start_epoch(child.pid),
            )
        finally:
            child.kill()
            child.wait()
        result = adapter.request_wake(binding, "[floati] wd-3 wake", 60)
        self.assertEqual("refused", result.outcome, result)
        self.assertEqual("wake_target_thread_dead", result.reason_code, result)
        self.assertEqual([], runner.calls, "a dead thread must never reach codex queue")

    def test_pid_reuse_refuses_typed(self) -> None:
        adapters, adapter, binding, runner, root, node, _ = self._world()
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _write_holder_testimony(
                root, node, "thread-fq6",
                pid=child.pid, start_epoch=_ps_start_epoch(child.pid) - 3600.0,
            )
            result = adapter.request_wake(binding, "[floati] wd-3 wake", 60)
        finally:
            child.kill()
            child.wait()
        self.assertEqual("refused", result.outcome, result)
        self.assertEqual("wake_target_thread_dead", result.reason_code, result)
        self.assertEqual([], runner.calls)

    def test_live_thread_still_queues_with_the_pinned_argv(self) -> None:
        """Control: a provably live holder queues exactly as today."""

        adapters, adapter, binding, runner, root, node, workspace = self._world()
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _write_holder_testimony(
                root, node, "thread-fq6",
                pid=child.pid, start_epoch=_ps_start_epoch(child.pid),
            )
            result = adapter.request_wake(binding, "[floati] wd-3 wake", 60)
        finally:
            child.kill()
            child.wait()
        self.assertEqual("queued", result.outcome, result)
        self.assertIsNone(result.reason_code, result)
        self.assertEqual(1, len(runner.calls), runner.calls)
        argv, cwd, _timeout = runner.calls[0]
        self.assertEqual(
            (
                str(adapters.CODEX_EXECUTABLE), "queue", "--thread", "thread-fq6",
                "--message", "[floati] wd-3 wake",
            ),
            argv, argv,
        )
        self.assertEqual(workspace, cwd)

    def test_absent_testimony_queues_exactly_as_today(self) -> None:
        """Control: no testimony proves nothing either way; today's behavior."""

        adapters, adapter, binding, runner, root, node, _ = self._world()
        result = adapter.request_wake(binding, "[floati] wd-3 wake", 60)
        self.assertEqual("queued", result.outcome, result)
        self.assertEqual(1, len(runner.calls))

    def test_testimony_about_another_session_never_refuses_this_thread(self) -> None:
        """Control: the refusal fires only on testimony naming THIS thread."""

        adapters, adapter, binding, runner, root, node, _ = self._world()
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _write_holder_testimony(
                root, node, "thread-other",
                pid=child.pid, start_epoch=_ps_start_epoch(child.pid),
            )
        finally:
            child.kill()
            child.wait()
        result = adapter.request_wake(binding, "[floati] wd-3 wake", 60)
        self.assertEqual("queued", result.outcome, result)
        self.assertEqual(1, len(runner.calls))


if __name__ == "__main__":
    unittest.main()
