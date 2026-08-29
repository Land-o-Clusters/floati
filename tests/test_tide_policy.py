from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema


class TidePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet-alpha", create=True
        )
        self.registry = Registry(self.root)
        self.registry.register("architect", "architect")
        self.registry.register("lane-codex", "codex")
        self.registry.register("lane-cursor", "cursor")
        self.registry.register("lane-claude", "claude")
        self.registry.register("lane-opencode", "opencode")
        self.registry.register("lane-cline", "cline")
        self.registry.register("lane-grok", "grok-build")

    def ledger(self):
        from floati.tide_policy import TidePolicyLedger

        return TidePolicyLedger(self.root)

    def test_policy_is_optional_and_absent_by_default(self) -> None:
        self.assertIsNone(self.ledger().show("lane-codex"))
        self.assertFalse(
            self.root.resolve_relative("receipts/tide-policy/lane-codex.jsonl").exists()
        )

    def test_set_show_clear_is_receipted_and_idempotent(self) -> None:
        ledger = self.ledger()
        first = ledger.set(
            "lane-codex", "context_fraction", "70%", "direct",
            idempotency_key="policy-set",
        )
        retry = ledger.set(
            "lane-codex", "context_fraction", "70%", "direct",
            idempotency_key="policy-set",
        )
        self.assertEqual(first, retry)
        self.assertEqual("0.700000", first["threshold"])
        self.assertEqual("active", ledger.show("lane-codex")["state"])
        cleared = ledger.clear("lane-codex", idempotency_key="policy-clear")
        self.assertEqual(first["id"], cleared["predecessor_policy_id"])
        self.assertIsNone(ledger.show("lane-codex"))
        validate_json_schema(first, Path("schemas/v0/tide-policy-record.schema.json"))

    def test_policy_can_be_replaced_after_clear_with_cleared_predecessor(self) -> None:
        ledger = self.ledger()
        first = ledger.set(
            "lane-codex", "context_fraction", "70%", "recommend",
            idempotency_key="first-policy",
        )
        cleared = ledger.clear("lane-codex", idempotency_key="clear-policy")

        replacement = ledger.set(
            "lane-codex", "context_fraction", "80%", "direct",
            idempotency_key="replacement-policy",
        )

        self.assertEqual(cleared["id"], replacement["predecessor_policy_id"])
        self.assertEqual(replacement, ledger.by_id("lane-codex", replacement["id"]))
        self.assertNotEqual(first["id"], replacement["id"])

    def test_policy_refuses_metric_not_authorized_for_recorded_harness(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger().set(
                "lane-cursor", "context_fraction", "0.7", "recommend",
                idempotency_key="cursor-percent",
            )
        self.assertEqual("tide_metric_not_derivable", caught.exception.code)
        self.assertIn("T1-tide-survey.md", caught.exception.detail)

    def test_unknown_metric_and_compact_action_refuse_without_writes(self) -> None:
        path = self.root.resolve_relative("receipts/tide-policy/lane-codex.jsonl")
        for metric, action, code in (
            ("invented", "recommend", "tide_metric_not_derivable"),
            ("context_fraction", "compact", "tide_action_not_supported"),
        ):
            with self.subTest(metric=metric, action=action):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.ledger().set(
                        "lane-codex", metric, "0.7", action,
                        idempotency_key=f"bad-{metric}-{action}",
                    )
                self.assertEqual(code, caught.exception.code)
        self.assertFalse(path.exists())

    def test_catalog_is_closed_and_names_formula_and_source(self) -> None:
        from floati.tide_catalog import metric_for

        metric = metric_for("codex", "context_fraction")
        self.assertEqual("A", metric.access_class)
        self.assertEqual("DERIVED", metric.stamp)
        self.assertIn("last_token_usage.total_tokens", metric.formula)
        self.assertIn("T1-depth2.md", metric.receipt_path)
        cursor = metric_for("cursor", "transcript_bytes")
        self.assertEqual("proxy_bytes", cursor.value_kind)

    def test_cline_metrics_without_t1_authority_are_not_in_the_catalog(self) -> None:
        from floati.tide_catalog import metric_for

        for name in ("run_context_fraction", "self_reported_context_fraction"):
            with self.subTest(metric=name):
                with self.assertRaises(ProtocolRefusal) as caught:
                    metric_for("cline", name)
                self.assertEqual("tide_metric_not_derivable", caught.exception.code)
                self.assertIn("T1-tide-survey.md", caught.exception.detail)

    def test_policy_refuses_catalog_metrics_without_a_shipped_daemon_evaluator(self) -> None:
        for node, metric in (
            ("lane-claude", "context_fraction"),
            ("lane-opencode", "session_tokens"),
            ("lane-grok", "message_count"),
        ):
            with self.subTest(node=node):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.ledger().set(
                        node, metric, "1", "recommend",
                        idempotency_key=f"unavailable-{node}",
                    )
                self.assertEqual("tide_evaluator_unavailable", caught.exception.code)
        for node in ("lane-claude", "lane-opencode", "lane-grok"):
            self.assertFalse(
                self.root.resolve_relative(f"receipts/tide-policy/{node}.jsonl").exists()
            )

    def test_tiny_positive_fraction_that_canonicalizes_to_zero_is_refused(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger().set(
                "lane-codex", "context_fraction", "0.0000001", "recommend",
                idempotency_key="tiny-threshold",
            )
        self.assertEqual("tide_threshold_invalid", caught.exception.code)

    def test_class_b_policy_requires_node_self_reported_testimony(self) -> None:
        from floati.tide_policy import TideTestimonyLedger

        policy = self.ledger().set(
            "lane-cursor", "self_reported_context_fraction", "70%", "recommend",
            idempotency_key="cursor-class-b",
        )
        self.assertEqual("B", policy["access_class"])
        testimony = TideTestimonyLedger(self.root).record(
            "lane-cursor", "self_reported_context_fraction", "75%", "/context",
            idempotency_key="cursor-testimony",
        )
        self.assertEqual("SELF_REPORTED", testimony["stamp"])
        self.assertEqual("lane-cursor", testimony["node_id"])
        validate_json_schema(
            testimony, Path("schemas/v0/tide-testimony-record.schema.json")
        )


if __name__ == "__main__":
    unittest.main()
