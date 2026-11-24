"""Usage is provider testimony bound to a native dispatch, never invented zero."""
from copy import deepcopy
import unittest

from tests import test_adhoc_codex, test_adhoc_claude, test_adhoc_pi


def fixture(owner, kind):
    case = kind()
    owner.addCleanup(case.doCleanups)
    case.setUp()
    return case


class AdhocUsageTests(unittest.TestCase):
    def test_codex_cumulative_reports_replace_instead_of_adding(self):
        case = fixture(self, test_adhoc_codex.AdhocCodexTests)
        failure, _ = case.capture(mode="usage")
        self.assertIsNone(failure)
        usage = case.events[-1].get("usage")
        self.assertIsNotNone(usage, "provider token testimony must survive the native terminal")
        self.assertEqual("thread_cumulative", usage["scope"])
        self.assertEqual("thread/tokenUsage/updated.tokenUsage.total", usage["source"])
        self.assertEqual({"input_tokens": 140, "output_tokens": 20,
            "cache_read_input_tokens": 40, "reasoning_output_tokens": 5}, usage["tokens"])

    def test_claude_terminal_usage_retains_cache_categories(self):
        case = fixture(self, test_adhoc_claude.AdhocClaudeTests)
        result = case.run_adapter(mode="usage")
        self.assertEqual("ok", result["code"])
        usage = result["events"][-1].get("usage")
        self.assertIsNotNone(usage, "Claude usage must be bound to its authenticated terminal")
        self.assertEqual("dispatch", usage["scope"])
        self.assertEqual("result.usage", usage["source"])
        self.assertEqual({"input_tokens": 140, "output_tokens": 20,
            "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}, usage["tokens"])

    def test_pi_counts_final_assistant_messages_once(self):
        case = fixture(self, test_adhoc_pi.AdhocPiTests)
        result = case.run_adapter(mode="usage")
        self.assertEqual("ok", result["code"])
        usage = result["events"][-1].get("usage")
        self.assertIsNotNone(usage, "Pi provider usage must survive settlement and cleanup")
        self.assertEqual("dispatch", usage["scope"])
        self.assertEqual("message_end.message.usage", usage["source"])
        self.assertEqual({"input_tokens": 140, "output_tokens": 20,
            "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}, usage["tokens"])

    def test_pi_startup_history_cannot_change_current_dispatch_usage(self):
        for mode in ("usage-startup", "usage-startup-missing"):
            with self.subTest(mode=mode):
                case = fixture(self, test_adhoc_pi.AdhocPiTests)
                result = case.run_adapter(mode=mode)
                self.assertEqual("ok", result["code"])
                usage = result["events"][-1].get("usage")
                self.assertIsNotNone(usage, "pre-prompt omission must not suppress current usage")
                self.assertEqual({"input_tokens": 140, "output_tokens": 20,
                    "cache_read_input_tokens": 40, "cache_creation_input_tokens": 10}, usage["tokens"])

    def test_invalid_provider_numbers_cannot_produce_success(self):
        for adapter, kind in (("codex", test_adhoc_codex.AdhocCodexTests),
                              ("claude", test_adhoc_claude.AdhocClaudeTests),
                              ("pi", test_adhoc_pi.AdhocPiTests)):
            with self.subTest(adapter=adapter):
                case = fixture(self, kind)
                if adapter == "codex":
                    code, _ = case.capture(mode="usage-invalid")
                    events = case.events
                else:
                    result = case.run_adapter(mode="usage-invalid")
                    code, events = result["code"], result["events"]
                self.assertEqual("protocol_error", code)
                self.assertFalse(any(event["event"] == "terminal" for event in events))

    def test_foreign_codex_usage_cannot_be_attached_to_this_dispatch(self):
        case = fixture(self, test_adhoc_codex.AdhocCodexTests)
        code, _ = case.capture(mode="usage-foreign")
        self.assertEqual("native_session_mismatch", code)
        self.assertFalse(any(event["event"] == "terminal" for event in case.events))

    def test_missing_usage_is_absent_from_legacy_terminal(self):
        # Existing providers remain readable; absence must never become a zero.
        case = fixture(self, test_adhoc_codex.AdhocCodexTests)
        code, _ = case.capture()
        self.assertIsNone(code)
        self.assertNotIn("usage", case.events[-1])

    def test_usage_metadata_is_closed_bounded_and_provider_specific(self):
        from floati.adhoc_records import validate_lifecycle_evidence
        from floati.errors import ProtocolRefusal
        from floati.native_thread_protocol import validate_native_event
        case = fixture(self, test_adhoc_codex.AdhocCodexTests)
        self.assertIsNone(case.capture(mode="usage")[0])
        event = case.events[-1]
        coordinate = event["provider_coordinate"]
        validate_native_event(case.context, event, adapter_kind="codex",
            opened_coordinate=coordinate, terminal_seen=False)
        validate_lifecycle_evidence("provider_terminal", {"event": event})
        for change in ({"scope": "dispatch"}, {"source": "result.usage"},
                       {"status": "estimated"}, {"transcript": "private"},
                       {"tokens": {"input_tokens": 1, "output_tokens": True}},
                       {"tokens": {"input_tokens": 1, "output_tokens": 2**63}},
                       {"tokens": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 2}}):
            with self.subTest(change=change):
                changed = deepcopy(event)
                changed["usage"].update(change)
                with self.assertRaises(ProtocolRefusal):
                    validate_native_event(case.context, changed, adapter_kind="codex",
                        opened_coordinate=coordinate, terminal_seen=False)
                with self.assertRaises(ProtocolRefusal):
                    validate_lifecycle_evidence("provider_terminal", {"event": changed})
