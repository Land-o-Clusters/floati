from __future__ import annotations

import io
import json
import shlex
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.node_wizard import NodeAddPlan, NodeRetirePlan, NodeWizard
from floati.root import FloatiRoot


class RecordingBackend:
    def __init__(self, preview_stream: io.StringIO) -> None:
        self.preview_stream = preview_stream
        self.added: Optional[NodeAddPlan] = None
        self.retired: Optional[NodeRetirePlan] = None
        self.node = {
            "schema_version": 0,
            "id": "registry-" + "1" * 32,
            "tenant_id": "fleet",
            "timestamp": "2026-08-27T20:00:00.000Z",
            "kind": "registry_entry",
            "node_id": "alpha",
            "role": "Codex",
            "state": "active",
        }
        self.lease: Optional[dict[str, Any]] = None

    def active_node(self, node_id: str) -> dict[str, Any]:
        if node_id != self.node["node_id"]:
            raise ProtocolRefusal("unknown_node", "node is not active")
        return dict(self.node)

    def active_lease(self, node_id: str) -> Optional[dict[str, Any]]:
        return None if self.lease is None else dict(self.lease)

    def commit_add(self, plan: NodeAddPlan) -> dict[str, Any]:
        self._assert_previews_precede_mutation(plan.records)
        self.added = plan
        return {"records": list(plan.records)}

    def commit_retire(self, plan: NodeRetirePlan) -> dict[str, Any]:
        self._assert_previews_precede_mutation(plan.records)
        self.retired = plan
        return {"records": list(plan.records)}

    def _assert_previews_precede_mutation(self, records: tuple[dict[str, Any], ...]) -> None:
        previews = [
            json.loads(line.removeprefix("ledger preview: "))
            for line in self.preview_stream.getvalue().splitlines()
            if line.startswith("ledger preview: ")
        ]
        if previews[-len(records):] != list(records):
            raise AssertionError("exact record previews must be flushed before commit")


class FlushTrackingOutput(io.StringIO):
    """Test-only stream proving preview visibility precedes the backend mutation."""

    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


class NodeWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.output = io.StringIO()
        self.backend = RecordingBackend(self.output)
        ids = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32))
        self.wizard = NodeWizard(
            self.root,
            self.backend,
            id_factory=lambda: next(ids),
            now=lambda: datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc),
        )

    def test_temporary_add_previews_exact_records_before_commit_and_has_one_command_lifecycle(self) -> None:
        """Catches a wizard write before preview or a multi-step temporary lifecycle."""
        result = self.wizard.add_from_keys(
            ["alpha", "Codex", "temporary", "90"], self.output
        )

        plan = self.backend.added
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(2, len(plan.records))
        self.assertEqual("registry_entry", plan.records[0]["kind"])
        self.assertEqual("node_lease", plan.records[1]["kind"])
        self.assertEqual("2026-08-27T23:30:00.000Z", plan.records[1]["expires_at"])
        self.assertEqual(str(self.root.path / "nodes" / "alpha"), plan.workspace)
        self.assertEqual(1, len(plan.boot_command.splitlines()))
        self.assertEqual(1, len(plan.teardown_command.splitlines()))
        self.assertEqual(list(plan.records), result["records"])

        from floati.cli import _parser

        parser = _parser()
        boot = parser.parse_args(shlex.split(plan.boot_command)[1:])
        teardown = parser.parse_args(shlex.split(plan.teardown_command)[1:])
        self.assertEqual("boot", boot.node_command)
        self.assertEqual("retire", teardown.node_command)
        self.assertEqual("alpha", boot.node)
        self.assertEqual("alpha", teardown.node)

    def test_public_add_plan_preview_and_commit_preserve_one_exact_plan_object(self) -> None:
        """Catches preview/commit rebuilding node-add rows after the user reviewed them."""
        output = FlushTrackingOutput()
        self.backend.preview_stream = output
        plan = self.wizard.plan_add(["alpha", "Codex", "temporary", "90"])

        preview = self.wizard.render_add_preview(plan)
        result = self.wizard.commit_add(plan, output)

        expected = "\n".join(
            "ledger preview: "
            + json.dumps(
                dict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            for record in plan.records
        ) + "\n"
        self.assertEqual(expected, preview)
        self.assertEqual(expected, output.getvalue())
        self.assertGreaterEqual(output.flush_count, 1)
        self.assertIs(plan, self.backend.added)
        self.assertEqual(list(plan.records), result["records"])

    def test_previewed_plan_records_cannot_be_mutated_before_that_plan_commits(self) -> None:
        """Catches a caller changing a reviewed record before the same plan reaches the backend."""
        output = FlushTrackingOutput()
        self.backend.preview_stream = output
        plan = self.wizard.plan_add(["alpha", "Codex", "temporary", "90"])
        preview = self.wizard.render_add_preview(plan)

        with self.assertRaises(TypeError):
            plan.records[0]["node_id"] = "changed-after-preview"
        result = self.wizard.commit_add(plan, output)

        self.assertIn('"node_id":"alpha"', preview)
        self.assertEqual("alpha", self.backend.added.records[0]["node_id"])
        self.assertEqual("alpha", result["records"][0]["node_id"])

    def test_previewed_plan_rejects_direct_dict_base_method_mutation_before_commit(self) -> None:
        """Catches dict.__setitem__ bypassing the reviewed plan's immutability guard."""
        output = FlushTrackingOutput()
        self.backend.preview_stream = output
        plan = self.wizard.plan_add(["alpha", "Codex", "temporary", "90"])
        preview = self.wizard.render_add_preview(plan)

        with self.assertRaises(TypeError):
            dict.__setitem__(plan.records[0], "node_id", "changed-after-preview")
        result = self.wizard.commit_add(plan, output)

        self.assertIn('"node_id":"alpha"', preview)
        self.assertEqual("alpha", result["records"][0]["node_id"])

    def test_previewed_plan_rejects_backing_attribute_rebinding_before_commit(self) -> None:
        """Catches direct _items rebinding replacing the reviewed record before commit."""
        output = FlushTrackingOutput()
        self.backend.preview_stream = output
        plan = self.wizard.plan_add(["alpha", "Codex", "temporary", "90"])
        preview = self.wizard.render_add_preview(plan)

        with self.assertRaises(AttributeError):
            plan.records[0]._items = (("node_id", "changed-after-preview"),)
        result = self.wizard.commit_add(plan, output)

        self.assertIn('"node_id":"alpha"', preview)
        self.assertEqual("alpha", result["records"][0]["node_id"])

    def test_permanent_add_has_one_registry_record_and_no_lease_commands(self) -> None:
        """Catches permanent onboarding accidentally creating lease state."""
        self.wizard.add_from_keys(["alpha", "Codex", "permanent"], self.output)

        plan = self.backend.added
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(1, len(plan.records))
        self.assertIsNone(plan.boot_command)
        self.assertIsNone(plan.teardown_command)

    def test_add_offers_only_t1_metrics_for_the_selected_harness(self) -> None:
        result = self.wizard.add_from_keys(["alpha", "Codex", "permanent"], self.output)
        self.assertEqual(
            [
                "context_fraction",
                "transcript_bytes",
                "turn_count",
                "self_reported_context_fraction",
                "quota_fraction",
            ],
            result["tide_metrics"],
        )

    def test_plain_fallback_collects_the_same_semantic_plan_as_keyboard_input(self) -> None:
        """Catches the plain fallback silently omitting a wizard choice."""
        plain_input = io.StringIO("alpha\nCodex\ntemporary\n90\n")
        self.wizard.add_plain(plain_input, self.output)
        plain = self.backend.added

        self.assertIsNotNone(plain)
        assert plain is not None
        self.assertEqual("alpha", plain.node_id)
        self.assertEqual("Codex", plain.harness)
        self.assertEqual("temporary", plain.lifetime)
        self.assertEqual(90, plain.lease_minutes)

    def test_plain_add_offers_an_optional_harness_specific_tide_policy_step(self) -> None:
        plain_input = io.StringIO(
            "alpha\nCodex\npermanent\ncontext_fraction\n70%\ndirect\n"
        )
        policy = {"state": "active", "metric": "context_fraction"}

        with mock.patch(
            "floati.tide_policy.TidePolicyLedger.set", return_value=policy
        ) as set_policy:
            result = self.wizard.add_plain(plain_input, self.output)

        self.assertEqual(policy, result["tide_policy"])
        set_policy.assert_called_once_with(
            "alpha", "context_fraction", "70%", "direct",
            idempotency_key="wizard-tide-" + "b" * 32,
        )
        self.assertIn("tide metric", self.output.getvalue())

    def test_invalid_node_refuses_before_backend_lookup_or_commit(self) -> None:
        """Catches the wizard deferring grammar validation until mutation."""
        with self.assertRaises(ProtocolRefusal) as raised:
            self.wizard.add_from_keys(
                ["../escape", "Codex", "permanent"], self.output
            )

        self.assertEqual("node_invalid", raised.exception.code)
        self.assertIsNone(self.backend.added)
        self.assertEqual("", self.output.getvalue())

    def test_retire_previews_the_exact_existing_role_and_retains_workspace(self) -> None:
        """Catches teardown guessing the role or promising workspace deletion."""
        result = self.wizard.retire_from_keys(["alpha"], self.output)

        plan = self.backend.retired
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual("Codex", plan.records[0]["role"])
        self.assertEqual("retired", plan.records[0]["state"])
        self.assertEqual(str(self.root.path / "nodes" / "alpha"), plan.workspace)
        self.assertEqual(
            "Teardown retires the node and retains its workspace.",
            result["notice"],
        )


if __name__ == "__main__":
    unittest.main()
