"""Independent approval absence stays visible beside native car execution."""
import json
import unittest

import tests
from tests import test_cursor_host_stop as fixture


class HostApprovalSurfaceTests(unittest.TestCase):
    setUp = fixture.CursorHostStopTests.setUp
    claim_car = fixture.CursorHostStopTests.claim_car
    payload = fixture.CursorHostStopTests.payload
    observe = fixture.CursorHostStopTests.observe
    executions = fixture.CursorHostStopTests.executions

    def board(self):
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines
        model = model_from_root(self.root)
        index = next(i for i, node in enumerate(model.nodes) if node["node_id"] == "builder")
        return model.nodes[index]["car_execution"], "\n".join(_detail_lines(model, 300, index))

    def assert_absence(self, projection):
        approval = projection.get("host_approval")
        self.assertIsInstance(approval, dict, "Cursor approval lacks typed unsupported-host projection")
        self.assertEqual("UNKNOWN", approval["state"])
        self.assertEqual("cursor_approval_callback_absent", approval["reason"])
        self.assertIn("3.24.12", json.dumps(approval))

    def test_approval_unknown_names_dedicated_callback_reopen_trigger(self):
        projection, rendered = self.board()
        self.assert_absence(projection)
        approval = projection["host_approval"]
        trigger = approval.get("reopen_trigger")
        self.assertIsInstance(trigger, str)
        self.assertIn("approval", trigger.casefold())
        self.assertIn("hook", trigger.casefold())
        self.assertIn("REOPEN", rendered)
        self.assertNotIn("current_cursor_version", approval,
            "the static absence baseline must not masquerade as live installed-version measurement")

    def test_doctor_names_measured_callback_absence_without_stalled_inference(self):
        from floati.doctor import project_seat_execution_findings
        finding = next(row for row in project_seat_execution_findings(self.root) if row["subject"] == "builder")
        projection = json.loads(finding["detail"])
        self.assert_absence(projection)
        self.assertEqual("UNKNOWN", projection["state"])
        self.assertEqual([], self.executions())

    def test_no_car_board_still_shows_approval_unknown_and_measured_version(self):
        self.work.complete(self.item["id"], "builder", [fixture.BINDING], now=self.now)
        projection, rendered = self.board()
        self.assertEqual("NO_CAR", projection["state"])
        self.assert_absence(projection)
        self.assertIn("APPROVAL UNKNOWN", rendered)
        self.assertIn("cursor_approval_callback_absent", rendered)
        self.assertIn("3.24.12", rendered)
        self.assertNotIn("STALLED", rendered)

    def test_approval_unknown_does_not_override_real_running(self):
        from floati.ids import uuid7_hex
        from floati.seat_execution import SeatExecutionController
        from floati.workers import WorkerReceipts
        receipts = WorkerReceipts(self.root)
        worker_session = "worker-" + uuid7_hex()
        receipts.append(worker_session, self.item["id"], "builder", "codex", "claim", None, [], now=self.now)
        start = receipts.append(worker_session, self.item["id"], "builder", "codex", "spawn", None, [], now=self.now)
        controller = SeatExecutionController(self.root)
        capability = controller.bind_worker(start, self.workspace)
        self.addCleanup(lambda: controller.callback(capability,
            {"event": "unknown", "code": "fixture_closed", "detail": "fixture cleanup"}))
        projection, rendered = self.board()
        self.assertEqual("RUNNING", projection["state"])
        self.assert_absence(projection)
        self.assertIn("APPROVAL UNKNOWN", rendered)
        self.assertIn("CAR RUNNING", rendered)

    def test_lifecycle_absence_is_rendered_without_none_or_fabricated_action(self):
        self.observe()
        projection, rendered = self.board()
        self.assertIsNone(projection["latest_execution"]["command"])
        self.assertIsNone(projection["latest_execution"]["code"])
        self.assertIsNone(projection["latest_execution"]["detail"])
        self.assertNotIn("ACTION None", rendered)
        self.assertNotIn("DETAIL null", rendered)
        self.assertNotIn("floati wake wait cursor", rendered)
        self.assertIn("completed", rendered)
        self.assertIn("APPROVAL UNKNOWN", rendered)

    def test_stop_waiting_renders_independently_without_turn_completion_claim(self):
        from dataclasses import replace
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines
        model = model_from_root(self.root)
        index = next(i for i, node in enumerate(model.nodes) if node["node_id"] == "builder")
        nodes = list(model.nodes)
        nodes[index] = dict(nodes[index], stop_waiting={"state": "STOP_WAITING",
            "reason": "native_stop_waiter_armed", "turn_ended": {
                "state": "UNKNOWN", "reason": "stop_is_not_turn_completion"}})
        rendered = "\n".join(_detail_lines(replace(model, nodes=tuple(nodes)), 300, index))
        self.assertIn("STOP_WAITING", rendered)
        self.assertIn("TURN ENDED UNKNOWN", rendered)
        self.assertIn("stop_is_not_turn_completion", rendered)
        self.assertNotIn("TURN ENDED FACT", rendered)

    def test_missing_stop_observation_renders_unknown(self):
        from dataclasses import replace
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines
        model = model_from_root(self.root)
        index = next(i for i, node in enumerate(model.nodes) if node["node_id"] == "builder")
        nodes = list(model.nodes)
        nodes[index] = {key: value for key, value in nodes[index].items() if key != "stop_waiting"}
        rendered = "\n".join(_detail_lines(replace(model, nodes=tuple(nodes)), 300, index))
        self.assertIn("STOP UNKNOWN", rendered)
        self.assertIn("TURN ENDED UNKNOWN", rendered)

    def test_denial_fact_renders_after_fact_time_without_pending_approval_or_stall(self):
        payload = fixture.CursorToolFailureTests.payload(self)
        fixture.CursorToolFailureTests.observe_failure(self, payload)
        projection, rendered = self.board()
        stamp = projection["latest_execution"]["source_binding"]["observed_at"]
        self.assertIn("tool request refused at " + stamp, rendered)
        self.assertIn("APPROVAL UNKNOWN", rendered)
        self.assertNotIn("STALLED", rendered)
        self.assertNotIn("fixture private refusal body", rendered)
        self.assertNotIn("fixture private command", rendered)

    def test_interrupt_fact_renders_interruption_without_permission_or_invented_tool(self):
        from floati.tui_render import _host_approval_lines
        stamp = "2026-10-16T04:00:00.000Z"
        rendered = "\n".join(_host_approval_lines({"state": "FACT", "reason": "interrupted",
            "observed_at": stamp, "tool_name": None, "sequence": 7,
            "turn_id": "interrupted-turn", "session_id": "interrupted-session"}, 300))
        self.assertIn("turn interrupted at " + stamp, rendered)
        self.assertNotIn("permission requested", rendered)
        self.assertNotIn("for UNKNOWN", rendered)
        self.assertNotIn("tool=", rendered)
        self.assertNotIn("None", rendered)


    def test_permission_fact_renders_the_ruled_recorded_event_limit(self):
        from floati.tui_render import _host_approval_lines
        stamp = "2026-10-16T05:00:00.000Z"
        rendered = "\n".join(_host_approval_lines({"state": "FACT", "reason": "permission_quiet",
            "observed_at": stamp, "tool_name": "Bash"}, 300))
        self.assertIn("permission requested at " + stamp + " for Bash; no later host event recorded for this session", rendered)

    def test_storage_probe_unknown_names_the_measured_errno_without_a_fact(self):
        import errno
        from floati.tui_render import _host_approval_lines
        rendered = "\n".join(_host_approval_lines({"state": "UNKNOWN",
            "reason": "host_observation_storage_not_writable", "errno": errno.ENOSPC}, 300))
        self.assertIn("UNKNOWN", rendered)
        self.assertIn("storage_not_writable", rendered)
        self.assertIn("errno=" + str(errno.ENOSPC), rendered)
        self.assertNotIn("FACT", rendered)


if __name__ == "__main__":
    unittest.main()
