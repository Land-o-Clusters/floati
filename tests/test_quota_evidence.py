from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from floati.quota import QuotaReceipt
from floati.quota_adapters import adapter_for, adapter_roster


OBSERVED_AT = "2026-08-29T12:00:00Z"
MEASURED_PATH = Path("docs/evidence/quota/v4-measured.json")
UNKNOWN_PATH = Path("docs/evidence/quota/v4-unknown.json")


def _independent_receipt_digest(payload: dict) -> str:
    base = dict(payload)
    base.pop("receipt_digest")
    return hashlib.sha256(
        json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class QuotaEvidenceTests(unittest.TestCase):
    def test_manifest_inventory_names_the_complete_quota_runtime(self) -> None:
        manifest = json.loads(
            Path("bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        by_path = {row["path"]: row["sha256"] for row in manifest["files"]}
        required = {
            "floati/quota.py",
            "floati/quota_adapters.py",
            "scripts/floati-quota-statusline",
            "schemas/v0/quota-receipt-record.schema.json",
            "schemas/v1/tide-policy-record.schema.json",
            "schemas/v1/tide-receipt-record.schema.json",
        }
        self.assertEqual(set(), required - set(by_path))
        for relative in required:
            self.assertEqual(
                hashlib.sha256(Path(relative).read_bytes()).hexdigest(),
                by_path[relative],
            )

    def test_measured_fixture_is_reproducible_and_independently_digest_valid(self) -> None:
        self.assertTrue(MEASURED_PATH.is_file(), "V4 measured fixture must exist")
        payload = json.loads(MEASURED_PATH.read_text(encoding="utf-8"))
        receipt = QuotaReceipt.from_dict(payload)
        self.assertEqual(payload["receipt_digest"], _independent_receipt_digest(payload))
        self.assertEqual(("MEASURED", "MEASURED"), tuple(fact.stamp for fact in receipt.facts))
        self.assertEqual(
            ("0.250000", "0.405000"),
            tuple(fact.state.value for fact in receipt.facts),
        )
        source = json.dumps(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 25, "resets_at": 1788022800},
                    "seven_day": {"used_percentage": 40.5, "resets_at": 1788609600},
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        reproduced = adapter_for("anthropic_claude_code").observe(
            source,
            observed_at=OBSERVED_AT,
            idempotency_key="v4-measured-claude",
        )
        self.assertEqual(receipt.to_dict(), reproduced.to_dict())

    def test_unknown_fixture_is_exact_six_reproducible_receipts_with_no_number(self) -> None:
        self.assertTrue(UNKNOWN_PATH.is_file(), "V4 unknown fixture must exist")
        payload = json.loads(UNKNOWN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            {"artifact_version", "fixture", "kind", "receipts", "schema_version"},
            set(payload),
        )
        self.assertEqual((0, 0, "fully_unknown", "quota_receipt_set"), (
            payload["artifact_version"], payload["schema_version"],
            payload["fixture"], payload["kind"],
        ))
        receipts = tuple(QuotaReceipt.from_dict(row) for row in payload["receipts"])
        self.assertEqual(
            tuple(adapter.provider for adapter in adapter_roster()),
            tuple(receipt.provider for receipt in receipts),
        )
        for row, receipt in zip(payload["receipts"], receipts):
            self.assertEqual(row["receipt_digest"], _independent_receipt_digest(row))
            self.assertEqual(1, len(receipt.facts))
            self.assertEqual(
                ("unknown", None),
                (receipt.facts[0].state.kind, receipt.facts[0].state.value),
            )
        reproduced = tuple(
            adapter.observe(
                b"",
                observed_at=OBSERVED_AT,
                idempotency_key="v4-unknown-" + adapter.provider,
            ).to_dict()
            for adapter in adapter_roster()
        )
        self.assertEqual(tuple(payload["receipts"]), reproduced)


if __name__ == "__main__":
    unittest.main()
