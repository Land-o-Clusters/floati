"""F2 native Codex controls: separate scratch Stop processes and native readiness.

The fault child obstructs only its owned holder temporary file. It calls the
normal native entry point without mocking authority, delivery or liveness.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unittest

from floati.codex_wait_contract import resolve_participant
from floati.codex_wait_liveness import holder_testimony_path
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.records import WAKE_HOLD_KINDS
from floati.stop_hold import StopHoldLedger
from tests import test_codex_wait as baseline


SESSION = "thread-native-codex-unacked-loop"
REPOSITORY = Path(__file__).resolve().parents[1]


def _native_hook(bus_home, workspace, ready_fd, gate_fd):
    """Keep native pipes/processes, spending consent only at actual readiness.

    This fixture tests conversation turnover and publication, not Python
    startup or projection speed. An empty native wait spends its requested
    interval; native readiness leaves time for the new mail's admission.
    """
    from floati.codex_wait import run_stop_waiter, native_wait_readable

    clock = [0.0]
    announced = False

    def wait_readable(source, seconds):
        nonlocal announced
        if not announced:
            announced = True
            os.write(ready_fd, b"1")
            # Parent inspection/append happens while the admitted hold is
            # live, before its quiet interval starts. Collection releases it.
            if os.read(gate_fd, 1) != b"1":
                raise RuntimeError("scratch parent did not release native readiness")
        ready = native_wait_readable(source, seconds)
        if not ready:
            clock[0] += seconds
        return ready

    def sleep(seconds):
        time.sleep(seconds)
        clock[0] += seconds

    payload = json.loads(sys.stdin.read())
    return run_stop_waiter(bus_home=Path(bus_home), hook_payload=payload,
        stdout=sys.stdout, stderr=sys.stderr, monotonic=lambda: clock[0],
        wait_readable=wait_readable, sleep=sleep)


def _obstructed_hook(bus_home, workspace, ready_fd, gate_fd):
    """Create a real O_EXCL collision before this child's native startup."""
    participant = resolve_participant(Path(bus_home), Path(workspace))
    if participant is None:
        raise RuntimeError("scratch hook lost its native workspace binding")
    holder = holder_testimony_path(participant.root, participant.binding.node_id)
    obstruction = holder.with_name("." + holder.name + "." + str(os.getpid()) + ".tmp")
    obstruction.write_bytes(b"scratch-owned exclusive-create obstruction\n")
    try:
        return _native_hook(bus_home, workspace, ready_fd, gate_fd)
    finally:
        obstruction.unlink()


class CodexSeparateStopUnackedLoopTests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.arm_codex_role_node("codex-unacked-loop")
        self.node = self.f.participant.binding.node_id
        self.f.arm_session(SESSION)
        self.holder = holder_testimony_path(self.f.root, self.node)

    def rows(self, directory, kinds):
        relative = Path("receipts") / directory / (self.node + ".jsonl")
        if not self.f.root.resolve_relative(relative).exists():
            return []
        return read_records_snapshot(self.f.root, relative, allowed_kinds=set(kinds))

    def deliveries(self):
        return self.rows("deliveries", set(WAKE_HOLD_KINDS))

    def attempts(self):
        return self.rows("wakes", {"wake_attempt_receipt"})

    def send(self, key):
        return EventLog(self.f.root).send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/ping.md", key, idempotency_key=key)

    def assert_no_ack(self):
        self.assertEqual([], self.rows("acks", {"ack_receipt"}))

    def launch(self, *, obstruct=False):
        ready_read, ready_write = os.pipe()
        gate_read, gate_write = os.pipe()
        if obstruct:
            argv = [sys.executable, "-B", "-m", "tests.test_f2_codex_unacked_loop",
                "--scratch-obstructed-hook", str(self.f.bus_home), str(self.f.workspace)]
        else:
            argv = [sys.executable, "-B", "-m", "tests.test_f2_codex_unacked_loop",
                "--scratch-hook", str(self.f.bus_home), str(self.f.workspace)]
        argv.extend([str(ready_write), str(gate_read)])
        try:
            child = subprocess.Popen(argv, cwd=str(REPOSITORY), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                pass_fds=(ready_write, gate_read))
        except BaseException:
            os.close(ready_read)
            os.close(gate_write)
            raise
        finally:
            os.close(ready_write)
            os.close(gate_read)
        child.fixture_ready = os.fdopen(ready_read, "rb", buffering=0)
        child.fixture_gate = os.fdopen(gate_write, "wb", buffering=0)
        self.addCleanup(child.fixture_ready.close)
        def finish():
            self.release(child)
            child.communicate()
        self.addCleanup(finish)
        child.stdin.write(json.dumps({"cwd": str(self.f.workspace), "session_id": SESSION}) + "\n")
        child.stdin.close()
        child.stdin = None
        return child

    def collect(self, child):
        self.release(child)
        output, errors = child.communicate()
        self.assertEqual(0, child.returncode, errors or output)
        return output, errors

    @staticmethod
    def release(child):
        if not child.fixture_gate.closed:
            try:
                child.fixture_gate.write(b"1")
            except BrokenPipeError:
                pass
            finally:
                child.fixture_gate.close()

    def first_publication(self):
        old = self.send("native-codex-unacked-old")
        first = self.launch()
        output, errors = self.collect(first)
        self.assertEqual("block", json.loads(output)["decision"], errors)
        self.assertIn(old["id"], output)
        testimony = json.loads(self.holder.read_bytes())
        self.assertEqual(first.pid, testimony["pid"])
        self.assertEqual(SESSION, testimony["session_id"])
        self.assertGreater(testimony["process_start_epoch"], 0)
        self.assertEqual([old["id"]], self.deliveries()[0]["item_ids"])
        self.assertEqual(["woke"], [row["outcome"] for row in self.attempts()])
        self.assertEqual(SESSION, self.attempts()[0]["acting_session_id"])
        self.assert_no_ack()
        return old, first, testimony, self.deliveries(), self.attempts()

    def await_second_start(self, child):
        ready = child.fixture_ready.read(1)
        if ready != b"1":
            self.fail("second native child exited without reaching its actual readiness: "
                + repr(self.collect(child)))
        starts = [row for row in StopHoldLedger(self.f.root).read(self.node)
            if row["phase"] == "start"]
        self.assertEqual(2, len(starts))
        self.assertEqual(SESSION, starts[-1]["acting_session_id"])
        return starts[-1]

    def hold_control(self, *, obstruct, append_fresh):
        old, first, prior_holder, before, before_attempts = self.first_publication()
        child = self.launch(obstruct=obstruct)
        self.assertNotEqual(first.pid, child.pid)
        start = self.await_second_start(child)
        testimony = json.loads(self.holder.read_bytes())
        if obstruct:
            self.assertEqual(prior_holder, testimony,
                "the physical fault did not preserve the exited predecessor's testimony")
        else:
            self.assertEqual(child.pid, testimony["pid"])
            self.assertEqual(SESSION, testimony["session_id"])
        claim = self.rows("codex-wait-session", {"codex_wait_session_receipt"})
        self.assertEqual(1, len(claim), "Stop process turnover changed durable claim authority")
        self.assertEqual(SESSION, claim[0]["acting_session_id"])
        # The child has admitted held-only mail and is gated at native
        # readiness. Any output now would be a premature republication.
        ready, _, _ = select.select([child.stdout], [], [], 0)
        if ready:
            output, errors = self.collect(child)
            self.assertNotIn(old["id"], output,
                "RED: exited hook PID re-presented old unACKed mail in the same conversation; " + errors)
            self.fail("second native Stop returned before its bounded hold: " + output + errors)
        self.assertEqual(before, self.deliveries())
        self.assertEqual(before_attempts, self.attempts())
        self.assert_no_ack()
        fresh = self.send("native-codex-unacked-new") if append_fresh else None
        output, errors = self.collect(child)
        if obstruct:
            self.assertIn("holder_testimony: FileExistsError", errors)
        if fresh is not None:
            self.assertNotIn(old["id"], output)
            self.assertIn(fresh["id"], output)
            self.assertEqual(before, self.deliveries()[:-1])
            self.assertEqual([fresh["id"]], self.deliveries()[-1]["item_ids"])
            self.assertEqual(before_attempts, self.attempts()[:-1])
        else:
            self.assertIn("deadline exhausted", output, errors)
            self.assertIn("held_unacked", output, errors)
            self.assertIn(old["id"], output, errors)
            self.assertNotIn("Exact native envelopes", output)
            self.assertEqual(before, self.deliveries())
            self.assertEqual(before_attempts, self.attempts())
        exits = [row for row in StopHoldLedger(self.f.root).read(self.node)
            if row["invocation_id"] == start["invocation_id"] and row["phase"] == "exit"]
        self.assertEqual(1, len(exits))
        self.assert_no_ack()

    def test_separate_stop_replaces_dead_holder_and_holds_old_mail(self):
        cases = ((False, False), (False, True), (True, False), (True, True))
        for index, (obstruct, append_fresh) in enumerate(cases):
            with self.subTest(obstruct=obstruct, append_fresh=append_fresh):
                if index:
                    # The previous case left durable holder and delivery receipts.
                    self.doCleanups()
                    self.setUp()
                self.hold_control(obstruct=obstruct, append_fresh=append_fresh)


if __name__ == "__main__":
    if len(sys.argv) == 6 and sys.argv[1] == "--scratch-hook":
        raise SystemExit(_native_hook(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])))
    if len(sys.argv) == 6 and sys.argv[1] == "--scratch-obstructed-hook":
        raise SystemExit(_obstructed_hook(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])))
    unittest.main()
