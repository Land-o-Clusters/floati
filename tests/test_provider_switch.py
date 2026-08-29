from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from floati.errors import ProtocolRefusal
from floati.provider_switch import ProviderSwitchPlan, ProviderSwitchWizard
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema


ID_A = "018f7e9b3c137abc8def0123456789ab"
ID_B = "018f7e9b3c137abc9def0123456789ab"
ID_PREVIOUS = "018f7e9b3c137abcbdef0123456789ab"


class RecordingSwitchBackend:
    def __init__(self, preview_stream: io.StringIO) -> None:
        self.preview_stream = preview_stream
        self.assignment = {
            "schema_version": 0,
            "id": "registry-" + ID_PREVIOUS,
            "tenant_id": "fleet",
            "timestamp": "2026-08-27T20:00:00.000Z",
            "kind": "registry_entry",
            "node_id": "alpha",
            "role": "Codex",
            "model": "gpt-5.5",
            "state": "active",
        }
        self.lookups = 0
        self.committed: Optional[ProviderSwitchPlan] = None

    def active_assignment(self, node_id: str) -> dict[str, Any]:
        self.lookups += 1
        if node_id != self.assignment["node_id"]:
            raise ProtocolRefusal("unknown_node", "node is not active")
        return dict(self.assignment)

    def commit_switch(self, plan: ProviderSwitchPlan) -> dict[str, Any]:
        previews = [
            line.removeprefix("ledger preview: ")
            for line in self.preview_stream.getvalue().splitlines()
            if line.startswith("ledger preview: ")
        ]
        import json

        if [json.loads(line) for line in previews[-len(plan.records):]] != list(plan.records):
            raise AssertionError("exact record previews must be flushed before commit")
        self.committed = plan
        return {"records": list(plan.records)}


class ProviderSwitchWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.output = io.StringIO()
        self.backend = RecordingSwitchBackend(self.output)
        ids = iter((ID_A, ID_B))
        self.wizard = ProviderSwitchWizard(
            self.root,
            self.backend,
            id_factory=lambda: next(ids),
            now=lambda: datetime(2026, 8, 27, 22, 30, tzinfo=timezone.utc),
        )

    def test_switch_previews_registry_reassignment_and_receipt_before_one_commit(self) -> None:
        """Catches a model change bypassing the registry row or its durable receipt."""
        result = self.wizard.switch_from_keys(
            ["alpha", "Claude", "claude-sonnet-4"], self.output
        )

        plan = self.backend.committed
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(2, len(plan.records))
        registry, receipt = plan.records
        self.assertEqual(
            {
                "schema_version": 0,
                "id": "registry-" + ID_A,
                "tenant_id": "fleet",
                "timestamp": "2026-08-27T22:30:00.000Z",
                "kind": "registry_entry",
                "node_id": "alpha",
                "role": "Claude",
                "state": "active",
            },
            registry,
        )
        validate_json_schema(registry, Path("schemas/v0/registry-entry.schema.json"))
        self.assertEqual("provider_switch_receipt", receipt["kind"])
        self.assertEqual("Codex", receipt["previous_harness"])
        self.assertEqual("gpt-5.5", receipt["previous_model"])
        self.assertEqual("Claude", receipt["harness"])
        self.assertEqual("claude-sonnet-4", receipt["model"])
        self.assertEqual(
            "registry-" + ID_PREVIOUS, receipt["previous_registry_entry_id"]
        )
        self.assertEqual(registry["id"], receipt["registry_entry_id"])
        self.assertEqual(list(plan.records), result["records"])
        validate_json_schema(
            receipt, Path("schemas/v0/provider-switch-receipt.schema.json")
        )

    def test_plain_fallback_collects_the_same_switch_shape(self) -> None:
        """Catches the plain wizard omitting either the harness or model choice."""
        self.wizard.switch_plain(
            io.StringIO("alpha\nClaude\nclaude-sonnet-4\n"), self.output
        )

        plan = self.backend.committed
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual("alpha", plan.node_id)
        self.assertEqual("Claude", plan.harness)
        self.assertEqual("claude-sonnet-4", plan.model)
        self.assertIn("node id to reassign: ", self.output.getvalue())

    def test_unchanged_assignment_refuses_before_preview_or_commit(self) -> None:
        """Catches no-op switches creating misleading registry history."""
        with self.assertRaises(ProtocolRefusal) as raised:
            self.wizard.switch_from_keys(["alpha", "Codex", "gpt-5.5"], self.output)

        self.assertEqual("provider_switch_unchanged", raised.exception.code)
        self.assertIsNone(self.backend.committed)
        self.assertEqual("", self.output.getvalue())

    def test_legacy_assignment_without_model_can_receive_its_first_model(self) -> None:
        """Catches pre-B6 registry rows being impossible to upgrade."""
        del self.backend.assignment["model"]

        result = self.wizard.switch_from_keys(
            ["alpha", "Codex", "gpt-5.6"], self.output
        )

        self.assertIsNone(result["records"][1]["previous_model"])
        self.assertEqual("gpt-5.6", result["records"][1]["model"])
        validate_json_schema(
            result["records"][1],
            Path("schemas/v0/provider-switch-receipt.schema.json"),
        )

    def test_invalid_active_assignment_refuses_before_preview_or_commit(self) -> None:
        """Catches cross-fleet or retired evidence authorizing reassignment."""
        cases = (("tenant_id", "foreign"), ("state", "retired"))
        for field, value in cases:
            with self.subTest(field=field):
                self.backend.assignment[field] = value
                with self.assertRaises(ProtocolRefusal) as raised:
                    self.wizard.switch_from_keys(
                        ["alpha", "Claude", "claude-sonnet-4"], self.output
                    )
                self.assertEqual("provider_assignment_invalid", raised.exception.code)
                self.assertIsNone(self.backend.committed)
                self.assertEqual("", self.output.getvalue())
                self.backend.assignment[field] = "fleet" if field == "tenant_id" else "active"

    def test_non_registry_assignment_evidence_refuses_before_preview(self) -> None:
        """Catches an arbitrary projection being trusted as registry evidence."""
        cases = (
            ("schema_version", 1),
            ("kind", "message_envelope"),
            ("id", "registry-" + "c" * 32),
        )
        for field, value in cases:
            with self.subTest(field=field):
                original = self.backend.assignment[field]
                self.backend.assignment[field] = value
                with self.assertRaises(ProtocolRefusal) as raised:
                    self.wizard.switch_from_keys(
                        ["alpha", "Claude", "claude-sonnet-4"], self.output
                    )
                self.assertEqual("provider_assignment_invalid", raised.exception.code)
                self.assertIsNone(self.backend.committed)
                self.assertEqual("", self.output.getvalue())
                self.backend.assignment[field] = original

    def test_extra_credential_shaped_input_refuses_before_lookup(self) -> None:
        """Catches the switch surface growing a credential input path."""
        with self.assertRaises(ProtocolRefusal) as raised:
            self.wizard.switch_from_keys(
                ["alpha", "Claude", "claude-sonnet-4", "api-key-value"], self.output
            )

        self.assertEqual("wizard_input_invalid", raised.exception.code)
        self.assertEqual(0, self.backend.lookups)
        self.assertIsNone(self.backend.committed)
        self.assertEqual("", self.output.getvalue())

    def test_invalid_model_refuses_before_registry_lookup(self) -> None:
        """Catches empty or terminal-unsafe model coordinates reaching the backend."""
        for model in ("", "bad model", "bad\x1bmodel"):
            with self.subTest(model=repr(model)):
                with self.assertRaises(ProtocolRefusal) as raised:
                    self.wizard.switch_from_keys(["alpha", "Claude", model], self.output)
                self.assertEqual("model_invalid", raised.exception.code)
        self.assertEqual(0, self.backend.lookups)
        self.assertIsNone(self.backend.committed)
        self.assertEqual("", self.output.getvalue())

    def test_non_uuid7_record_id_refuses_before_preview_or_commit(self) -> None:
        """Catches a reassignment producing rows the durable registry rejects."""
        wizard = ProviderSwitchWizard(
            self.root,
            self.backend,
            id_factory=lambda: "c" * 32,
            now=lambda: datetime(2026, 8, 27, 22, 30, tzinfo=timezone.utc),
        )

        with self.assertRaises(ProtocolRefusal) as raised:
            wizard.switch_from_keys(["alpha", "Claude", "claude-sonnet-4"], self.output)

        self.assertEqual("wizard_id_invalid", raised.exception.code)
        self.assertIsNone(self.backend.committed)
        self.assertEqual("", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
