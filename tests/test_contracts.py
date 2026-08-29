from __future__ import annotations

import unittest

from floati.contracts import AcceptanceReceipt, ContractHistory, PlanAmendment, TaskContract, contract_digest
from floati.errors import ProtocolRefusal


class TaskContractTests(unittest.TestCase):
    def contract(self) -> TaskContract:
        return TaskContract.create(
            objective="add immutable task provenance",
            non_goals=("do not score model confidence", "do not mutate history"),
            areas_to_avoid=(
                {"path": "docs/DESIGN.md", "region": "Task 6"},
                {"path": "slip/graph.py", "region": "all"},
            ),
            input_hashes={"brief": "a" * 64, "design": "b" * 64},
            acceptance_checks={
                "contracts.unit": "python3 -m unittest tests.test_contracts",
                "schemas.strict": "python3 -m unittest tests.test_schemas",
            },
            constraints={"network": "dark", "order": "physical"},
            risk_class="high",
            retry_policy={
                "max_attempts": 2,
                "backoff": {
                    "base_delay_ms": 100,
                    "cap_delay_ms": 1000,
                    "strategy": "exponential",
                },
            },
            dependencies=("work-alpha", "work-beta"),
        )

    def test_canonical_contract_binds_every_governed_field_to_a_literal_digest(self) -> None:
        """Catches a contract digest that omits avoid areas, constraints, or another governed field."""
        contract = self.contract()
        self.assertEqual(
            {
                "objective": "add immutable task provenance",
                "non_goals": ["do not score model confidence", "do not mutate history"],
                "areas_to_avoid": [
                    {"path": "docs/DESIGN.md", "region": "Task 6"},
                    {"path": "slip/graph.py", "region": "all"},
                ],
                "input_hashes": {"brief": "a" * 64, "design": "b" * 64},
                "acceptance_checks": {
                    "contracts.unit": "python3 -m unittest tests.test_contracts",
                    "schemas.strict": "python3 -m unittest tests.test_schemas",
                },
                "constraints": {"network": "dark", "order": "physical"},
                "risk_class": "high",
                "retry_policy": {
                    "max_attempts": 2,
                    "backoff": {
                        "base_delay_ms": 100,
                        "cap_delay_ms": 1000,
                        "strategy": "exponential",
                    },
                },
                "dependencies": ["work-alpha", "work-beta"],
            },
            contract.canonical(),
        )
        self.assertEqual(
            "0577b95cbc0da8be9e55c2e52284043f49bf2e61dfe9143c73d74f304158d987",
            contract_digest(contract),
        )

    def test_contract_change_requires_a_prior_digest_append_only_amendment(self) -> None:
        """Catches mutable task history or an amendment that does not name the exact prior digest."""
        original = self.contract()
        amended = original.replaced(objective="add durable immutable task provenance")
        history = ContractHistory(original)
        amendment = PlanAmendment.between(original, amended)
        history.append(amendment, amended)
        self.assertEqual((original, amended), history.contracts())
        self.assertEqual(contract_digest(original), amendment.previous_digest)
        self.assertEqual(
            {"objective": "add durable immutable task provenance"},
            dict(amendment.replacement_fields),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            history.append(amendment, amended)
        self.assertEqual("amendment_previous_digest_invalid", caught.exception.code)

    def test_acceptance_receipt_binds_named_checks_reviewer_evidence_and_deviations(self) -> None:
        """Catches a receipt object that omits acceptance evidence or reintroduces semantic-score authority."""
        receipt = AcceptanceReceipt.create(
            contract_digest=contract_digest(self.contract()), check_ids=("contracts.unit",),
            reviewer="reviewer-a", evidence_bindings=("worker-receipt-a",),
            deviations=("none",), result="accepted",
        )
        self.assertEqual(
            {"contract_digest", "check_ids", "reviewer", "evidence_bindings", "deviations", "result"},
            set(receipt.canonical()),
        )
        self.assertEqual("accepted", receipt.canonical()["result"])

    def test_root_contract_and_unhashable_closed_values_refuse_with_protocol_errors(self) -> None:
        """Catches root-task rejection or raw TypeError escapes from closed contract/receipt vocabularies."""
        root = self.contract().replaced(dependencies=[])
        self.assertEqual([], root.canonical()["dependencies"])
        for changes in ({"risk_class": []}, {"retry_policy": {"max_attempts": 1, "backoff": {"base_delay_ms": 0, "cap_delay_ms": 0, "strategy": []}}}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolRefusal):
                self.contract().replaced(**changes)
        with self.assertRaises(ProtocolRefusal):
            AcceptanceReceipt.create(contract_digest=contract_digest(self.contract()), check_ids=["contracts.unit"], reviewer="reviewer-a", evidence_bindings=["worker-receipt-a"], deviations=[], result=[])


if __name__ == "__main__":
    unittest.main()
