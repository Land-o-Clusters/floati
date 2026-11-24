"""OpenCode plugin events retain host event IDs and native receipt authority."""
import unittest

import tests
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.events import EventLog
from floati.registry import Registry
from floati.waiter_consent import arm_waiter_consent
from tests import test_desktop_host_notifications as desktop
from tests import test_seat_execution_host_tool_native as fixture


class OpenCodeHostNotificationTests(unittest.TestCase):
    executions = desktop.DesktopHostNotificationTests.executions
    notify = desktop.DesktopHostNotificationTests.notify
    assert_notice = desktop.DesktopHostNotificationTests.assert_notice

    def setUp(self):
        fixture.HostToolCallbackNativeTests.setUp(self)
        self.node = "opencode-builder"
        Registry(self.root).register(self.node, "OpenCode")
        self.work.complete(self.item["id"], "builder", [fixture.BINDING], now=self.now)
        grant = fixture.AuthorityGrantStore(self.root).claim("opencode-work", self.node, 60, 60, self.now)
        self.item = self.work.add("native plugin callback", self.node, [fixture.BINDING], now=self.now,
            car_binding={"row": "F2-SEAT-STALL-HOST-1", "repository": "floati", "branch": "sol/car"})
        self.claim = self.work.claim(self.item["id"], self.node, "opencode-work", grant["epoch"], now=self.now)
        self.desktop = self.base / "opencode"
        self.desktop.mkdir()
        arm_waiter_consent(self.root.path, self.desktop, self.node, "OpenCode", 2, 1)
        binding = resolve_participant(self.root.path, self.desktop).binding
        consent = CodexWaitConsentLedger(self.root).require_armed(binding)
        self.session_claim = CodexWaitSessionLedger(self.root).arm(binding, consent,
            fixture.SESSION, idempotency_key="opencode-fixture", take_over=True)

    def payload(self, event="permission.asked", event_id="evt_native_pending"):
        properties = {"sessionID": fixture.SESSION}
        if event == "permission.asked":
            properties.update(id="per_native_request", permission="bash", patterns=["private command"],
                metadata={"command": "private approval command"}, always=["private pattern"])
        return {"cwd": str(self.desktop), "worktree": str(self.desktop),
            "event": {"id": event_id, "type": event, "properties": properties}}

    def test_pending_event_publishes_one_native_notification_without_tool_content(self):
        self.notify(self.payload(), "opencode")
        row = self.assert_notice("approval_pending", "opencode")
        self.assertEqual("evt_native_pending", row["source_binding"]["native_event_id"])
        self.assertIsNone(row["source_binding"]["native_prompt_id"])

    def test_replayed_event_is_silent_but_next_native_idle_is_a_new_observation(self):
        payload = self.payload("session.idle", "evt_native_idle")
        self.notify(payload, "opencode")
        before = EventLog(self.root).event_records()
        self.notify(payload, "opencode")
        self.assertEqual(before, EventLog(self.root).event_records())
        self.notify(self.payload("session.idle", "evt_native_next_idle"), "opencode")
        self.assertEqual(2, len(self.executions()))

    def test_missing_native_event_id_or_foreign_session_cannot_create_testimony(self):
        payload = self.payload()
        del payload["event"]["id"]
        self.notify(payload, "opencode")
        self.assertEqual([], self.executions())
        payload = self.payload()
        payload["event"]["properties"]["sessionID"] = "foreign-session"
        self.notify(payload, "opencode")
        self.assertEqual([], self.executions())

    def test_idle_and_status_do_not_double_publish_the_same_transition(self):
        payload = self.payload("session.status", "evt_status")
        payload["event"]["properties"]["status"] = {"type": "idle"}
        self.notify(payload, "opencode")
        self.assertEqual([], self.executions())
        self.notify(self.payload("session.idle", "evt_idle"), "opencode")
        row = self.assert_notice("ended", "opencode")
        self.assertEqual("evt_idle", row["source_binding"]["native_event_id"])
        self.assertIsNone(row["source_binding"]["native_prompt_id"])
        self.assertIsNone(row["source_binding"]["native_timestamp"])
