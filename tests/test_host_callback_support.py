"""A documented callback is a contract capability, never live testimony."""
import unittest

from floati.host_callback_support import desktop_callback_support


class DesktopCallbackSupportTests(unittest.TestCase):
    def test_predecision_and_blockable_stop_do_not_claim_desktop_outcomes(self):
        support = desktop_callback_support("Codex")
        for outcome in ("pending_approval", "ended_turn"):
            with self.subTest(outcome=outcome):
                row = support[outcome]
                self.assertEqual("UNKNOWN", row["state"])
                self.assertEqual("codex_" + outcome + "_callback_absent", row["reason"])
                self.assertTrue(row["detail"])

    def test_supported_notifications_require_native_prompt_identity(self):
        for harness, outcome, state, event, identity in (
                ("Claude Code", "pending_approval", "SUPPORTED", "Notification", "prompt_id"),
                ("Claude Code", "ended_turn", "SUPPORTED", "Notification", "prompt_id"),
                ("grok-build", "pending_approval", "SUPPORTED", "Notification", "promptId"),
                ("GROK-BUILD", "ended_turn", "SUPPORTED", "Notification", "timestamp"),
                ("CURSOR", "pending_approval", "UNKNOWN", None, None),
                ("CURSOR", "ended_turn", "SUPPORTED", "stop", None),
                ("OpenCode", "pending_approval", "SUPPORTED", "permission.asked", "event.id"),
                ("OpenCode", "ended_turn", "SUPPORTED", "session.idle", "event.id")):
            with self.subTest(case=(harness, outcome)):
                row = desktop_callback_support(harness)[outcome]
                self.assertEqual(state, row["state"])
                self.assertEqual(event, row.get("event"))
                self.assertEqual(identity, row.get("required_identity"))
                self.assertTrue(row["source"].startswith("https://"))
                if harness == "CURSOR" and outcome == "pending_approval":
                    self.assertEqual("cursor_approval_callback_absent", row["reason"])
        self.assertEqual("idle_prompt", desktop_callback_support("claude")["ended_turn"]["notification_type"])
        self.assertEqual("permission_prompt", desktop_callback_support("grok-build")["pending_approval"]["notification_type"])
        self.assertEqual("idle_prompt", desktop_callback_support("GROK-BUILD")["ended_turn"]["notification_type"])

    def test_unavailable_hosts_are_explicit_and_do_not_borrow_another_contract(self):
        for harness in ("zcode", "pi", "unlisted-host"):
            with self.subTest(harness=harness):
                support = desktop_callback_support(harness)
                self.assertEqual(harness, support["harness"])
                for outcome in ("pending_approval", "ended_turn"):
                    row = support[outcome]
                    self.assertEqual("UNKNOWN", row["state"])
                    self.assertIn(harness, row["reason"])
                    self.assertNotIn("event", row)

    def test_casefold_aliases_share_the_contract_without_shared_mutable_results(self):
        self.assertEqual(desktop_callback_support("Claude Code"), desktop_callback_support("cLaUdE"))
        first = desktop_callback_support("codex")
        first["pending_approval"]["state"] = "SUPPORTED"
        self.assertEqual("UNKNOWN", desktop_callback_support("CODEX")["pending_approval"]["state"])


if __name__ == "__main__":
    unittest.main()
