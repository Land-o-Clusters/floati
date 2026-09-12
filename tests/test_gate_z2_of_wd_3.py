"""GATE of the WD-3 row, the #15 mechanism, at gated tip d8bdfae3 - clean room.

The deciding questions, constructed by the gate from the dispatch contract,
not from the author's tests:

G1  a participation claim whose holder pid is DEAD classifies released and
    the held work is re-presented within ONE evaluation;
G2  a claim whose holder pid was REUSED (same pid, different measured
    process start) must ALSO release - liveness is pid AND start, never
    pid alone;
G3  the codex wake surface, handed a thread whose recorded holder is
    proven gone, returns the typed refusal wake_target_thread_dead and
    the refusal fires BEFORE the spawn surface is touched;
G4  at the DAEMON, a wake attempt refused wake_target_thread_dead writes
    exactly one refused attempt receipt and NO queued receipt row exists;
G5  controls: a live holder (same pid AND same measured start) holds, and
    a live thread still reaches the queue surface; testimony about a
    different session, or none at all, never refuses this thread.
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

from floati import fixture_ids as public_ids
from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import (
    AdapterBindingStore,
    DaemonCoordinate,
)
from tests.temp_roots import REAL_TEMP_ROOT

try:
    import floati.codex_wait_liveness  # noqa: F401
    _WD3_PRESENT = True
except ImportError:
    # Typed absence: this gate file rides on the gating lane's branch, whose
    # tree does not carry the WD-3 modules. It runs where the WD-3 code
    # exists (the gated tip d8bdfae3, and main after the landing).
    _WD3_PRESENT = False


def _ps_start(pid: int) -> float:
    out = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out, f"/bin/ps reports no start for {pid}"
    return datetime.strptime(out, "%a %b %d %H:%M:%S %Y").timestamp()


def _testimony(root: FloatiRoot, node: str, session: str, *, pid: int, start: float) -> Path:
    path = root.resolve_relative(Path("state/codex-wait") / node / "holder.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "codex_wait_holder_testimony",
                "tenant_id": root.tenant_id,
                "node_id": node,
                "session_id": session,
                "pid": int(pid),
                "process_start_epoch": float(start),
                "timestamp": "2026-09-10T21:00:00.000Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class HoldWorld:
    """One registered seat, one envelope, one armed participation claim."""

    def __init__(self, testcase: unittest.TestCase) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        testcase.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        self.node = "gate-holder"
        Registry(self.root).register(public_ids.worker("alpha"), "worker")
        Registry(self.root).register(self.node, "worker")
        from floati.events import EventLog

        EventLog(self.root, Registry(self.root)).send(
            public_ids.worker("alpha"), self.node, "floati", "a" * 40,
            "docs/evidence/gate-z2-of-wd-3.md", "gate mail",
            idempotency_key="gate-z2-wd3-envelope",
        )
        from floati.framing import encode_frame
        from floati.records import validate_record

        self.session = "thread-gate-1"
        row = {
            "schema_version": 1,
            "id": "codex-wait-session-018f7e9b3c117abc8def0123456789ab",
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-09-10T21:00:00.000Z",
            "kind": "codex_wait_session_receipt",
            "node_id": self.node,
            "workspace": "\x2ftmp/gate-z2-wd3-workspace",
            "workspace_map_digest": "a" * 64,
            "acting_session_id": self.session,
            "operation": "claim",
            "state": "armed",
            "predecessor_receipt_id": None,
            "consent_receipt_id": "codex-wait-consent-018f7e9b3c117abc8def0123456789ab",
            "idempotency_key": "gate-z2-wd3-claim",
        }
        validate_record(row, self.root.tenant_id, {"codex_wait_session_receipt"}, integrity=False)
        claim_path = self.root.resolve_relative(
            Path("receipts/codex-wait-session") / f"{self.node}.jsonl"
        )
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        with claim_path.open("ab") as handle:
            handle.write(encode_frame(row))

    def evaluate(self, key: str) -> dict:
        from floati.wake_hold import WakeHoldController

        return WakeHoldController(self.root).evaluate(self.node, idempotency_key=key)

    def present(self) -> dict:
        first = self.evaluate("gate-z2-wd3-present")
        assert first["state"] == "fresh_work", first
        return first


class CodexSurfaceWorld:
    """One codex-bound coordinate whose queue surface is a spy runner."""

    def __init__(self, testcase: unittest.TestCase, *, executable_exists: bool = True) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        testcase.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(base / "fleet", create=True)
        self.node = public_ids.builder("a")
        Registry(self.root).register(self.node, "codex")
        workspace = base / "workspace"
        workspace.mkdir()
        target = base / "codex-target"
        target.write_bytes(b"#!/bin/sh\nexit 0\n")
        target.chmod(0o700)
        link = base / "codex"
        link.symlink_to(target)
        if not executable_exists:
            link.unlink()
        from floati import wake_daemon_adapters as adapters
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate

        self.adapters = adapters
        prior = adapters.CODEX_EXECUTABLE
        adapters.CODEX_EXECUTABLE = link
        testcase.addCleanup(setattr, adapters, "CODEX_EXECUTABLE", prior)
        coordinate = DaemonCoordinate(self.root, self.node, "codex")
        self.binding = AdapterBindingStore(self.root).write(
            coordinate,
            session_id="thread-gate-2",
            workspace=workspace,
            executable=link.resolve(strict=True) if executable_exists else target,
            adapter_version=adapters._ADAPTER_VERSIONS["codex"],
            adapter_digest=adapters.adapter_contract_digest("codex"),
            binding_epoch=time.time_ns(),
        )
        self.calls: list[tuple] = []

        def runner(argv, cwd, timeout):
            self.calls.append((argv, cwd, timeout))
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")

        self.adapter = adapters.wake_adapter_for(
            self.root, self.node, "codex", runner=runner
        )
        self.session = "thread-gate-2"


@unittest.skipUnless(_WD3_PRESENT, "WD-3 code absent on this branch; "
                  "the gate runs at the gated tip d8bdfae3")
class GateDeadHolderReleasesWithinOneEvaluation(unittest.TestCase):
    def test_g1_dead_holder_releases_held_work_in_one_evaluation(self) -> None:
        world = HoldWorld(self)
        world.present()
        self.assertEqual("held_only", world.evaluate("gate-hold-1")["state"])
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _testimony(
                world.root, world.node, world.session,
                pid=child.pid, start=_ps_start(child.pid),
            )
        finally:
            child.kill()
            child.wait()
        verdict = world.evaluate("gate-after-death")
        self.assertEqual(
            "fresh_work", verdict["state"],
            "one evaluation after proven death must re-present the held work",
        )


@unittest.skipUnless(_WD3_PRESENT, "WD-3 code absent on this branch; "
                  "the gate runs at the gated tip d8bdfae3")
class GatePidReuseIsAlsoRelease(unittest.TestCase):
    def test_g2_reused_pid_with_a_different_start_releases(self) -> None:
        from floati.codex_wait_liveness import classify_holder, read_holder_testimony

        world = HoldWorld(self)
        world.present()
        self.assertEqual("held_only", world.evaluate("gate-hold-2")["state"])
        _testimony(
            world.root, world.node, world.session,
            pid=os.getpid(), start=_ps_start(os.getpid()) + 86400.0,
        )
        testimony = read_holder_testimony(world.root, world.node)
        self.assertEqual(
            "released", classify_holder(testimony),
            "a live pid with a different measured start is a reused pid, not a holder",
        )
        verdict = world.evaluate("gate-after-reuse")
        self.assertEqual("fresh_work", verdict["state"])


@unittest.skipUnless(_WD3_PRESENT, "WD-3 code absent on this branch; "
                  "the gate runs at the gated tip d8bdfae3")
class GateTypedRefusalBeforeTheSpawnSurface(unittest.TestCase):
    def test_g3_dead_thread_refuses_typed_even_when_the_executable_is_absent(
        self,
    ) -> None:
        """The refusal must precede the spawn surface entirely: with the
        recorded holder proven gone, the answer is the typed refusal EVEN
        WHEN the codex executable cannot be resolved - any other answer
        would prove the gate fires after the spawn path."""

        world = CodexSurfaceWorld(self, executable_exists=False)
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _testimony(
                world.root, world.node, world.session,
                pid=child.pid, start=_ps_start(child.pid),
            )
        finally:
            child.kill()
            child.wait()
        result = world.adapter.request_wake(
            world.binding, "[floati] gate wake", 60
        )
        self.assertEqual("refused", result.outcome, result)
        self.assertEqual("wake_target_thread_dead", result.reason_code, result)
        self.assertEqual([], world.calls, "no spawn may be attempted")


@unittest.skipUnless(_WD3_PRESENT, "WD-3 code absent on this branch; "
                  "the gate runs at the gated tip d8bdfae3")
class GateDaemonWritesNoQueuedReceipt(unittest.TestCase):
    def test_g4_daemon_records_refused_never_queued(self) -> None:
        """End to end at the daemon: an adapter refusal of the typed reason
        composes with the attempt ledger - exactly one REFUSED attempt
        receipt, and no queued row can exist for the dead thread."""

        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        root = FloatiRoot.open_direct_home(base / "fleet", create=True)
        node = public_ids.builder("a")
        Registry(root).register(node, "worker")
        Registry(root).register("sender", "architect")
        coordinate = DaemonCoordinate(root, node, "cursor")
        workspace = base / "workspace"
        workspace.mkdir()
        executable = base / "cursor-agent"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        AdapterBindingStore(root).write(
            coordinate,
            session_id="cursor-session-1",
            workspace=workspace,
            executable=executable,
            adapter_version="1",
            adapter_digest=__import__(
                "floati.wake_daemon_adapters", fromlist=["adapter_contract_digest"]
            ).adapter_contract_digest("cursor"),
            binding_epoch=1,
        )
        from floati.wake_daemon_contract import DaemonConsentLedger

        DaemonConsentLedger(root).consent(
            coordinate,
            adapter_version="1",
            adapter_digest=__import__(
                "floati.wake_daemon_adapters", fromlist=["adapter_contract_digest"]
            ).adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key="gate-z2-wd3-consent",
        )
        from floati.events import EventLog

        EventLog(root).send(
            "sender", node, "floati", "a" * 40,
            "docs/evidence/gate-z2-of-wd-3.md", "gate daemon mail",
            worker_session_id="cursor-session-1",
            idempotency_key="gate-z2-wd3-daemon-mail",
        )

        class _RefusingAdapter:
            def __init__(self, bound_coordinate):
                self.coordinate = bound_coordinate

            def exact_binding(self):
                from floati.wake_daemon_adapters import AdapterBinding

                return AdapterBinding.from_record(
                    AdapterBindingStore(root).read(self.coordinate)
                )

            def request_wake(self, binding, reason, deadline_seconds, envelopes=None):
                from floati.wake_daemon_adapters import WakeAdapterResult

                return WakeAdapterResult(
                    "refused", "wake_target_thread_dead", None, None
                )

        from floati.wake_daemon import WakeDaemon
        from floati.jsonl import read_records_snapshot

        daemon = WakeDaemon(coordinate, _RefusingAdapter(coordinate))
        artifact = daemon.run_cycle(100.0)
        self.assertEqual("refused", artifact["state"], artifact)
        self.assertEqual("wake_target_thread_dead", artifact["reason_code"], artifact)
        rows = read_records_snapshot(
            root,
            Path("receipts/wakes") / f"{node}.jsonl",
            allowed_kinds={"wake_attempt_receipt"},
        )
        attempts = [
            row
            for row in rows
            if row.get("kind") == "wake_attempt_receipt"
            and row.get("node_id") == node
        ]
        self.assertEqual(1, len(attempts), attempts)
        self.assertEqual("refused", attempts[0]["outcome"], attempts)
        self.assertEqual("wake_target_thread_dead", attempts[0]["reason_code"])
        self.assertEqual(
            [], [row for row in attempts if row.get("outcome") == "queued"],
            "no queued receipt may exist for a dead thread",
        )


@unittest.skipUnless(_WD3_PRESENT, "WD-3 code absent on this branch; "
                  "the gate runs at the gated tip d8bdfae3")
class GateLiveHolderControls(unittest.TestCase):
    def test_g5_live_holder_holds_and_a_live_thread_still_reaches_the_queue(
        self,
    ) -> None:
        world = HoldWorld(self)
        world.present()
        _testimony(
            world.root, world.node, world.session,
            pid=os.getpid(), start=_ps_start(os.getpid()),
        )
        self.assertEqual(
            "held_only", world.evaluate("gate-live-holder")["state"],
            "a live holder (same pid AND same start) keeps the hold",
        )

        surface = CodexSurfaceWorld(self)
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _testimony(
                surface.root, surface.node, surface.session,
                pid=child.pid, start=_ps_start(child.pid),
            )
            result = surface.adapter.request_wake(
                surface.binding, "[floati] gate wake", 60
            )
        finally:
            child.kill()
            child.wait()
        self.assertEqual("queued", result.outcome, result)
        self.assertEqual(1, len(surface.calls), "the live thread queues as today")

    def test_g5_testimony_about_another_session_never_refuses(self) -> None:
        surface = CodexSurfaceWorld(self)
        child = subprocess.Popen(["/bin/sleep", "30"])
        try:
            _testimony(
                surface.root, surface.node, "thread-other",
                pid=child.pid, start=_ps_start(child.pid),
            )
            result = surface.adapter.request_wake(
                surface.binding, "[floati] gate wake", 60
            )
        finally:
            child.kill()
            child.wait()
        self.assertEqual("queued", result.outcome, result)
        self.assertEqual(1, len(surface.calls), "the other session's death never gates this thread")


if __name__ == "__main__":
    unittest.main()
