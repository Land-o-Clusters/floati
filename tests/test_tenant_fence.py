"""Focused bank for digest-backed tenant coordinate fences."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from floati import tenant_fence
from floati.tenant_fence import (
    DECLARATION_RELATIVE,
    FORBIDDEN_TOKEN_DIGESTS,
    declaration_matches_public_pins,
    tenant_coordinate_found,
    tenant_violation_codes,
)
from tests.export_inventory import POLICY_RELATIVE, export_policy
from tests.private_artifacts import require_private_artifact


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class TenantFenceDetectionTests(unittest.TestCase):
    """The fence still REDs on planted private coordinates."""

    def test_planted_private_workspace_path_is_a_finding(self) -> None:
        token = self._operator_token("private_workspace_path")
        self.assertTrue(
            tenant_coordinate_found(
                f"ROOT={token}\n", key=tenant_fence.operator_key(REPOSITORY_ROOT)
            )
        )

    def test_planted_private_fleet_id_is_a_finding(self) -> None:
        token = self._operator_token("private_fleet_id")
        self.assertIn(
            "private_fleet_id",
            tenant_violation_codes(
                f'"tenant_id":"{token}"',
                key=tenant_fence.operator_key(REPOSITORY_ROOT),
            ),
        )

    def test_without_the_key_the_digest_half_is_inert_and_says_why(self) -> None:
        """The projection's honest posture: patterns live, digests cannot."""

        token = self._operator_token("private_fleet_id")
        self.assertEqual(
            (), tenant_violation_codes(f'"tenant_id":"{token}"', key=None)
        )

    def test_neutral_floati_copy_is_clean(self) -> None:
        self.assertFalse(
            tenant_coordinate_found(
                "Point your coding agent at this repository. floati walks it from install."
            )
        )

    def _operator_token(self, code: str) -> str:
        require_private_artifact(self, DECLARATION_RELATIVE)
        document = json.loads(
            (REPOSITORY_ROOT / DECLARATION_RELATIVE).read_text(encoding="utf-8")
        )
        for entry in document["forbidden_tokens"]:
            if entry["code"] == code:
                return entry["token"]
        raise AssertionError(f"missing operator token for {code}")


class TenantFenceDeclarationTests(unittest.TestCase):
    """Harbor-only operator declarations stay aligned with exported digest pins."""

    def test_operator_declaration_matches_public_pins(self) -> None:
        require_private_artifact(self, DECLARATION_RELATIVE)
        self.assertTrue(declaration_matches_public_pins(REPOSITORY_ROOT))

    def test_public_pins_cover_every_declaration_code(self) -> None:
        require_private_artifact(self, DECLARATION_RELATIVE)
        document = json.loads(
            (REPOSITORY_ROOT / DECLARATION_RELATIVE).read_text(encoding="utf-8")
        )
        declared = {entry["code"] for entry in document["forbidden_tokens"]}
        pinned = {code for code, _digest, _length in FORBIDDEN_TOKEN_DIGESTS}
        self.assertEqual(declared, pinned)

    def test_declaration_is_private_only(self) -> None:
        require_private_artifact(self, POLICY_RELATIVE)
        self.assertIn(DECLARATION_RELATIVE, export_policy().private_only_paths)

    def test_operator_tokens_are_absent_from_the_exported_module(self) -> None:
        require_private_artifact(self, DECLARATION_RELATIVE)
        source = (REPOSITORY_ROOT / "floati" / "tenant_fence.py").read_text(
            encoding="utf-8"
        )
        document = json.loads(
            (REPOSITORY_ROOT / DECLARATION_RELATIVE).read_text(encoding="utf-8")
        )
        key = document["key"]
        for entry in document["forbidden_tokens"]:
            with self.subTest(code=entry["code"]):
                self.assertNotIn(entry["token"], source)
        self.assertNotIn(key, source)


class TenantPinReconstructibilityTests(unittest.TestCase):
    """NAME-FENCE-1 Am.1: a short bare sha256 pin is plaintext recovery.

    A pin over a 6-byte token is invertible by enumeration: 6 lowercase
    bytes is a few billion candidates, and the harbor cannot tell a
    matched guess from a remembered one. The pins are therefore keyed
    (HMAC-SHA256) with the key declared in the private_only operator
    file, so the fence still recognises a forbidden coordinate without
    publishing a reversible fingerprint of it.
    """

    def test_pins_are_keyed_by_the_declared_operator_key(self) -> None:
        import hashlib
        import hmac as hmac_module

        require_private_artifact(self, DECLARATION_RELATIVE)
        self.assertEqual("hmac-sha256", tenant_fence.PIN_SCHEME)
        document = tenant_fence.load_operator_declaration(REPOSITORY_ROOT)
        self.assertEqual(tenant_fence.PIN_SCHEME, document.get("scheme"))
        key = tenant_fence.operator_key(REPOSITORY_ROOT)
        for entry in document["forbidden_tokens"]:
            expected = hmac_module.new(
                key, entry["token"].casefold().encode("utf-8"), hashlib.sha256
            ).hexdigest()
            with self.subTest(code=entry["code"]):
                self.assertEqual(
                    expected,
                    entry["digest"],
                    "declaration digest is not the keyed digest of its token",
                )
        pinned = {(code, digest, length) for code, digest, length in FORBIDDEN_TOKEN_DIGESTS}
        declared = {
            (entry["code"], entry["digest"], entry["length"])
            for entry in document["forbidden_tokens"]
        }
        self.assertEqual(pinned, declared)

    def test_a_bare_short_digest_yields_its_plaintext_to_enumeration(self) -> None:
        """The attack class, demonstrated on a synthetic 6-byte token.

        The token lives in a tiny declared charset so the enumeration is
        bounded. The bare digest gives up the plaintext; the keyed digest
        of the SAME token does not, because it is not a bare hash of any
        candidate the enumerator can produce -- matching it requires the
        key, which the enumerator does not have.
        """

        import hashlib
        import itertools

        charset = "abcdef"
        token = "fedcba"
        space = ("".join(parts) for parts in itertools.product(charset, repeat=6))
        bare = hashlib.sha256(token.encode("utf-8")).hexdigest()
        recovered = next(
            (
                candidate
                for candidate in space
                if hashlib.sha256(candidate.encode("utf-8")).hexdigest() == bare
            ),
            None,
        )
        self.assertEqual(token, recovered)

        keyed = tenant_fence.hmac_digest(b"am1-demo-key", token)
        self.assertNotEqual(bare, keyed)
        space = ("".join(parts) for parts in itertools.product(charset, repeat=6))
        self.assertIsNone(
            next(
                (
                    candidate
                    for candidate in space
                    if hashlib.sha256(candidate.encode("utf-8")).hexdigest() == keyed
                ),
                None,
            )
        )


if __name__ == "__main__":
    unittest.main()
