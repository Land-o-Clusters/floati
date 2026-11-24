"""The unavailable-history alternative preserves the prior public contract."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WakeReceiptLegacySchemaTests(unittest.TestCase):
    def test_only_named_unavailable_alternatives_extend_the_frozen_schema(self):
        before = ROOT / "tests/fixtures/wake-receipt-legacy-read-before.schema.json"
        self.assertEqual(
            "5910a886eab78365a3a55bf81d0c054ccafce774a081ab21bca1da83e9505171",
            hashlib.sha256(before.read_bytes()).hexdigest(),
        )
        old = json.loads(before.read_text())
        # NEEDS-YOU-OBSERVED-1 later widens the live schema's waiter_exit_reason
        # by observed_item (tests/test_needs_you_wake_1.py); this row's delta is
        # held by the schema as it stood before that row.
        new = json.loads((ROOT / "tests/fixtures/needs-you-wake-health-v1-before.schema.json").read_text())
        self.assertEqual([*old["properties"]["state"]["enum"], "unknown"],
                         new["properties"]["state"]["enum"])
        new["properties"]["state"] = old["properties"]["state"]
        self.assertEqual({"$ref": "#/$defs/read_finding"},
                         new["properties"].pop("read_finding"))
        self.assertNotIn("read_finding", new["required"])
        coverage = new["$defs"]["recent_sessions"]["properties"]["native_woke"]["properties"]
        self.assertEqual({"enum": ["recorded_receipts_only", "unavailable"]},
                         coverage["coverage"])
        coverage["coverage"] = old["$defs"]["recent_sessions"]["properties"]["native_woke"]["properties"]["coverage"]
        finding = new["$defs"].pop("read_finding")
        self.assertFalse(finding["additionalProperties"])
        self.assertEqual({"code", "ledger", "record_id", "source_code"},
                         set(finding["required"]))
        # New allOf conditions can only restrict the existing base schema;
        # native fixture mutation tests exercise the unavailable branch.
        self.assertTrue(new.pop("allOf"))
        self.assertEqual(old, new, "every preexisting field and definition stays exact")


if __name__ == "__main__":
    unittest.main()
