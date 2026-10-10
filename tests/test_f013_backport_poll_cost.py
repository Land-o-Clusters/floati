"""F013 RED: idle replay cost, with native next-poll safety controls.

Runtime clocks and sleeps are injected. Projection instrumentation delegates
to the real function and times it with independent real clocks.
The parent owns execution and the RED/green evidence bank.
"""

from __future__ import annotations

import io
import json
import multiprocessing
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from floati import codex_wait, wake_hold
from floati.codex_wait_contract import CodexWaitConsentLedger, consent_ledger_relative
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.wake_control import WakeController
from tests import test_codex_wait as baseline


def _native_holder_child(root, node, session, channel, unused_channel) -> None:
    """Own truthful holder testimony and a bounded, signal-free release path."""
    from floati.codex_wait_liveness import write_holder_testimony

    unused_channel.close()
    try:
        if not channel.poll(10) or channel.recv() != "write":
            return
        channel.send(write_holder_testimony(root, node, session))
        if channel.poll(10):
            channel.recv()
    finally:
        channel.close()


class F013BackportPollCostTests(unittest.TestCase):
    SESSION = "f013-native-waiter"

    def setUp(self) -> None:
        # Compose the canonical fixture without inheriting all of its tests.
        self.runtime = baseline.CodexWaitRuntimeTests(methodName="runTest")
        self.addCleanup(self.runtime.doCleanups)
        self.runtime.setUp()
        self.root = self.runtime.root
        self.node = self.runtime.participant.binding.node_id
        CodexWaitConsentLedger(self.root).arm(
            self.runtime.participant.binding,
            hook_timeout_seconds=25,
            wait_deadline_seconds=20,
            idempotency_key="f013-twenty-tick-consent",
        )
        self.runtime.arm_session(self.SESSION)

    def _inputs(self) -> dict[str, bytes | None]:
        """The durable semantic/authority inputs, excluding waiter testimony."""
        relative = (
            "events.jsonl",
            "receipts/deliveries/" + self.node + ".jsonl",
            "receipts/acks/" + self.node + ".jsonl",
            "receipts/codex-wait-consent/" + self.node + ".jsonl",
            "receipts/codex-wait-session/" + self.node + ".jsonl",
            "codex-wait/workspaces.v0.json",
        )
        return {
            name: self.root.resolve_relative(name).read_bytes()
            if self.root.resolve_relative(name).exists() else None
            for name in relative
        }

    def _hold(self, key: str) -> dict:
        message = self.runtime.send(key)
        messages, delivery = EventLog(self.root).present(self.node)
        self.assertEqual([message["id"]], [row["id"] for row in messages])
        self.assertIsNotNone(delivery)
        return message

    def _run(self, on_first_sleep=None) -> dict:
        clock, sleeps, projections = [0.0], [], []
        real_project = wake_hold.project_wake_items

        def project(*args, **kwargs):
            started_wall, started_cpu = time.perf_counter_ns(), time.process_time_ns()
            try:
                return real_project(*args, **kwargs)
            finally:
                projections.append({
                    "wall_ns": time.perf_counter_ns() - started_wall,
                    "cpu_ns": time.process_time_ns() - started_cpu,
                })

        def sleep(seconds: float) -> None:
            self.assertGreater(seconds, 0)
            clock[0] += seconds
            sleeps.append(seconds)
            self.assertLessEqual(len(sleeps), 20, "waiter exceeded its injected native deadline")
            if len(sleeps) == 1 and on_first_sleep is not None:
                on_first_sleep()

        stdout, stderr = io.StringIO(), io.StringIO()
        started_wall, started_cpu = time.perf_counter_ns(), time.process_time_ns()
        with mock.patch.object(wake_hold, "project_wake_items", project):
            status = codex_wait.run_stop_waiter(
                bus_home=self.runtime.bus_home,
                hook_payload={"cwd": str(self.runtime.workspace), "session_id": self.SESSION},
                stdout=stdout,
                stderr=stderr,
                monotonic=lambda: clock[0],
                sleep=sleep,
                wall_time=lambda: 1000.0,
                poll_interval_seconds=1.0,
            )
        return {
            "status": status, "stdout": stdout.getvalue(), "stderr": stderr.getvalue(),
            "clock_seconds": clock[0], "sleep_count": len(sleeps),
            "projection_count": len(projections),
            "projection_wall_ns": sum(row["wall_ns"] for row in projections),
            "projection_cpu_ns": sum(row["cpu_ns"] for row in projections),
            "runtime_wall_ns": time.perf_counter_ns() - started_wall,
            "runtime_cpu_ns": time.process_time_ns() - started_cpu,
        }

    def _rows(self, family: str, kind: str) -> list[dict]:
        relative = "receipts/" + family + "/" + self.node + ".jsonl"
        if not self.root.resolve_relative(relative).exists():
            return []
        return read_records_snapshot(self.root, relative, allowed_kinds={kind})

    def _assert_idle_deadline(self, result: dict) -> None:
        self.assertEqual(0, result["status"], result)
        self.assertEqual("", result["stderr"], result)
        self.assertEqual(20.0, result["clock_seconds"], result)
        self.assertEqual(20, result["sleep_count"], result)
        self.assertEqual(
            {"decision": "block", "reason": "(floati: wait deadline exhausted; end this turn to re-arm)"},
            json.loads(result["stdout"]), result,
        )
        rows = self._rows("codex-wait-exhaustion", "codex_wait_exhaustion_receipt")
        self.assertEqual(1, len(rows), result)
        self.assertEqual(20, rows[0]["waited_seconds"], result)

    def _assert_silent_exit(self, result: dict, reason: str, ticks: int = 1) -> None:
        self.assertEqual(0, result["status"], result)
        self.assertEqual("", result["stdout"], result)
        self.assertEqual(float(ticks), result["clock_seconds"], result)
        self.assertEqual(ticks, result["sleep_count"], result)
        self.assertEqual([], self._rows("codex-wait-exhaustion", "codex_wait_exhaustion_receipt"))
        rows = self._rows("wake-waiter-exit", "wake_waiter_exit_receipt")
        self.assertEqual([reason], [row["reason_code"] for row in rows], result)

    @staticmethod
    def _replace(path: Path, raw: bytes) -> None:
        original = path.stat()
        temporary = path.with_name(path.name + ".f013-replacement")
        temporary.write_bytes(raw)
        os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(temporary, path)

    def test_empty_twenty_tick_wait_does_not_repeat_full_projection(self) -> None:
        """Catches full semantic replay on every unchanged empty-inbox poll."""
        before = self._inputs()
        result = self._run()
        self._assert_idle_deadline(result)
        self.assertEqual(before, self._inputs())
        self.assertGreater(result["projection_count"], 0, result)
        self.assertLessEqual(result["projection_count"], 3, result)

    def test_held_twenty_tick_wait_does_not_repeat_full_projection(self) -> None:
        """Catches replaying unchanged nonempty held history on every poll."""
        message = self._hold("f013-held-cost")
        before = self._inputs()
        result = self._run()
        self._assert_idle_deadline(result)
        self.assertEqual(before, self._inputs())
        self.assertEqual([message["id"]], EventLog(self.root).unacked_ids(self.node))
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))
        self.assertGreater(result["projection_count"], 0, result)
        self.assertLessEqual(result["projection_count"], 3, result)

    def test_next_poll_sees_fresh_append_with_restored_mtime(self) -> None:
        """Catches an idle cache hiding an appended frame when mtime is restored."""
        self._hold("f013-append-held")
        path = self.root.resolve_relative("events.jsonl")
        original = path.stat()
        appended = []

        def append() -> None:
            appended.append(self.runtime.send("f013-next-poll-fresh"))
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(original.st_mtime_ns, path.stat().st_mtime_ns)

        result = self._run(append)
        self.assertEqual(0, result["status"], result)
        self.assertEqual("", result["stderr"], result)
        self.assertEqual(1, result["sleep_count"], result)
        decision = json.loads(result["stdout"])
        self.assertEqual("block", decision["decision"], result)
        self.assertIn(appended[0]["id"], decision["reason"], result)
        self.assertNotIn("deadline exhausted", decision["reason"], result)
        self.assertEqual([], self._rows("codex-wait-exhaustion", "codex_wait_exhaustion_receipt"))
        attempts = self._rows("wakes", "wake_attempt_receipt")
        self.assertEqual(1, len(attempts), result)
        self.assertEqual([appended[0]["id"]], attempts[0]["item_ids"], result)

    def test_next_poll_rejects_same_size_corrupt_frame_with_restored_mtime(self) -> None:
        """Catches metadata-only reuse of a same-size corrupted event prefix."""
        self._hold("f013-corruption-held")
        path = self.root.resolve_relative("events.jsonl")
        original, raw = path.stat(), path.read_bytes()
        self.assertTrue(raw.startswith(b"{"))

        def corrupt() -> None:
            path.write_bytes(b"!" + raw[1:])
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(original.st_size, path.stat().st_size)
            self.assertEqual(original.st_mtime_ns, path.stat().st_mtime_ns)

        result = self._run(corrupt)
        self._assert_silent_exit(result, "integrity_failure")
        self.assertIn("wake_evaluation", result["stderr"], result)
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))

    def test_next_poll_rejects_middle_frame_corruption_with_restored_mtime(self) -> None:
        """Catches sampled-prefix reuse hiding a same-size middle-frame rewrite."""
        messages = [self.runtime.send("f013-middle-held-" + str(index)) for index in range(3)]
        presented, delivery = EventLog(self.root).present(self.node)
        self.assertEqual([row["id"] for row in messages], [row["id"] for row in presented])
        self.assertIsNotNone(delivery)
        path = self.root.resolve_relative("events.jsonl")
        original, raw = path.stat(), path.read_bytes()
        frames = raw.splitlines(keepends=True)
        self.assertGreaterEqual(len(frames), 3)
        middle = len(frames) // 2
        self.assertGreater(middle, 0)
        self.assertLess(middle, len(frames) - 1)
        self.assertTrue(frames[middle].startswith(b"{"))
        offset = sum(len(frame) for frame in frames[:middle])

        def corrupt_middle() -> None:
            with path.open("r+b") as stream:
                stream.seek(offset)
                self.assertEqual(1, stream.write(b"!"))
                stream.flush()
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            rewritten, after = path.read_bytes(), path.stat()
            self.assertEqual((original.st_dev, original.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(original.st_size, after.st_size)
            self.assertEqual(original.st_mtime_ns, after.st_mtime_ns)
            self.assertEqual(frames[0], rewritten[:len(frames[0])])
            self.assertEqual(raw[:offset], rewritten[:offset])
            self.assertEqual(b"!", rewritten[offset:offset + 1])
            self.assertEqual(raw[offset + 1:], rewritten[offset + 1:])

        result = self._run(corrupt_middle)
        self._assert_silent_exit(result, "integrity_failure")
        self.assertIn("wake_evaluation", result["stderr"], result)
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))

    def test_next_poll_stops_after_native_claim_takeover(self) -> None:
        """Catches cached authority allowing a superseded claimant to keep waiting."""
        result = self._run(lambda: self.runtime.arm_session("f013-successor"))
        self._assert_silent_exit(result, "not_claimant")
        self.assertEqual("", result["stderr"], result)

    def test_paused_session_at_entry_does_not_consume_fresh_work(self) -> None:
        """Catches idle-cache setup bypassing the baseline's paused-entry gate."""
        message = self.runtime.send("f013-paused-fresh")
        WakeController(self.root).pause(self.node, self.SESSION, idempotency_key="f013-pause")
        result = self._run()
        self._assert_silent_exit(result, "paused", ticks=0)
        self.assertEqual("", result["stderr"], result)
        self.assertEqual([message["id"]], EventLog(self.root).unacked_ids(self.node))
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))

    def test_next_poll_stops_after_consent_inode_withdrawal(self) -> None:
        """Catches caching armed consent after an operator restore removes it."""
        path = self.root.resolve_relative(consent_ledger_relative(self.node))
        before = path.stat()
        result = self._run(lambda: self._replace(path, b""))
        self._assert_silent_exit(result, "consent_withdrawn")
        self.assertEqual("", result["stderr"], result)
        self.assertNotEqual(before.st_ino, path.stat().st_ino)
        self.assertEqual(before.st_mtime_ns, path.stat().st_mtime_ns)

    def test_next_poll_stops_after_map_inode_removes_workspace(self) -> None:
        """Catches retaining a cached participant after a restored map unbinds it."""
        path = self.runtime.bus_home / "codex-wait/workspaces.v0.json"
        before = path.stat()
        replacement = (json.dumps({"schema_version": 0, "tenant_id": "demo-fleet", "mappings": []},
                                  sort_keys=True, separators=(",", ":")) + "\n").encode()
        result = self._run(lambda: self._replace(path, replacement))
        self._assert_silent_exit(result, "not_claimant")
        self.assertEqual("", result["stderr"], result)
        self.assertNotEqual(before.st_ino, path.stat().st_ino)
        self.assertEqual(before.st_mtime_ns, path.stat().st_mtime_ns)

    def test_native_append_racing_first_evaluation_is_not_absorbed(self) -> None:
        """Catches baselining new bytes after a decision made from older bytes."""
        self._hold("f013-racing-held")
        real_evaluate = wake_hold.WakeHoldController.evaluate
        appended = []

        def evaluate_then_append(controller, *args, **kwargs):
            artifact = real_evaluate(controller, *args, **kwargs)
            if not appended:
                self.assertEqual("held_only", artifact["state"])
                appended.append(self.runtime.send("f013-racing-fresh"))
            return artifact

        with mock.patch.object(wake_hold.WakeHoldController, "evaluate", evaluate_then_append):
            result = self._run()
        self.assertEqual(0, result["status"], result)
        self.assertEqual("", result["stderr"], result)
        self.assertLessEqual(result["sleep_count"], 1, result)
        decision = json.loads(result["stdout"])
        self.assertEqual("block", decision["decision"], result)
        self.assertIn(appended[0]["id"], decision["reason"], result)
        self.assertEqual([], self._rows("codex-wait-exhaustion", "codex_wait_exhaustion_receipt"))
        attempts = self._rows("wakes", "wake_attempt_receipt")
        self.assertEqual(1, len(attempts), result)
        self.assertEqual([appended[0]["id"]], attempts[0]["item_ids"], result)

    def test_unavailable_notifications_fall_back_to_quiet_native_evaluation(self) -> None:
        """Catches an optional observer failure silencing evaluation or adding diagnostics."""
        from floati.errors import ProtocolRefusal

        before = self._inputs()
        unavailable = ProtocolRefusal("board_event_watch_unavailable", "injected unavailable observer")
        with mock.patch("floati.tui.BoardFilesystemWakeup", side_effect=unavailable) as source:
            result = self._run()
        self.assertTrue(source.called, "notification failure seam was not exercised")
        self._assert_idle_deadline(result)
        self.assertEqual(before, self._inputs())
        self.assertGreater(result["projection_count"], 3, result)
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))

    def test_unknown_selected_metadata_keeps_native_evaluation(self) -> None:
        """Catches interpreting an unreadable selected-input hint as unchanged truth."""
        self._hold("f013-unknown-metadata-held")
        before = self._inputs()
        real_fingerprint = codex_wait._WaiterInputsWatch._fingerprint_paths
        hints = []

        def uncertain_fingerprint(watch, *args, **kwargs):
            fingerprint = real_fingerprint(watch, *args, **kwargs)
            watch.metadata_unknown = True
            hints.append(None)
            return fingerprint

        with mock.patch.object(codex_wait._WaiterInputsWatch, "_fingerprint_paths", uncertain_fingerprint):
            result = self._run()
        self.assertTrue(hints, "selected metadata uncertainty seam was not exercised")
        self._assert_idle_deadline(result)
        self.assertEqual(before, self._inputs())
        self.assertGreater(result["projection_count"], 3, result)
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))

    def test_incomplete_notification_registration_keeps_native_evaluation(self) -> None:
        """Catches trusting a functioning notifier that covers no selected inputs."""
        self._hold("f013-incomplete-registration-held")
        before = self._inputs()
        observers = []

        class IncompleteObserver:
            def __init__(self, *_args, **_kwargs):
                self._read, self._write = os.pipe()
                self.closed = False
                observers.append(self)

            def fileno(self):
                return self._read

            def registered_paths(self):
                return ()

            def close(self):
                if self.closed:
                    return
                self.closed = True
                try:
                    os.close(self._read)
                finally:
                    os.close(self._write)

            def drain(self):
                raise AssertionError("incompletely registered observer must not enable reuse")

        with mock.patch("floati.tui.BoardFilesystemWakeup", IncompleteObserver):
            result = self._run()
        self.assertTrue(observers, "registration uncertainty seam was not exercised")
        self.assertTrue(all(observer.closed for observer in observers))
        self._assert_idle_deadline(result)
        self.assertEqual(before, self._inputs())
        self.assertGreater(result["projection_count"], 3, result)
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))

    def test_held_child_release_is_seen_without_filesystem_change(self) -> None:
        """Catches treating unchanged ledger bytes as proof a holder stays alive."""
        from floati import codex_wait_liveness as liveness

        message = self._hold("f013-child-held")
        context = multiprocessing.get_context("fork")
        parent_channel, child_channel = context.Pipe()
        child = context.Process(
            target=_native_holder_child,
            args=(self.root, self.node, self.SESSION, child_channel, parent_channel),
        )
        child.start()
        child_channel.close()

        def write_from_child(root, node, session):
            self.assertEqual((self.root, self.node, self.SESSION), (root, node, session))
            parent_channel.send("write")
            self.assertTrue(parent_channel.poll(5), "native holder did not publish testimony")
            row = parent_channel.recv()
            self.assertEqual(child.pid, row["pid"])
            self.assertEqual("live", liveness.classify_holder(liveness.read_holder_testimony(root, node)))
            return row

        def release() -> None:
            before = self._inputs()
            path = liveness.holder_testimony_path(self.root, self.node)
            testimony = path.read_bytes()
            parent_channel.send("release")
            child.join(5)
            self.assertFalse(child.is_alive(), "owned native holder did not exit")
            self.assertEqual(0, child.exitcode)
            self.assertEqual(before, self._inputs())
            self.assertEqual(testimony, path.read_bytes())
            self.assertEqual("released", liveness.classify_holder(liveness.read_holder_testimony(self.root, self.node)))

        try:
            with mock.patch.object(liveness, "write_holder_testimony", write_from_child):
                result = self._run(release)
        finally:
            if child.is_alive():
                try:
                    parent_channel.send("release")
                except (BrokenPipeError, EOFError, OSError):
                    pass
            parent_channel.close()
            child.join(12)
            self.assertFalse(child.is_alive(), "bounded owned holder cleanup failed")
            child.close()
        self.assertEqual(0, result["status"], result)
        self.assertEqual("", result["stderr"], result)
        self.assertEqual(1, result["sleep_count"], result)
        decision = json.loads(result["stdout"])
        self.assertEqual("block", decision["decision"], result)
        self.assertIn(message["id"], decision["reason"], result)
        self.assertEqual([], self._rows("codex-wait-exhaustion", "codex_wait_exhaustion_receipt"))
        attempts = self._rows("wakes", "wake_attempt_receipt")
        self.assertEqual(1, len(attempts), result)
        self.assertEqual([message["id"]], attempts[0]["item_ids"], result)

    def test_claim_takeover_after_clean_hint_still_checks_native_authority(self) -> None:
        """Catches an idle-cache shortcut running before current claim validation."""
        real_changed = codex_wait._WaiterInputsWatch.changed
        real_evaluate = wake_hold.WakeHoldController.evaluate
        polls, evaluations, takeovers = [], [], []

        def evaluate(controller, *args, **kwargs):
            artifact = real_evaluate(controller, *args, **kwargs)
            evaluations.append(artifact["state"])
            return artifact

        def changed_then_take_over(watch):
            hint = real_changed(watch)
            polls.append(hint)
            self.assertLessEqual(len(polls), 21, "waiter exceeded its native twenty-tick deadline budget")
            if evaluations and not hint[0] and not takeovers:
                takeovers.append({"poll": len(polls), "evaluations": len(evaluations)})
                self.runtime.arm_session("f013-clean-hint-successor")
            return hint

        with mock.patch.object(codex_wait._WaiterInputsWatch, "changed", changed_then_take_over),\
                mock.patch.object(wake_hold.WakeHoldController, "evaluate", evaluate):
            result = self._run()
        self.assertEqual(1, len(takeovers), result)
        self.assertGreater(takeovers[0]["poll"], 1, result)
        self.assertEqual(takeovers[0]["poll"], len(polls), result)
        self.assertEqual(takeovers[0]["evaluations"], len(evaluations), result)
        self._assert_silent_exit(result, "not_claimant", ticks=takeovers[0]["poll"] - 1)
        self.assertEqual("", result["stderr"], result)
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))

    def test_unavailable_notifications_still_reject_middle_frame_rewrite(self) -> None:
        """Catches native fallback retaining the sampled cursor after a middle rewrite."""
        from floati.errors import ProtocolRefusal

        messages = [self.runtime.send("f013-fallback-middle-" + str(index)) for index in range(3)]
        presented, delivery = EventLog(self.root).present(self.node)
        self.assertEqual([row["id"] for row in messages], [row["id"] for row in presented])
        self.assertIsNotNone(delivery)
        path = self.root.resolve_relative("events.jsonl")
        original, raw = path.stat(), path.read_bytes()
        frames = raw.splitlines(keepends=True)
        self.assertGreaterEqual(len(frames), 3)
        middle = len(frames) // 2
        self.assertTrue(0 < middle < len(frames) - 1)
        self.assertTrue(frames[middle].startswith(b"{"))
        offset = sum(len(frame) for frame in frames[:middle])

        def corrupt() -> None:
            with path.open("r+b") as stream:
                stream.seek(offset)
                self.assertEqual(1, stream.write(b"!"))
                stream.flush()
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            after = path.stat()
            self.assertEqual((original.st_dev, original.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(original.st_size, after.st_size)
            self.assertEqual(original.st_mtime_ns, after.st_mtime_ns)
            self.assertEqual(raw[:offset] + b"!" + raw[offset + 1:], path.read_bytes())

        unavailable = ProtocolRefusal("board_event_watch_unavailable", "injected unavailable observer")
        with mock.patch("floati.tui.BoardFilesystemWakeup", side_effect=unavailable) as source:
            result = self._run(corrupt)
        self.assertTrue(source.called, "notification failure seam was not exercised")
        self._assert_silent_exit(result, "integrity_failure")
        self.assertIn("wake_evaluation", result["stderr"], result)
        self.assertEqual([], self._rows("wakes", "wake_attempt_receipt"))


if __name__ == "__main__":
    unittest.main()
