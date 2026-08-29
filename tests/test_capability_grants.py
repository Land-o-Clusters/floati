from __future__ import annotations

import dataclasses
import tempfile
import unittest
import multiprocessing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.approvals import ApprovalLedger
from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
from floati.planes import AuthorityGrantStore
from floati.policy import Policy
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.test_policy import VALID_POLICY

try:
    from floati.capabilities import CapabilityGrantLedger, capability_grant_digest
except ModuleNotFoundError:
    CapabilityGrantLedger = capability_grant_digest = None


NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


def _race_grant_and_authority_release(
    root_path, policy_path, request_id, decision_id, epoch, action, start, queue,
):
    try:
        root = FloatiRoot.open_direct_home(Path(root_path), create=False)
        start.wait(5)
        if action == "grant":
            result = CapabilityGrantLedger(root).grant(
                "alice", "workspace_write", Policy.load(Path(policy_path)),
                request_id, decision_id, now=NOW + timedelta(seconds=3),
            )
        else:
            result = AuthorityGrantStore(root).release(
                "approve-build", "fable", epoch, NOW + timedelta(seconds=3)
            )
        queue.put(("ok", result["kind"]))
    except ProtocolRefusal as exc:
        queue.put(("refused", exc.code))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


class CapabilityGrantLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root = FloatiRoot.open_direct_home(base / "alpha", create=True)
        self.policy_path = base / "FLOATI.toml"
        self.policy_path.write_text(VALID_POLICY, encoding="utf-8")
        self.policy = Policy.load(self.policy_path)
        registry = Registry(self.root)
        registry.register("alice", "Codex")
        registry.register("fable", "Claude")
        self.authority_store = AuthorityGrantStore(self.root)
        self.authority = self.authority_store.claim(
            "approve-build", "fable", 120, 120, NOW
        )
        approvals = ApprovalLedger(self.root)
        self.request = approvals.request(
            "alice", "workspace_write", "worker:alice", 60,
            "approve-build", self.authority["epoch"], now=NOW + timedelta(seconds=1),
        )
        self.decision = approvals.decide(
            self.request["id"], "fable", "approved", None,
            granted_scope="worker:alice", granted_ttl_seconds=30,
            now=NOW + timedelta(seconds=2),
        )
        self.assertIsNotNone(CapabilityGrantLedger, "floati.capabilities must provide the v1 grant ledger")
        self.ledger = CapabilityGrantLedger(self.root)

    def grant(self, **overrides):
        arguments = {
            "worker_id": "alice",
            "capability_name": "workspace_write",
            "policy": self.policy,
            "approval_request_id": self.request["id"],
            "approval_decision_id": self.decision["id"],
            "now": NOW + timedelta(seconds=3),
        }
        arguments.update(overrides)
        return self.ledger.grant(**arguments)

    def test_grant_resolves_exact_approved_worker_capability_and_digest(self) -> None:
        """Catches declarations, caller flags, or mismatched approvals becoming capability authority."""
        grant = self.grant()

        self.assertEqual(1, grant["schema_version"])
        self.assertEqual("capability_grant", grant["kind"])
        self.assertEqual(self.policy.digest, grant["policy_digest"])
        self.assertEqual(self.request["id"], grant["approval_request_id"])
        self.assertEqual(self.decision["id"], grant["approval_decision_id"])
        self.assertEqual(capability_grant_digest(grant), grant["grant_digest"])
        effective = self.ledger.effective(
            "alice", self.policy.digest, NOW + timedelta(seconds=4)
        )
        self.assertEqual(
            [("workspace_write", grant["id"], 1)],
            effective.grant_triples,
        )
        self.assertEqual(1, effective.high_watermark)

    def test_grant_mismatch_or_expiry_refuses_before_append(self) -> None:
        """Catches a grant that broadens approval identity, vocabulary, or lifetime."""
        path = self.root.resolve_relative("capabilities/grants.jsonl")
        cases = (
            ({"worker_id": "fable"}, "capability_worker_mismatch"),
            ({"capability_name": "review"}, "capability_approval_mismatch"),
            ({"capability_name": "shell_exec"}, "capability_unregistered"),
            ({"now": NOW + timedelta(seconds=32)}, "capability_approval_expired"),
        )
        for arguments, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.grant(**arguments)
                self.assertEqual(code, caught.exception.code)
                self.assertFalse(path.exists())

    def test_grant_refuses_stale_or_released_approval_authority(self) -> None:
        """Catches an approved decision becoming fresh grant authority after its authority epoch closes."""
        self.authority_store.release(
            "approve-build", "fable", self.authority["epoch"],
            NOW + timedelta(seconds=3),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.grant(now=NOW + timedelta(seconds=4))
        self.assertEqual("capability_authority_inactive", caught.exception.code)
        self.assertFalse(self.root.resolve_relative("capabilities/grants.jsonl").exists())

    def test_grant_and_authority_release_race_is_serialized_by_one_cas_order(self) -> None:
        """Catches release landing between authority observation and the grant append."""
        context = multiprocessing.get_context("fork")
        start = context.Event()
        queue = context.Queue()
        processes = [
            context.Process(
                target=_race_grant_and_authority_release,
                args=(
                    str(self.root.path), str(self.policy_path), self.request["id"],
                    self.decision["id"], self.authority["epoch"], action, start, queue,
                ),
            )
            for action in ("grant", "release")
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(6)
            self.assertEqual(0, process.exitcode)
        results = [queue.get(timeout=1) for _ in processes]
        self.assertNotIn("error", {status for status, _ in results})
        self.assertEqual(1, sum(value == "authority_grant" for _, value in results))
        self.assertLessEqual(sum(value == "capability_grant" for _, value in results), 1)
        authority = read_records(
            self.root,
            Path("authority-grants/approve-build.jsonl"),
            allowed_kinds={"authority_grant"},
        )[-1]
        self.assertEqual("released", authority["state"])

    def test_one_approval_decision_materializes_one_grant_even_after_revocation(self) -> None:
        """Catches reminting a revoked capability repeatedly from one approval receipt."""
        grant = self.grant()
        self.ledger.revoke(
            grant["id"], "operator_revoked", now=NOW + timedelta(seconds=4)
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.grant(now=NOW + timedelta(seconds=5))
        self.assertEqual("capability_approval_reused", caught.exception.code)
        self.assertEqual(2, len(self.ledger.records()))

    def test_forged_policy_semantic_cache_refuses_before_grant_append(self) -> None:
        """Catches a dataclass replacement retaining a stale trusted policy digest."""
        forged = dataclasses.replace(
            self.policy,
            capability_registry=self.policy.capability_registry + ("shell_exec",),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.grant(policy=forged)
        self.assertEqual("policy_integrity_invalid", caught.exception.code)
        self.assertFalse(self.root.resolve_relative("capabilities/grants.jsonl").exists())

    def test_revocation_and_expiry_change_only_future_effective_snapshots(self) -> None:
        """Catches timestamp sorting, deletion, or replay-time expiry reinterpretation."""
        grant = self.grant()
        revoked = self.ledger.revoke(
            grant["id"], "operator_revoked", now=NOW + timedelta(seconds=4)
        )
        self.assertEqual("capability_revoked", revoked["kind"])
        self.assertEqual([], self.ledger.effective(
            "alice", self.policy.digest, NOW + timedelta(seconds=5)
        ).grant_triples)

        with self.assertRaises(ProtocolRefusal) as duplicate:
            self.ledger.revoke(
                grant["id"], "authority_revoked", now=NOW + timedelta(seconds=6)
            )
        self.assertEqual("capability_already_revoked", duplicate.exception.code)

        approvals = ApprovalLedger(self.root)
        second_request = approvals.request(
            "alice", "workspace_write", "worker:alice", 60,
            "approve-build", self.authority["epoch"], now=NOW + timedelta(seconds=4),
        )
        second_decision = approvals.decide(
            second_request["id"], "fable", "approved", None,
            granted_scope="worker:alice", granted_ttl_seconds=20,
            now=NOW + timedelta(seconds=5),
        )
        second = self.grant(
            approval_request_id=second_request["id"],
            approval_decision_id=second_decision["id"],
            now=NOW + timedelta(seconds=6),
        )
        self.assertEqual([], self.ledger.effective(
            "alice", self.policy.digest, NOW + timedelta(seconds=32)
        ).grant_triples)
        with self.assertRaises(ProtocolRefusal) as replacement:
            self.ledger.revoke(
                second["id"], "policy_replaced", now=NOW + timedelta(seconds=5)
            )
        self.assertEqual("replacement_policy_digest_required", replacement.exception.code)
        with self.assertRaises(ProtocolRefusal) as unchanged:
            self.ledger.revoke(
                second["id"], "policy_replaced",
                replacement_policy_digest=self.policy.digest,
                now=NOW + timedelta(seconds=5),
            )
        self.assertEqual("replacement_policy_digest_unchanged", unchanged.exception.code)

    def test_corrupt_persisted_grant_digest_yields_no_partial_projection(self) -> None:
        """Catches a grant reader returning earlier authority after later physical corruption."""
        grant = self.grant()
        path = self.root.resolve_relative("capabilities/grants.jsonl")
        data = path.read_text(encoding="utf-8").replace(
            grant["grant_digest"], "0" * 64
        )
        path.write_text(data, encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.effective("alice", self.policy.digest, NOW + timedelta(seconds=4))
        self.assertEqual("capability_grant_digest_invalid", caught.exception.code)

    def test_grant_reader_framing_vectors_fail_closed_with_typed_integrity(self) -> None:
        """Covers malformed, truncated, duplicate, oversized, and non-UTF8 grant history."""
        grant = self.grant()
        frame = encode_frame(grant)
        path = self.root.resolve_relative(self.ledger.relative_path)
        mutations = {
            "malformed": (b"{bad}\n", "malformed_json"),
            "truncated": (frame[:-1], "incomplete_jsonl_line"),
            "duplicated": (frame + frame, "duplicate_record_id"),
            "oversized": (b'{"value":"' + b"a" * 1_048_576 + b'"}\n', "record_too_large"),
            "invalid_utf8": (b'{"value":"\xff"}\n', "malformed_json"),
        }
        for name, (payload, code) in mutations.items():
            with self.subTest(name=name):
                path.write_bytes(payload)
                with self.assertRaises(IntegrityFailure) as caught:
                    self.ledger.records()
                self.assertEqual(code, caught.exception.code)

    def test_revoke_refuses_to_extend_corrupt_lifecycle_and_preserves_bytes(self) -> None:
        """Catches revocation writers appending after a causally corrupt durable prefix."""
        grant = self.grant()
        path = self.root.resolve_relative(self.ledger.relative_path)
        forward = {
            "schema_version": 1,
            "id": "capability-revoked-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-08T12:00:04.000Z",
            "kind": "capability_revoked",
            "grant_id": "capability-grant-" + uuid7_hex(),
            "reason_code": "operator_revoked",
            "replacement_policy_digest": None,
        }
        path.write_bytes(encode_frame(grant) + encode_frame(forward))
        before = path.read_bytes()
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.revoke(
                grant["id"], "operator_revoked", now=NOW + timedelta(seconds=5)
            )
        self.assertEqual("capability_revocation_forward", caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def _append_forged_grant(self, **overrides):
        record = {
            "schema_version": 1,
            "id": "capability-grant-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-08T12:00:03.000Z",
            "kind": "capability_grant",
            "worker_id": "alice",
            "capability_name": "workspace_write",
            "policy_digest": self.policy.digest,
            "approval_request_id": self.request["id"],
            "approval_decision_id": self.decision["id"],
            "authority_subject": "approve-build",
            "authority_epoch": self.authority["epoch"],
            "expires_at": self.decision["expires_at"],
        }
        record.update(overrides)
        record["grant_digest"] = capability_grant_digest(record)
        append_record(
            self.root,
            self.ledger.relative_path,
            record,
            allowed_kinds={"capability_grant", "capability_revoked"},
        )
        return record

    def test_persisted_grant_missing_approval_evidence_is_integrity_failure(self) -> None:
        """Catches a shape-valid grant becoming authority without its durable approval join."""
        self._append_forged_grant(
            approval_request_id="approval-request-" + uuid7_hex(),
            approval_decision_id="approval-decision-" + uuid7_hex(),
        )
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.effective("alice", self.policy.digest, NOW + timedelta(seconds=4))
        self.assertEqual("capability_approval_missing", caught.exception.code)

    def test_persisted_grant_mismatched_approval_evidence_is_integrity_failure(self) -> None:
        """Catches a real approval being rebound to a different capability during replay."""
        self._append_forged_grant(capability_name="review")
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.effective("alice", self.policy.digest, NOW + timedelta(seconds=4))
        self.assertEqual("capability_approval_mismatch", caught.exception.code)

    def test_persisted_grant_denied_approval_evidence_is_integrity_failure(self) -> None:
        """Catches a denied decision being presented as durable grant authority."""
        approvals = ApprovalLedger(self.root)
        request = approvals.request(
            "alice", "review", "worker:alice", 60,
            "approve-build", self.authority["epoch"], now=NOW + timedelta(seconds=4),
        )
        decision = approvals.decide(
            request["id"], "fable", "denied", "operator_denied",
            now=NOW + timedelta(seconds=5),
        )
        self._append_forged_grant(
            capability_name="review",
            approval_request_id=request["id"],
            approval_decision_id=decision["id"],
            expires_at="2026-08-08T12:00:20.000Z",
        )
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.effective("alice", self.policy.digest, NOW + timedelta(seconds=6))
        self.assertEqual("capability_approval_denied", caught.exception.code)

    def test_persisted_approval_ttl_arithmetic_is_rederived(self) -> None:
        """Catches forged expiry testimony broadening a decision's declared TTL."""
        approvals = ApprovalLedger(self.root)
        request = approvals.request(
            "alice", "review", "worker:alice", 60,
            "approve-build", self.authority["epoch"], now=NOW + timedelta(seconds=4),
        )
        decision = {
            "schema_version": 0,
            "id": "approval-decision-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-08T12:00:05.000Z",
            "kind": "approval_decision",
            "request_id": request["id"],
            "decider": "fable",
            "decision": "approved",
            "granted_scope": "worker:alice",
            "granted_ttl_seconds": 1,
            "reason_code": None,
            "decided_at": "2026-08-08T12:00:05.000Z",
            "expires_at": "2026-08-08T12:00:50.000Z",
            "authority_subject": "approve-build",
            "authority_epoch": self.authority["epoch"],
        }
        append_record(
            self.root,
            Path("approvals/decisions.jsonl"),
            decision,
            allowed_kinds={"approval_decision"},
        )
        self._append_forged_grant(
            capability_name="review",
            approval_request_id=request["id"],
            approval_decision_id=decision["id"],
            expires_at=decision["expires_at"],
        )
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.effective("alice", self.policy.digest, NOW + timedelta(seconds=40))
        self.assertEqual("capability_approval_lifetime_invalid", caught.exception.code)

    def test_forward_revocation_is_integrity_failure_and_timestamp_is_testimony(self) -> None:
        """Catches lifecycle projection sorting by timestamp or accepting a forward revocation."""
        grant = self.grant()
        baseline = self.ledger.effective(
            "alice", self.policy.digest, NOW + timedelta(seconds=4)
        ).grant_triples
        path = self.root.resolve_relative("capabilities/grants.jsonl")
        testimony_changed = dict(grant, timestamp="2030-01-01T00:00:00.000Z")
        path.write_bytes(encode_frame(testimony_changed))
        self.assertEqual(
            baseline,
            self.ledger.effective(
                "alice", self.policy.digest, NOW + timedelta(seconds=4)
            ).grant_triples,
        )

        path.write_bytes(encode_frame(grant))
        revoked = self.ledger.revoke(
            grant["id"], "operator_revoked", now=NOW + timedelta(seconds=5)
        )
        path.write_bytes(encode_frame(revoked) + encode_frame(grant))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.effective("alice", self.policy.digest, NOW + timedelta(seconds=6))
        self.assertEqual("capability_revocation_forward", caught.exception.code)

    def test_grant_and_revocation_short_writes_roll_back_exactly(self) -> None:
        """Catches either new external-ledger kind leaving a partial durable frame."""
        path = self.root.resolve_relative("capabilities/grants.jsonl")
        with patch("floati.jsonl.os.write", return_value=1):
            with self.assertRaises(DurabilityFailure) as grant_failure:
                self.grant()
        self.assertEqual("short_write", grant_failure.exception.code)
        self.assertEqual(b"", path.read_bytes())

        grant = self.grant()
        before = path.read_bytes()
        with patch("floati.jsonl.os.write", return_value=1):
            with self.assertRaises(DurabilityFailure) as revoke_failure:
                self.ledger.revoke(
                    grant["id"], "operator_revoked", now=NOW + timedelta(seconds=4)
                )
        self.assertEqual("short_write", revoke_failure.exception.code)
        self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
