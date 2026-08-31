from __future__ import annotations

from floati import fixture_ids

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from floati.events import EventLog
from floati.quota import QuotaFact, QuotaLedger, QuotaReceipt, QuotaState
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.tide import TideEvaluator, TideReading
from floati.tide_catalog import metric_for
from floati.tide_policy import TidePolicyLedger
from tests.schema_validation import validate_json_schema


SOURCE_SHA = "f2b587634cfc6d6a52cc24bd02bfd978919c359b"
OBSERVED_AT = "2026-08-29T12:00:00Z"
HARNESS_PROVIDERS = (
    ("claude", "anthropic_claude_code"),
    ("codex", "openai_codex"),
    ("gemini", "google_gemini"),
    ("cursor", "cursor_individual"),
    ("grok-build", "xai_grok"),
    ("github-copilot", "github_copilot"),
)


class _FailingDelegate:
    def read(self, binding, metric):
        raise AssertionError("quota reads must not touch the exact-session delegate")


class _IdentityDelegate:
    def __init__(self, reading: TideReading) -> None:
        self.reading = reading
        self.calls = 0

    def read(self, binding, metric):
        self.calls += 1
        return self.reading


class QuotaTideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "quota-tide-fleet", create=True
        )
        Registry(self.root).register("architect", "architect")

    def quota_tide(self):
        try:
            from floati.tide import QuotaAwareTideReader
            from floati.tide_catalog import provider_for_harness
        except ImportError:
            self.fail("V4 Tide quota integration must exist")
        return QuotaAwareTideReader, provider_for_harness

    def register(self, node: str, harness: str) -> SimpleNamespace:
        Registry(self.root).register(node, harness)
        return SimpleNamespace(
            node_id=node,
            harness=harness,
            session_id=node + "-session",
            workspace=Path(self.temporary.name),
        )

    def append_fact(
        self,
        provider: str,
        *,
        state: QuotaState,
        stamp: str,
        key: str,
    ) -> dict:
        fact = QuotaFact(
            provider=provider,
            surface="account_quota",
            state=state,
            stamp=stamp,
            source="https://provider.example/quota-contract",
            evidence_digest=hashlib.sha256(key.encode()).hexdigest(),
            observed_at=OBSERVED_AT,
            resets_at="2026-08-30T12:00:00Z" if state.kind == "consumed_fraction" else None,
        )
        receipt = QuotaReceipt.create(
            provider=provider,
            endpoint_id="quota-fixture",
            facts=(fact,),
            idempotency_key=key,
        )
        return QuotaLedger(self.root).append(receipt)

    def evaluator(self):
        QuotaAwareTideReader, _ = self.quota_tide()
        return TideEvaluator(
            self.root,
            reader=QuotaAwareTideReader(self.root, _FailingDelegate()),
            source_sha=SOURCE_SHA,
        )

    def test_exact_six_harness_provider_mappings_admit_quota_policy(self) -> None:
        _, provider_for_harness = self.quota_tide()
        for index, (harness, provider) in enumerate(HARNESS_PROVIDERS):
            with self.subTest(harness=harness):
                node = fixture_ids.builder(str(index))
                self.register(node, harness)
                self.assertEqual(provider, provider_for_harness(harness))
                metric = metric_for(harness, "quota_fraction")
                self.assertEqual(("fraction", "MEASURED_OR_DERIVED"), (metric.value_kind, metric.stamp))
                policy = TidePolicyLedger(self.root).set(
                    node,
                    "quota_fraction",
                    "70%",
                    "recommend",
                    idempotency_key="quota-policy-" + str(index),
                )
                self.assertEqual(1, policy["schema_version"])
                self.assertEqual("MEASURED_OR_DERIVED", policy["stamp"])
                validate_json_schema(policy, Path("schemas/v1/tide-policy-record.schema.json"))

    def test_measured_and_derived_fraction_crossings_preserve_receipt_evidence(self) -> None:
        for index, stamp in enumerate(("MEASURED", "DERIVED")):
            with self.subTest(stamp=stamp):
                node = "quota-cross-" + str(index)
                binding = self.register(node, "codex")
                policy = TidePolicyLedger(self.root).set(
                    node,
                    "quota_fraction",
                    "70%",
                    "recommend",
                    idempotency_key="policy-cross-" + str(index),
                )
                quota_row = self.append_fact(
                    "openai_codex",
                    state=QuotaState("consumed_fraction", "0.8"),
                    stamp=stamp,
                    key="quota-cross-" + str(index),
                )

                result = self.evaluator().evaluate(node, binding)

                self.assertEqual("crossed", result["state"])
                receipt = result["receipt"]
                self.assertEqual(1, receipt["schema_version"])
                self.assertEqual(stamp, receipt["stamp"])
                self.assertEqual(policy["formula"], receipt["formula"])
                self.assertIn("quota-receipt:" + quota_row["id"], receipt["sources"])
                self.assertIn(
                    "quota-source:https://provider.example/quota-contract",
                    receipt["sources"],
                )
                self.assertIn(
                    "quota-evidence-sha256:" + quota_row["facts"][0]["evidence_digest"],
                    receipt["sources"],
                )
                validate_json_schema(receipt, Path("schemas/v1/tide-receipt-record.schema.json"))

    def test_simultaneous_windows_bind_the_highest_consumed_fraction(self) -> None:
        node = "quota-multi-window"
        binding = self.register(node, "claude")
        TidePolicyLedger(self.root).set(
            node, "quota_fraction", "70%", "recommend",
            idempotency_key="policy-multi-window",
        )
        facts = tuple(
            QuotaFact(
                provider="anthropic_claude_code",
                surface=surface,
                state=QuotaState("consumed_fraction", value),
                stamp="MEASURED",
                source="https://provider.example/quota-contract#" + surface,
                evidence_digest=hashlib.sha256(surface.encode()).hexdigest(),
                observed_at=OBSERVED_AT,
                resets_at="2026-08-30T12:00:00Z",
            )
            for surface, value in (
                ("rate_limits.five_hour", "0.9"),
                ("rate_limits.seven_day", "0.4"),
            )
        )
        QuotaLedger(self.root).append(QuotaReceipt.create(
            provider="anthropic_claude_code",
            endpoint_id="claude-statusline",
            facts=facts,
            idempotency_key="quota-multi-window",
        ))

        result = self.evaluator().evaluate(node, binding)

        self.assertEqual("crossed", result["state"])
        self.assertEqual("0.900000", result["receipt"]["value"])
        self.assertTrue(any(
            source.endswith("#rate_limits.five_hour")
            for source in result["receipt"]["sources"]
        ))

    def test_estimate_refuses_before_any_tide_receipt_or_envelope(self) -> None:
        binding = self.register("quota-estimate", "codex")
        TidePolicyLedger(self.root).set(
            "quota-estimate", "quota_fraction", "70%", "recommend",
            idempotency_key="policy-estimate",
        )
        self.append_fact(
            "openai_codex",
            state=QuotaState("consumed_fraction", "0.9"),
            stamp="ESTIMATE",
            key="quota-estimate",
        )

        result = self.evaluator().evaluate("quota-estimate", binding)

        self.assertEqual("quota_refused", result["state"])
        self.assertEqual("quota_fact_not_evaluable", result["reason"]["code"])
        self.assertIn("stamp=ESTIMATE", result["reason"]["detail"])
        self.assertEqual([], self.evaluator()._rows("quota-estimate"))
        self.assertEqual([], EventLog(self.root).records())

    def test_unknown_is_quiet_across_repeated_daemon_cycles(self) -> None:
        binding = self.register("quota-unknown", "cursor")
        TidePolicyLedger(self.root).set(
            "quota-unknown", "quota_fraction", "70%", "recommend",
            idempotency_key="policy-unknown",
        )
        self.append_fact(
            "cursor_individual",
            state=QuotaState("unknown", None),
            stamp="DERIVED",
            key="quota-unknown",
        )
        evaluator = self.evaluator()

        states = tuple(
            evaluator.evaluate("quota-unknown", binding)["state"] for _ in range(3)
        )

        self.assertEqual(("quota_unknown",) * 3, states)
        self.assertEqual([], evaluator._rows("quota-unknown"))
        self.assertEqual([], EventLog(self.root).records())

    def test_absent_quota_receipt_is_the_same_quiet_unknown(self) -> None:
        binding = self.register("quota-absent", "github-copilot")
        TidePolicyLedger(self.root).set(
            "quota-absent", "quota_fraction", "70%", "recommend",
            idempotency_key="policy-absent",
        )

        result = self.evaluator().evaluate("quota-absent", binding)

        self.assertEqual("quota_unknown", result["state"])
        self.assertEqual("quota_fact_unknown", result["reason"]["code"])
        self.assertEqual([], EventLog(self.root).records())

    def test_non_quota_metric_delegates_the_exact_reading_unchanged(self) -> None:
        QuotaAwareTideReader, _ = self.quota_tide()
        metric = metric_for("codex", "context_fraction")
        expected = TideReading(
            metric=metric.name,
            value="0.5",
            stamp=metric.stamp,
            access_class=metric.access_class,
            formula=metric.formula,
            sources=(metric.receipt_path, "session:fixture"),
        )
        delegate = _IdentityDelegate(expected)

        actual = QuotaAwareTideReader(self.root, delegate).read(
            SimpleNamespace(harness="codex"), metric
        )

        self.assertIs(expected, actual)
        self.assertEqual(1, delegate.calls)

    def test_quota_commands_are_explicit_and_shipped_help_carries_no_draft_marker(self) -> None:
        from floati.cli import _parser
        from floati.helptext import help_for

        collect = _parser().parse_args([
            "quota", "collect", "--root", "\x2fprivate\x2ftmp/fleet",
            "--provider", "openai_codex", "--observed-at", OBSERVED_AT,
            "--idempotency-key", "quota-cli-1", "--executable", "/usr/bin/codex",
        ])
        show = _parser().parse_args([
            "quota", "show", "--root", "\x2fprivate\x2ftmp/fleet",
            "--provider", "openai_codex",
        ])

        self.assertTrue(callable(collect.handler))
        self.assertTrue(callable(show.handler))
        for argv in (("quota", "--help"), ("quota", "collect", "--help"), ("quota", "show", "--help")):
            page = help_for(argv)
            self.assertIsNotNone(page)
            # reviewer voice pass 2026-08-29 restamped the quota pages; shipped
            # help must never carry a provenance marker after its pass.
            self.assertNotIn("DRAFT -", page)

    def test_unknown_provider_collect_and_show_handlers_round_trip_typed_truth(self) -> None:
        from floati.cli import _parser

        absent = _parser().parse_args([
            "quota", "show", "--root", str(self.root.path),
            "--provider", "cursor_individual",
        ])
        status, evidence, code = absent.handler(absent)
        self.assertEqual(("no_result", 32, None), (status, code, evidence["receipt"]))

        collect = _parser().parse_args([
            "quota", "collect", "--root", str(self.root.path),
            "--provider", "cursor_individual", "--observed-at", OBSERVED_AT,
            "--idempotency-key", "cursor-cli-1",
        ])
        status, evidence, code = collect.handler(collect)
        self.assertEqual(("ok", 0, "cursor_individual"), (status, code, evidence["provider"]))
        self.assertEqual(
            {"kind": "unknown", "value": None},
            evidence["receipt"]["facts"][0]["state"],
        )

        shown = _parser().parse_args([
            "quota", "show", "--root", str(self.root.path),
            "--provider", "cursor_individual",
        ])
        status, shown_evidence, code = shown.handler(shown)
        self.assertEqual(("ok", 0), (status, code))
        self.assertEqual(evidence["receipt"], shown_evidence["receipt"])


if __name__ == "__main__":
    unittest.main()
