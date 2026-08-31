from __future__ import annotations

import unittest


class OperatorContractTests(unittest.TestCase):
    def test_argument_refusal_names_class_flags_and_full_shape(self) -> None:
        from floati.entrypoint_contract import ArgumentContract

        contract = ArgumentContract(
            operation="wake waiter",
            required=("--root", "--as", "--session", "--idempotency-key"),
            optional=("--json",),
            full_shape=(
                "floati wake waiter --root ROOT --as NODE --session SESSION "
                "--idempotency-key KEY [--json]"
            ),
        )
        refusal = contract.refuse(
            missing={"--idempotency-key"}, unknown={"--bogus"}
        )

        self.assertEqual("malformed_invocation", refusal["class"])
        self.assertEqual(["--idempotency-key"], refusal["missing"])
        self.assertEqual(["--bogus"], refusal["unknown"])
        self.assertIn("--idempotency-key", refusal["detail"])
        self.assertIn("--bogus", refusal["detail"])
        self.assertIn(contract.full_shape, refusal["detail"])
        self.assertEqual(
            {
                "operation", "class", "missing", "unknown", "required",
                "optional", "full_shape", "detail",
            },
            set(refusal),
        )

    def test_shipped_waiter_contract_is_discoverable_and_closed(self) -> None:
        from floati.entrypoint_contract import ENTRYPOINT_CONTRACTS

        contract = ENTRYPOINT_CONTRACTS["codex-stop-waiter"]
        self.assertEqual(
            ("--root",), contract.required,
            "the installed waiter process binds identity from hook payload, not flags",
        )
        self.assertEqual((), contract.optional)
        self.assertEqual(
            "scripts/floati-codex-wait --root ROOT", contract.full_shape
        )


if __name__ == "__main__":
    unittest.main()
