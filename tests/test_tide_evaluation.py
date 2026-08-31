from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.tide_catalog import metric_for
from floati.tide_policy import TidePolicyLedger
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


class _Reader:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def read(self, binding, metric):
        from floati.tide import TideReading

        self.calls += 1
        return TideReading(
            metric=metric.name,
            value=self.value,
            stamp=metric.stamp,
            access_class=metric.access_class,
            formula=metric.formula,
            sources=(metric.receipt_path, f"session:{binding.session_id}"),
        )


class TideEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet-alpha", create=True
        )
        registry = Registry(self.root)
        registry.register("architect", "architect")
        registry.register(public_ids.builder('codex'), "codex")
        self.binding = SimpleNamespace(
            harness="codex", session_id="session-1",
            workspace=Path(self.temporary.name),
        )

    def evaluator(self, reader: _Reader):
        from floati.tide import TideEvaluator

        return TideEvaluator(
            self.root,
            reader=reader,
            turnover_projector=lambda node: {
                "kind": "context_turnover_projection",
                "node_id": node,
                "role_provenance": {"role_record_id": "registry-role-fixture"},
                "steps": [
                    {"kind": "teardown_projection", "argv": ["floati", "prep-clear"]},
                    {"kind": "state_flush_receipt", "argv": ["floati", "state-flush"]},
                    {"kind": "boot_projection", "argv": ["floati", "boot"]},
                ],
            },
            source_sha="f2b587634cfc6d6a52cc24bd02bfd978919c359b",
        )

    def policy(self, action: str = "recommend") -> dict:
        return TidePolicyLedger(self.root).set(
            public_ids.builder('codex'), "context_fraction", "70%", action,
            idempotency_key=f"policy-{action}",
        )

    def test_no_policy_performs_zero_metric_reads(self) -> None:
        reader = _Reader("0.9")
        result = self.evaluator(reader).evaluate(public_ids.builder('codex'), self.binding)
        self.assertEqual("off", result["state"])
        self.assertEqual(0, reader.calls)

    def test_crossing_is_receipted_once_and_rearms_below_threshold(self) -> None:
        self.policy()
        reader = _Reader("0.8")
        evaluator = self.evaluator(reader)
        first = evaluator.evaluate(public_ids.builder('codex'), self.binding)
        repeated = evaluator.evaluate(public_ids.builder('codex'), self.binding)
        self.assertEqual("crossed", first["state"])
        self.assertEqual("above", repeated["state"])
        self.assertEqual("DERIVED", first["receipt"]["stamp"])
        validate_json_schema(
            first["receipt"], Path("schemas/v0/tide-receipt-record.schema.json")
        )
        self.assertEqual(metric_for("codex", "context_fraction").formula, first["receipt"]["formula"])
        notices = [row for row in EventLog(self.root).records() if row["recipient"] == "architect"]
        self.assertEqual(1, len(notices))
        self.assertIn("TIDE NOTICE", notices[0]["note"])
        reader.value = "0.6"
        self.assertEqual("rearmed", evaluator.evaluate(public_ids.builder('codex'), self.binding)["state"])
        reader.value = "0.8"
        self.assertEqual("crossed", evaluator.evaluate(public_ids.builder('codex'), self.binding)["state"])
        self.assertEqual(2, len([row for row in EventLog(self.root).records() if row["recipient"] == "architect"]))
        projected = evaluator.projection(public_ids.builder('codex'))
        self.assertEqual("DERIVED", projected["last_reading"]["stamp"])
        self.assertEqual("0.8", projected["last_reading"]["value"])
        self.assertEqual("active", projected["policy"]["state"])

    def test_direct_sends_recipe_and_holds_until_newer_state_flush(self) -> None:
        self.policy("direct")
        reader = _Reader("0.8")
        evaluator = self.evaluator(reader)
        crossed = evaluator.evaluate(public_ids.builder('codex'), self.binding)
        envelope = [row for row in EventLog(self.root).records() if row["recipient"] == public_ids.builder('codex')][0]
        self.assertIn("TURNOVER DIRECTIVE", envelope["note"])
        self.assertIn("prep-clear", envelope["note"])
        self.assertTrue(evaluator.dispatch_held(public_ids.builder('codex')))
        self.assertEqual("directed", evaluator.status(public_ids.builder('codex'))["turnover_state"])
        self.assertEqual("held", evaluator.evaluate(public_ids.builder('codex'), self.binding)["state"])
        self.assertEqual(1, reader.calls)
        self.assertEqual(
            1,
            len([row for row in EventLog(self.root).records() if row["recipient"] == public_ids.builder('codex')]),
        )
        flush = {
            "schema_version": 0,
            "id": "node-state-flush-018f7e9b3c147abc8def0123456789ab",
            "tenant_id": self.root.tenant_id,
            "timestamp": "2099-01-01T00:00:00.000Z",
            "kind": "node_state_flush_receipt",
            "node_id": public_ids.builder('codex'),
            "state_file": str(self.root.path / public_ids.compose('nodes/', public_ids.builder('codex'), '/STATE.md')),
            "operation": "flush",
            "observed_mtime_ns": 2,
            "observed_size_bytes": 1,
            "prior_mtime_ns": 1,
        }
        completion = evaluator.observe_state_flush(flush)
        self.assertEqual(
            crossed["receipt"]["crossing_receipt_id"],
            completion["crossing_receipt_id"],
        )
        self.assertFalse(evaluator.dispatch_held(public_ids.builder('codex')))
        self.assertEqual("state_flushed", evaluator.status(public_ids.builder('codex'))["turnover_state"])

    def test_directed_turnover_blocks_policy_clear_or_replacement(self) -> None:
        self.policy("direct")
        evaluator = self.evaluator(_Reader("0.8"))
        evaluator.evaluate(public_ids.builder('codex'), self.binding)
        policies = TidePolicyLedger(self.root)

        with self.assertRaises(ProtocolRefusal) as clear_error:
            policies.clear(public_ids.builder('codex'), idempotency_key="clear-while-directed")
        self.assertEqual("tide_directive_active", clear_error.exception.code)

        with self.assertRaises(ProtocolRefusal) as set_error:
            policies.set(
                public_ids.builder('codex'), "context_fraction", "80%", "recommend",
                idempotency_key="replace-while-directed",
            )
        self.assertEqual("tide_directive_active", set_error.exception.code)
        self.assertEqual("directed", evaluator.status(public_ids.builder('codex'))["turnover_state"])

    def test_fleet_status_names_policy_and_directive_side_of_flush_line(self) -> None:
        from datetime import datetime, timezone
        from floati.projection import FleetProjection

        self.policy("direct")
        self.evaluator(_Reader("0.8")).evaluate(public_ids.builder('codex'), self.binding)
        snapshot = FleetProjection(self.root).snapshot(datetime.now(timezone.utc))
        node = next(row for row in snapshot["nodes"] if row["node_id"] == public_ids.builder('codex'))
        self.assertEqual(
            {"policy": "active", "turnover_state": "directed"},
            node["tide"],
        )

    def test_restart_recovers_action_receipt_after_envelope_append(self) -> None:
        self.policy("recommend")
        evaluator = self.evaluator(_Reader("0.8"))
        original = evaluator._record
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected action-receipt failure")
            return original(*args, **kwargs)

        with mock.patch.object(evaluator, "_record", side_effect=fail_second):
            with self.assertRaises(OSError):
                evaluator.evaluate(public_ids.builder('codex'), self.binding)
        recovered = self.evaluator(_Reader("0.8")).evaluate(public_ids.builder('codex'), self.binding)
        self.assertEqual("crossed", recovered["state"])
        self.assertEqual(
            1,
            len([row for row in EventLog(self.root).records() if row["recipient"] == "architect"]),
        )

    def test_restart_completes_a_crossing_even_if_pressure_falls_before_recovery(self) -> None:
        self.policy("recommend")
        evaluator = self.evaluator(_Reader("0.8"))
        original = evaluator._record
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected action-receipt failure")
            return original(*args, **kwargs)

        with mock.patch.object(evaluator, "_record", side_effect=fail_second):
            with self.assertRaises(OSError):
                evaluator.evaluate(public_ids.builder('codex'), self.binding)

        recovered = self.evaluator(_Reader("0.6")).evaluate(
            public_ids.builder('codex'), self.binding
        )

        self.assertEqual("crossed", recovered["state"])
        self.assertEqual(
            1,
            len([row for row in EventLog(self.root).records() if row["recipient"] == "architect"]),
        )
        self.assertEqual(
            "rearmed",
            self.evaluator(_Reader("0.6")).evaluate(public_ids.builder('codex'), self.binding)["state"],
        )


if __name__ == "__main__":
    unittest.main()
