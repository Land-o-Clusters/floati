from __future__ import annotations

import json
import unittest
from pathlib import Path


HM3I_RECEIPT = Path("docs/evidence/HM3I-ORCHESTRATION-TRUTH.md")


class HM3IContractTests(unittest.TestCase):
    def test_retained_receipt_cites_the_exact_hm3i_authority_and_scope(self) -> None:
        receipt = HM3I_RECEIPT.read_text()
        runtruth = Path("floati/runtruth.py").read_text()

        self.assertIn("bounded local run graph", receipt.lower())
        self.assertIn("a111202b228d34c2b371bcc5e2c4798206474439", receipt)
        self.assertIn("plan_amendment", runtruth)
        self.assertFalse(Path("docs/DESIGN.md").exists())
        self.assertFalse(Path("docs/SPEC-DRAFT.md").exists())

    def test_item_ten_and_item_eleven_public_contracts_are_local_and_fail_closed(self) -> None:
        receipt = HM3I_RECEIPT.read_text()
        index = json.loads(Path("bundle/c7.1/bundle-index.json").read_text())
        catalog = json.loads(Path("bundle/c7.1/schema-catalog.json").read_text())
        readme = Path("bundle/c7.1/README.md").read_text()
        for literal in (
            "runs/events.jsonl",
            "FLOATI.toml",
            "attempt_harness_session_bound",
            "supervisor_orphaned",
            "conflicting_binding",
            "auxiliary_sources",
        ):
            self.assertIn(literal, receipt)
        self.assertIn("no exact committed-candidate, Fable final gate, publication,", receipt)
        self.assertEqual("excluded-c7.1", index["approvals"])
        self.assertEqual("schema-catalog.json", index["schema_catalog"])
        self.assertTrue(any(entry.get("ledger_template") == "raw/repositories/<repository-coordinate>/decisions.jsonl" for entry in catalog["entries"]))
        self.assertIn("deterministically reprojects", readme)
        self.assertFalse(Path("docs/PUBLICATION-CHECKLIST.md").exists())

    def test_item_nine_decision_boundary_remains_in_receipt_and_schema(self) -> None:
        """The retained receipt and schema preserve Item 9's durable boundary."""
        receipt = HM3I_RECEIPT.read_text()
        schema = json.loads(Path("schemas/v0/decision-record.schema.json").read_text())
        for literal in (
            "Item 9 coordinate, closed source taxonomy and injected document resolver",
            "`operator|architect` terminal authority",
            "optional same-repository",
            "legacy fail-closed behavior",
            "full-record digest",
            "accepted-only physical-order capsule",
        ):
            self.assertIn(literal, receipt)
        self.assertIn("decision_digest", schema["required"])
        terminal_authorities = schema["allOf"][1]["then"]["properties"]["author_authority"]["enum"]
        self.assertEqual(["operator", "architect"], terminal_authorities)


if __name__ == "__main__":
    unittest.main()
