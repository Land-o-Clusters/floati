from __future__ import annotations

import importlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema


OBSERVED_AT = "2026-08-29T12:00:00Z"
RESETS_AT = "2026-08-29T17:00:00Z"
EVIDENCE_DIGEST = "a" * 64


def _quota_api():
    try:
        return importlib.import_module("floati.quota")
    except ModuleNotFoundError as exc:
        raise AssertionError("V4 quota truth module must exist") from exc


def _fact(api, *, stamp="MEASURED", state=None, source="codex_app_server:codex.primary"):
    selected_state = state or api.QuotaState("consumed_fraction", "0.25")
    return api.QuotaFact(
        provider="openai_codex",
        surface="account/rateLimits/read",
        state=selected_state,
        stamp=stamp,
        source=source,
        evidence_digest=EVIDENCE_DIGEST,
        observed_at=OBSERVED_AT,
        resets_at=RESETS_AT,
    )


def _receipt(api, *, fraction="0.25", idempotency_key="quota-1"):
    return api.QuotaReceipt.create(
        provider="openai_codex",
        endpoint_id="local-codex",
        facts=(
            _fact(
                api,
                state=api.QuotaState("consumed_fraction", fraction),
            ),
        ),
        idempotency_key=idempotency_key,
    )


class QuotaTruthTests(unittest.TestCase):
    def test_quota_fact_requires_canonical_state_stamp_source_digest_and_time(self) -> None:
        api = _quota_api()

        fact = _fact(api)

        self.assertEqual(
            {"kind": "consumed_fraction", "value": "0.250000"},
            fact.state.to_dict(),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            api.QuotaState("unknown", "0.25")
        self.assertEqual("quota_state_invalid", caught.exception.code)
        invalid = (
            {"stamp": "SELF_REPORTED"},
            {"source": ""},
            {"evidence_digest": "not-a-digest"},
            {"observed_at": "yesterday"},
            {"resets_at": "2026-08-29T11:59:59Z"},
        )
        base = fact.to_dict()
        for mutation in invalid:
            with self.subTest(mutation=mutation):
                changed = dict(base)
                changed.update(mutation)
                if isinstance(changed["state"], dict):
                    changed["state"] = api.QuotaState.from_dict(changed["state"])
                with self.assertRaises(ProtocolRefusal) as caught:
                    api.QuotaFact(**changed)
                self.assertEqual("quota_fact_invalid", caught.exception.code)

    def test_quota_receipt_digest_is_canonical_and_detects_tampering(self) -> None:
        api = _quota_api()

        receipt = _receipt(api)

        self.assertEqual(
            "7152f55dfe9fab694a96a968f2c9556c3d7fa1135e0a5ee54fbd6fcacf4bab77",
            receipt.receipt_digest,
        )
        self.assertTrue(receipt.to_json().endswith("\n"))
        tampered = receipt.to_dict()
        tampered["facts"][0]["state"]["value"] = "0.500000"
        with self.assertRaises(IntegrityFailure) as caught:
            api.QuotaReceipt.from_dict(tampered)
        self.assertEqual("quota_receipt_invalid", caught.exception.code)

    def test_quota_receipt_refuses_duplicate_surface_window_with_changed_evidence(self) -> None:
        api = _quota_api()
        first = _fact(api)
        conflicting = replace(first, evidence_digest="b" * 64)

        with self.assertRaises(IntegrityFailure) as caught:
            api.QuotaReceipt.create(
                provider="openai_codex",
                endpoint_id="local-codex",
                facts=(first, conflicting),
                idempotency_key="duplicate-surface-window",
            )

        self.assertEqual("quota_receipt_invalid", caught.exception.code)

    def test_quota_ledger_replay_is_idempotent_and_conflicts_on_changed_content(self) -> None:
        api = _quota_api()
        temporary = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(temporary.cleanup)
        root = FloatiRoot.open_direct_home(
            Path(temporary.name) / "fleet-alpha", create=True
        )
        ledger = api.QuotaLedger(root)

        first = ledger.append(_receipt(api))
        retry = ledger.append(_receipt(api))

        self.assertEqual(first, retry)
        validate_json_schema(
            first, Path("schemas/v0/quota-receipt-record.schema.json")
        )
        self.assertEqual(
            "0.250000",
            ledger.latest("openai_codex").facts[0].state.value,
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.append(_receipt(api, fraction="0.50"))
        self.assertEqual("quota_receipt_idempotency_conflict", caught.exception.code)

    def test_estimate_fact_is_impossible_to_bind_and_refusal_cites_stamp(self) -> None:
        api = _quota_api()
        fact = _fact(api, stamp="ESTIMATE", source="documentation_prior")

        with self.assertRaises(ProtocolRefusal) as caught:
            api.require_schedulable_fraction(fact)

        self.assertEqual("quota_fact_not_evaluable", caught.exception.code)
        self.assertIn("stamp=ESTIMATE", caught.exception.detail)
        self.assertIn("source=documentation_prior", caught.exception.detail)

    def test_unknown_fact_has_no_number_and_schedules_no_action(self) -> None:
        api = _quota_api()
        state = api.QuotaState("unknown", None)
        fact = _fact(api, state=state, source="no_documented_local_surface")

        self.assertIsNone(fact.state.value)
        with self.assertRaises(ProtocolRefusal) as caught:
            api.require_schedulable_fraction(fact)
        self.assertEqual("quota_fact_unknown", caught.exception.code)

    def test_measured_and_derived_consumed_fractions_are_schedulable(self) -> None:
        api = _quota_api()

        for stamp in ("MEASURED", "DERIVED"):
            with self.subTest(stamp=stamp):
                self.assertEqual(
                    "0.250000",
                    api.require_schedulable_fraction(_fact(api, stamp=stamp)),
                )


if __name__ == "__main__":
    unittest.main()
