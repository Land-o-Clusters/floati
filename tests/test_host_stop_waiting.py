"""Current native Stop-holder evidence, never Codex turn-completion testimony.

The parent owns RED/GREEN execution. These private scratch fixtures run the
real waiter; only explicit fault controls replace host liveness or writes.
"""
from datetime import datetime, timedelta, timezone
import io
import json
import os
import unittest
from unittest import mock

import tests
from floati import codex_wait, codex_wait_liveness
from floati.codex_wait_contract import CodexWaitConsentLedger
from floati.events import EventLog
from floati.stop_hold import StopHoldLedger, _HoldEvidence
from floati.wake_health import WakeHealthProjection
from tests import test_codex_wait as native


SESSION = "host-stop-waiting-native-session"
ENDED_UNKNOWN = {"state": "UNKNOWN", "reason": "stop_is_not_turn_completion"}


class HostStopWaitingTests(unittest.TestCase):
    def setUp(self):
        self.f = native.CodexWaitRuntimeTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.arm_codex_role_node("host-stop-waiting")
        self.f.arm_session(SESSION)
        self.root = self.f.root
        self.node = self.f.participant.binding.node_id
        self.now = datetime.now(timezone.utc)
        self.holder = codex_wait_liveness.holder_testimony_path(self.root, self.node)

    def observe(self, now=None):
        method = getattr(WakeHealthProjection(self.root), "stop_waiting", None)
        self.assertTrue(callable(method), "current native Stop-holder projection is absent")
        return method(self.node, self.now if now is None else now)

    def assert_state(self, wanted, fact):
        self.assertEqual(wanted, fact["state"], fact)
        self.assertEqual(ENDED_UNKNOWN, fact["turn_ended"])
        self.assertTrue(fact["reason"])

    def current_start(self):
        rows = StopHoldLedger(self.root).read(self.node)
        starts = [row for row in rows if row["phase"] == "start"]
        self.assertTrue(starts, "fixture did not reach a real native hold")
        return starts[-1]

    def run_waiter(self, during_hold=None):
        clock = [0.0]
        visited = []
        output, errors = io.StringIO(), io.StringIO()
        def tick(seconds):
            if not visited:
                visited.append(True)
                if during_hold is not None:
                    during_hold()
            clock[0] += seconds
        status = codex_wait.run_stop_waiter(
            bus_home=self.f.bus_home,
            hook_payload={"cwd": str(self.f.workspace), "session_id": SESSION},
            stdout=output, stderr=errors, monotonic=lambda: clock[0], sleep=tick,
            wall_time=lambda: self.now.timestamp() + clock[0], poll_interval_seconds=1.0)
        self.assertEqual(0, status, errors.getvalue())
        self.assertTrue(visited, errors.getvalue() or output.getvalue())
        return output.getvalue(), errors.getvalue()

    def replace_holder(self, **changes):
        row = json.loads(self.holder.read_bytes())
        row.update(changes)
        self.holder.write_text(json.dumps(row) + "\n", encoding="utf-8")

    def test_armed_session_without_actual_hold_is_unknown(self):
        self.assert_state("UNKNOWN", self.observe())

    def test_native_hold_binds_exact_invocation_without_minting_completion(self):
        def during_hold():
            start = self.current_start()
            testimony = json.loads(self.holder.read_bytes())
            self.assertEqual(start["invocation_id"], testimony.get("invocation_id"),
                "a live session alone cannot identify this actual Stop hold")
            before = self.f.bus_bytes()
            self.assert_state("STOP_WAITING", self.observe())
            self.assertEqual(before, self.f.bus_bytes(), "observation wrote fleet state")
            self.assertEqual([], EventLog(self.root).event_records())
        self.run_waiter(during_hold)
        self.assertFalse(any(row.get("kind") == "seat_execution"
            for row in EventLog(self.root).event_records()))
        self.assertEqual([], EventLog(self.root).records())

    def test_native_exit_retires_invocation_while_its_process_remains_live(self):
        self.run_waiter()
        testimony = codex_wait_liveness.read_holder_testimony(self.root, self.node)
        self.assertEqual("live", codex_wait_liveness.classify_holder(testimony))
        self.assertFalse(json.loads(self.holder.read_bytes()).get("invocation_id"),
            "completed hold left current invocation authority behind")
        self.assert_state("UNKNOWN", self.observe())

    def test_legacy_live_process_testimony_is_not_current_invocation_proof(self):
        def during_hold():
            row = json.loads(self.holder.read_bytes())
            row.pop("invocation_id", None)
            self.holder.write_text(json.dumps(row) + "\n", encoding="utf-8")
            testimony = codex_wait_liveness.read_holder_testimony(self.root, self.node)
            self.assertEqual("live", codex_wait_liveness.classify_holder(testimony),
                "legacy process classification must remain compatible")
            self.assert_state("UNKNOWN", self.observe())
        self.run_waiter(during_hold)

    def test_open_hold_without_holder_testimony_is_unknown(self):
        def during_hold():
            self.current_start()
            self.holder.unlink()
            self.assert_state("UNKNOWN", self.observe())
        self.run_waiter(during_hold)

    def test_foreign_invocation_or_session_cannot_borrow_current_live_process(self):
        def during_hold():
            original = self.holder.read_bytes()
            for changes in ({"invocation_id": "stop-hold-foreign"},
                            {"session_id": "another-native-session"}):
                with self.subTest(changes=changes):
                    self.holder.write_bytes(original)
                    self.replace_holder(**changes)
                    self.assert_state("UNKNOWN", self.observe())
            self.holder.write_bytes(original)
        self.run_waiter(during_hold)

    def test_dead_reused_or_unmeasurable_process_never_shows_stop_waiting(self):
        def during_hold():
            start = json.loads(self.holder.read_bytes())["process_start_epoch"]
            controls = [(False, start), (True, start + 60), (True, None)]
            for alive, measured in controls:
                with self.subTest(alive=alive, measured=measured), mock.patch(
                    "floati.codex_wait_liveness.process_alive", return_value=alive), mock.patch(
                    "floati.codex_wait_liveness.process_start_epoch", return_value=measured):
                    self.assert_state("UNKNOWN", self.observe())
        self.run_waiter(during_hold)

    def test_exact_budget_end_cannot_reuse_an_open_start_and_live_process(self):
        def during_hold():
            start = self.current_start()
            begun = datetime.fromisoformat(start["started_at"].replace("Z", "+00:00"))
            self.assert_state("UNKNOWN", self.observe(
                begun + timedelta(seconds=start["deadline_seconds"])))
        self.run_waiter(during_hold)

    def test_superseded_session_immediately_invalidates_current_stop_observation(self):
        def during_hold():
            self.f.arm_session("host-stop-waiting-successor")
            self.assert_state("UNKNOWN", self.observe())
        self.run_waiter(during_hold)

    def test_changed_consent_cannot_authorize_the_old_open_invocation(self):
        def during_hold():
            CodexWaitConsentLedger(self.root).arm(self.f.participant.binding,
                hook_timeout_seconds=10, wait_deadline_seconds=1,
                idempotency_key="host-stop-waiting-new-consent")
            self.assert_state("UNKNOWN", self.observe())
        self.run_waiter(during_hold)

    def test_paused_session_cannot_show_stop_waiting(self):
        from floati.wake_control import WakeController
        def during_hold():
            WakeController(self.root).pause(self.node, SESSION,
                idempotency_key="host-stop-waiting-pause")
            self.assert_state("UNKNOWN", self.observe())
        self.run_waiter(during_hold)

    def test_unreadable_native_hold_history_cannot_show_stop_waiting(self):
        def during_hold():
            path = self.root.tenant_home / StopHoldLedger.relative(self.node)
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"{not-a-native-hold}\n")
                self.assert_state("UNKNOWN", self.observe())
            finally:
                path.write_bytes(original)
        self.run_waiter(during_hold)

    def test_failed_exit_append_still_retires_actual_invocation_before_return(self):
        with mock.patch.object(StopHoldLedger, "_close_already_guarded",
                side_effect=OSError("fixture exit ledger unavailable")):
            self.run_waiter()
        self.assertFalse(json.loads(self.holder.read_bytes()).get("invocation_id"),
            "failed ledger append must not leave a live completed-process hold")
        self.assert_state("UNKNOWN", self.observe())

    def test_old_close_cannot_retire_a_newer_native_hold_in_same_session(self):
        consent = CodexWaitConsentLedger(self.root).require_armed(self.f.participant.binding)
        stamp = lambda: self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        older, newer = _HoldEvidence(io.StringIO(), stamp), _HoldEvidence(io.StringIO(), stamp)
        self.addCleanup(older.close)
        self.addCleanup(newer.close)
        older.start(self.f.participant, consent, SESSION, 2)
        newer.start(self.f.participant, consent, SESSION, 2)
        self.assertIsNotNone(newer.start_row)
        older.close()
        self.assertEqual(newer.start_row["invocation_id"],
            json.loads(self.holder.read_bytes()).get("invocation_id"))
        self.assert_state("STOP_WAITING", self.observe())

    def test_holder_change_during_liveness_measurement_cannot_show_stale_positive(self):
        def during_hold():
            original = codex_wait_liveness.classify_holder
            changed = []
            def invalidate(testimony):
                verdict = original(testimony)
                if not changed:
                    changed.append(True)
                    self.replace_holder(invocation_id="stop-hold-replaced")
                return verdict
            with mock.patch.object(codex_wait_liveness, "classify_holder", side_effect=invalidate):
                self.assert_state("UNKNOWN", self.observe())
        self.run_waiter(during_hold)

    def test_fresh_and_cached_board_reobserve_physical_holder_liveness(self):
        from floati.tui import model_from_root
        def during_hold():
            for _ in range(2):
                model = model_from_root(self.root, now=self.now)
                node = next(row for row in model.nodes if row["node_id"] == self.node)
                self.assertIn("stop_waiting", node, "board omits the independent Stop observation")
                self.assert_state("STOP_WAITING", node["stop_waiting"])
                self.assertEqual("NO_CAR", node["car_execution"]["state"])
            with mock.patch("floati.codex_wait_liveness.process_alive", return_value=False):
                model = model_from_root(self.root, now=self.now)
                node = next(row for row in model.nodes if row["node_id"] == self.node)
                self.assert_state("UNKNOWN", node["stop_waiting"])
        self.run_waiter(during_hold)

    def acquired_scope(self, budget=2):
        consent = CodexWaitConsentLedger(self.root).require_armed(self.f.participant.binding)
        stamp = lambda: self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        scope = _HoldEvidence(io.StringIO(), stamp)
        self.addCleanup(scope.close)
        scope.start(self.f.participant, consent, SESSION, budget)
        self.assertIsNotNone(scope.start_row)
        self.assert_state("STOP_WAITING", self.observe())
        return scope

    def assert_publication_withheld(self, scope):
        output = io.StringIO()
        failure = None
        try:
            scope.output(output).write('{"decision":"block","reason":"fixture mail"}\n')
        except Exception as exc:
            failure = exc
        self.assertEqual("", output.getvalue(),
            "output advanced while this process still claimed an unclosed native hold")
        self.assertIsNotNone(failure,
            "uncertain closure must refuse publication, not return a successful write")
        self.assertEqual([], EventLog(self.root).event_records())

    def test_public_close_guard_failure_cannot_publish_while_holder_still_claims_waiting(self):
        scope = self.acquired_scope()
        with mock.patch("floati.stop_hold.wake_coordination_guard",
                side_effect=PermissionError("fixture close guard unavailable")):
            self.assert_publication_withheld(scope)

    def test_public_close_guard_failure_remains_retryable_after_guard_recovers(self):
        scope = self.acquired_scope()
        with mock.patch("floati.stop_hold.wake_coordination_guard",
                side_effect=PermissionError("fixture close guard unavailable")):
            try:
                scope.close()
            except Exception:
                pass
        scope.close()
        self.assert_state("UNKNOWN", self.observe())
        self.assertEqual(["start", "exit"],
            [row["phase"] for row in StopHoldLedger(self.root).read(self.node)])

    def test_combined_retirement_and_exit_append_failure_cannot_publish(self):
        scope = self.acquired_scope()
        with mock.patch("floati.codex_wait_liveness._retire_holder_invocation",
                side_effect=OSError("fixture holder retirement unavailable")), mock.patch.object(
                StopHoldLedger, "_close_already_guarded",
                side_effect=OSError("fixture exit append unavailable")):
            self.assert_publication_withheld(scope)

    def test_exact_exit_commit_allows_output_when_only_holder_retirement_fails(self):
        scope = self.acquired_scope()
        output = io.StringIO()
        with mock.patch("floati.codex_wait_liveness._retire_holder_invocation",
                side_effect=OSError("fixture holder retirement unavailable")):
            scope.output(output).write("fixture output after exact exit\n")
        self.assertEqual("fixture output after exact exit\n", output.getvalue())
        self.assert_state("UNKNOWN", self.observe())

    def check_idle_board_refresh(self, *, expires):
        """The actual event loop must schedule a reload without an FS event."""
        from floati.tui import model_from_root, run_board
        scope = self.acquired_scope(budget=0.25 if expires else 2)
        start = scope.start_row
        deadline = datetime.fromisoformat(start["started_at"].replace("Z", "+00:00")) + (
            timedelta(seconds=start["deadline_seconds"]))
        hold_path = self.root.tenant_home / StopHoldLedger.relative(self.node)
        before = (hold_path.read_bytes(), self.holder.read_bytes())
        observed_at = [self.now]
        states, timeouts = [], []

        class Clock:
            @staticmethod
            def now(_timezone=None):
                return observed_at[0]

            @staticmethod
            def fromisoformat(value):
                return datetime.fromisoformat(value)

        class TTY(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 93

        def load_model():
            model = model_from_root(self.root, now=observed_at[0])
            node = next(row for row in model.nodes if row["node_id"] == self.node)
            states.append(node["stop_waiting"]["state"])
            return model

        def read_event(timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                self.assertIsNotNone(timeout,
                    "STOP_WAITING must not block forever without keyboard or filesystem input")
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 1.0, "physical-holder recheck is not bounded")
                self.assertLessEqual(timeout, (deadline - observed_at[0]).total_seconds(),
                    "board can retain STOP_WAITING beyond the native hold's budget")
                if expires:
                    observed_at[0] = deadline
                else:
                    alive.return_value = False
                return ""  # Timeout only: no keyboard/model/filesystem event.
            self.assertIsNone(timeout, "UNKNOWN idle state should return to event-driven blocking")
            return "q"

        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), mock.patch(
                "floati.tui.termios.tcgetattr", return_value=[]), mock.patch(
                "floati.tui.termios.tcsetattr"), mock.patch("floati.tui.tty.setcbreak"), mock.patch(
                "floati.tui.shutil.get_terminal_size", return_value=os.terminal_size((100, 40))), mock.patch(
                "floati.tui.datetime", Clock), mock.patch(
                "floati.codex_wait_liveness.process_alive", return_value=True) as alive:
            self.assertEqual(0, run_board(model_loader=load_model,
                input_stream=TTY(), output_stream=TTY(), read_event=read_event,
                terminal_response=b"\x1b[?1;2c"))
        self.assertEqual(["STOP_WAITING", "UNKNOWN"], states)
        self.assertEqual(before, (hold_path.read_bytes(), self.holder.read_bytes()),
            "control wrote the durable testimony instead of observing timer/liveness change")
        self.assertEqual("live", codex_wait_liveness.classify_holder(
            codex_wait_liveness.read_holder_testimony(self.root, self.node)))

    def test_idle_board_rechecks_holder_crash_without_any_filesystem_write(self):
        self.check_idle_board_refresh(expires=False)

    def test_idle_board_schedules_native_budget_end_before_liveness_poll(self):
        self.check_idle_board_refresh(expires=True)
