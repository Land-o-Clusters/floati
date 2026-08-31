from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot


NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


class ApprovalLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.registry = Registry(self.root)
        self.registry.register(public_ids.worker('alpha'), "Codex")
        self.registry.register(public_ids.reviewer(), "Claude")
        self.grant = AuthorityGrantStore(self.root).claim(
            "approve-build", public_ids.reviewer(), 120, 120, NOW
        )

    def request(self):
        from floati.approvals import ApprovalLedger

        return ApprovalLedger(self.root).request(
            public_ids.worker('alpha'),
            "workspace.patch",
            "repo:slipway",
            60,
            "approve-build",
            self.grant["epoch"],
            now=NOW + timedelta(seconds=1),
        )

    def action_request(self):
        from floati.approvals import ApprovalLedger

        digest = hashlib.sha256(b"git push origin guarded").hexdigest()
        request = ApprovalLedger(self.root).request_for_action(
            public_ids.worker('alpha'),
            "workspace.patch",
            "repo:slipway",
            60,
            digest,
            "approve-build",
            self.grant["epoch"],
            now=NOW + timedelta(seconds=1),
        )
        return digest, request

    def test_v0_approval_request_and_decision_remain_byte_compatible(self) -> None:
        from floati.approvals import ApprovalLedger

        ledger = ApprovalLedger(self.root)
        request = self.request()
        decision = ledger.decide(
            request["id"],
            public_ids.reviewer(),
            "approved",
            None,
            granted_scope="repo:slipway",
            granted_ttl_seconds=30,
            now=NOW + timedelta(seconds=2),
        )

        self.assertEqual(0, request["schema_version"])
        self.assertEqual(0, decision["schema_version"])
        self.assertNotIn("exact_action_digest", request)
        self.assertNotIn("exact_action_digest", decision)

    def test_v1_action_bound_request_and_decision_repeat_exact_digest(self) -> None:
        from floati.approvals import ApprovalLedger

        ledger = ApprovalLedger(self.root)
        digest, request = self.action_request()
        decision = ledger.decide(
            request["id"],
            public_ids.reviewer(),
            "approved",
            None,
            granted_scope="repo:slipway",
            granted_ttl_seconds=30,
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(
            (1, digest),
            (request["schema_version"], request["exact_action_digest"]),
        )
        self.assertEqual(
            (1, digest, request["id"]),
            (
                decision["schema_version"],
                decision["exact_action_digest"],
                decision["request_id"],
            ),
        )

        denied_digest = hashlib.sha256(b"git reset --hard guarded").hexdigest()
        denied_request = ledger.request_for_action(
            public_ids.worker('alpha'),
            "workspace.patch",
            "repo:slipway",
            60,
            denied_digest,
            "approve-build",
            self.grant["epoch"],
            now=NOW + timedelta(seconds=3),
        )
        denied = ledger.decide(
            denied_request["id"],
            public_ids.reviewer(),
            "denied",
            "operator_denied",
            now=NOW + timedelta(seconds=4),
        )
        self.assertEqual(
            (1, denied_digest, denied_request["id"], "denied"),
            (
                denied["schema_version"],
                denied["exact_action_digest"],
                denied["request_id"],
                denied["decision"],
            ),
        )

    def test_effect_action_lookup_requires_exact_requester_digest_and_decision(self) -> None:
        """Catches effect approval lookup accepting an unrelated durable approval."""
        from floati.approvals import ApprovalLedger

        ledger = ApprovalLedger(self.root)
        digest, request = self.action_request()
        decision = ledger.decide(
            request["id"], public_ids.reviewer(), "approved", None,
            granted_scope="repo:slipway", granted_ttl_seconds=30,
            now=NOW + timedelta(seconds=2),
        )
        selected_request, selected_decision = ledger.require_approved_action(
            request["id"], decision["id"], requester=public_ids.worker('alpha'),
            exact_action_digest=digest, now=NOW + timedelta(seconds=3),
        )
        self.assertEqual(request, selected_request)
        self.assertEqual(decision, selected_decision)

        for requester, action_digest in (
            (public_ids.reviewer(), digest),
            (public_ids.worker('alpha'), "9" * 64),
        ):
            with self.subTest(requester=requester, digest=action_digest):
                with self.assertRaises(ProtocolRefusal) as caught:
                    ledger.require_approved_action(
                        request["id"], decision["id"], requester=requester,
                        exact_action_digest=action_digest,
                        now=NOW + timedelta(seconds=3),
                    )
                self.assertEqual(
                    "effect_approval_action_mismatch", caught.exception.code
                )

        forged = deepcopy(decision)
        forged["id"] = "approval-decision-" + uuid7_hex()
        forged["granted_scope"] = "repo:other"
        append_record(
            self.root, "approvals/decisions.jsonl", forged,
            allowed_kinds={"approval_decision"},
        )
        with self.assertRaises(ProtocolRefusal) as semantic:
            ledger.require_approved_action(
                request["id"], forged["id"], requester=public_ids.worker('alpha'),
                exact_action_digest=digest, now=NOW + timedelta(seconds=3),
            )
        self.assertEqual("effect_approval_action_mismatch", semantic.exception.code)

    def test_v1_action_digest_scope_ttl_and_authority_cannot_drift(self) -> None:
        from floati.approvals import ApprovalLedger

        ledger = ApprovalLedger(self.root)
        digest, request = self.action_request()
        self.assertEqual(digest, request["exact_action_digest"])

        for invalid in ("0" * 63, "A" * 64, "g" * 64, None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProtocolRefusal) as caught:
                    ledger.request_for_action(
                        public_ids.worker('alpha'),
                        "workspace.patch",
                        "repo:slipway",
                        60,
                        invalid,
                        "approve-build",
                        self.grant["epoch"],
                        now=NOW + timedelta(seconds=1),
                    )
                self.assertEqual("exact_action_digest_invalid", caught.exception.code)

        for arguments, code in (
            (
                {"granted_scope": "repo:other", "granted_ttl_seconds": 30},
                "approval_scope_broadened",
            ),
            (
                {"granted_scope": "repo:slipway", "granted_ttl_seconds": 61},
                "approval_ttl_broadened",
            ),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    ledger.decide(
                        request["id"],
                        public_ids.reviewer(),
                        "approved",
                        None,
                        now=NOW + timedelta(seconds=2),
                        **arguments,
                    )
                self.assertEqual(code, caught.exception.code)

        with self.assertRaises(ProtocolRefusal) as authority:
            ledger.request_for_action(
                public_ids.worker('alpha'),
                "workspace.patch",
                "repo:slipway",
                60,
                digest,
                "approve-build",
                self.grant["epoch"] + 1,
                now=NOW + timedelta(seconds=1),
            )
        self.assertEqual("authority_epoch_mismatch", authority.exception.code)

    def test_approval_grant_is_narrow_ttl_bound_and_one_decision(self) -> None:
        from floati.approvals import ApprovalLedger

        ledger = ApprovalLedger(self.root)
        request = self.request()
        decision = ledger.decide(
            request["id"],
            public_ids.reviewer(),
            "approved",
            None,
            granted_scope="repo:slipway",
            granted_ttl_seconds=30,
            now=NOW + timedelta(seconds=2),
        )

        self.assertEqual("approval_request", request["kind"])
        self.assertEqual("approval_decision", decision["kind"])
        self.assertEqual("approved", decision["decision"])
        self.assertEqual("approve-build", decision["authority_subject"])
        self.assertEqual(self.grant["epoch"], decision["authority_epoch"])
        self.assertEqual("repo:slipway", decision["granted_scope"])
        self.assertEqual(30, decision["granted_ttl_seconds"])
        with self.assertRaises(ProtocolRefusal) as duplicate:
            ledger.decide(
                request["id"], public_ids.reviewer(), "denied", "operator_denied",
                now=NOW + timedelta(seconds=3),
            )
        self.assertEqual("approval_already_decided", duplicate.exception.code)
        decisions = read_records(
            self.root,
            "approvals/decisions.jsonl",
            allowed_kinds={"approval_decision"},
        )
        self.assertEqual([decision], decisions)

    def test_approval_cannot_broaden_scope_or_ttl(self) -> None:
        from floati.approvals import ApprovalLedger

        ledger = ApprovalLedger(self.root)
        request = self.request()
        path = self.home / "approvals" / "decisions.jsonl"
        cases = (
            ({"granted_scope": "repo:other", "granted_ttl_seconds": 30}, "approval_scope_broadened"),
            ({"granted_scope": "repo:slipway", "granted_ttl_seconds": 61}, "approval_ttl_broadened"),
        )
        for arguments, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    ledger.decide(
                        request["id"],
                        public_ids.reviewer(),
                        "approved",
                        None,
                        now=NOW + timedelta(seconds=2),
                        **arguments,
                    )
                self.assertEqual(code, caught.exception.code)
                self.assertFalse(path.exists())

    def test_denial_is_a_durable_decision_receipt_without_capability_grant(self) -> None:
        from floati.approvals import ApprovalLedger

        ledger = ApprovalLedger(self.root)
        request = self.request()
        denied = ledger.decide(
            request["id"],
            public_ids.reviewer(),
            "denied",
            "operator_denied",
            now=NOW + timedelta(seconds=2),
        )

        self.assertEqual("denied", denied["decision"])
        self.assertEqual("operator_denied", denied["reason_code"])
        self.assertIsNone(denied["granted_scope"])
        self.assertIsNone(denied["granted_ttl_seconds"])
        self.assertIsNone(denied["expires_at"])

    def test_request_requires_exact_live_authority_binding(self) -> None:
        from floati.approvals import ApprovalLedger

        ledger = ApprovalLedger(self.root)
        cases = (
            ("missing", self.grant["epoch"], NOW + timedelta(seconds=1), "authority_missing"),
            ("approve-build", self.grant["epoch"] + 1, NOW + timedelta(seconds=1), "authority_epoch_mismatch"),
            ("approve-build", self.grant["epoch"], NOW + timedelta(seconds=120), "authority_inactive"),
        )
        for subject, epoch, current, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    ledger.request(
                        public_ids.worker('alpha'), "workspace.patch", "repo:slipway", 60,
                        subject, epoch, now=current,
                    )
                self.assertEqual(code, caught.exception.code)

    def test_capability_modes_are_tri_state_and_expire_honestly(self) -> None:
        from floati.approvals import CapabilityLedger

        ledger = CapabilityLedger(self.root)
        record = ledger.declare(
            public_ids.worker('alpha'), "workspace.patch", "read_only", "repo:slipway", 30, now=NOW
        )

        self.assertEqual("capability", record["kind"])
        self.assertEqual("read_only", ledger.current(public_ids.worker('alpha'), "workspace.patch", NOW)["mode"])
        self.assertEqual("expired", ledger.current(
            public_ids.worker('alpha'), "workspace.patch", NOW + timedelta(seconds=30)
        )["status"])
        with self.assertRaises(ProtocolRefusal) as boolean_mode:
            ledger.declare(public_ids.worker('alpha'), "workspace.patch", True, "repo:slipway", 30, now=NOW)
        self.assertEqual("capability_mode_invalid", boolean_mode.exception.code)


if __name__ == "__main__":
    unittest.main()
