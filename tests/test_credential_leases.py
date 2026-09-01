from __future__ import annotations

import tempfile
import unittest
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.grants import AuthorityGrantService
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import AuthorityGrantStore
from floati.policy import Policy
from floati.records import capability_set_digest
from floati.registry import REGISTRY_KINDS, Registry
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from floati.runtruth import RUN_KINDS
from tests.test_policy import VALID_POLICY

try:
    from floati.credential_leases import (
        CredentialDelivery,
        CredentialLeaseLedger,
        CredentialLeaseService,
    )
except (ImportError, ModuleNotFoundError):
    CredentialDelivery = CredentialLeaseService = None
    try:
        from floati.credential_leases import CredentialLeaseLedger
    except ModuleNotFoundError:
        CredentialLeaseLedger = None


NOW = datetime(2026, 8, 30, 18, 30, 0, tzinfo=timezone.utc)


class CredentialLeaseLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root = FloatiRoot.open_direct_home(base / "alpha", create=True)
        self.assertIsNotNone(
            CredentialLeaseLedger,
            "floati.credential_leases must provide the v1 credential lease ledger",
        )
        self.ledger = CredentialLeaseLedger(self.root)
        self.attempt_id = "attempt-" + uuid7_hex()
        self.authority_record_id = "authority-" + uuid7_hex()
        self.capability_set_bound_id = "capability-set-bound-" + uuid7_hex()

    def issue(self, **overrides):
        arguments = {
            "attempt_id": self.attempt_id,
            "principal": "builder-a",
            "capability": "workspace_write",
            "secret_alias": "openrouter-api",
            "ttl_seconds": 30,
            "authority_epoch": 1,
            "authority_record_id": self.authority_record_id,
            "capability_set_bound_id": self.capability_set_bound_id,
            "now": NOW,
        }
        arguments.update(overrides)
        return self.ledger.issue(**arguments)

    def test_issue_binds_every_coordinate_without_persisting_secret_bytes(self) -> None:
        secret_fixture = b"secret-fixture-never-persisted-4d3586"

        lease = self.issue()

        self.assertEqual(1, lease["schema_version"])
        self.assertEqual("credential_lease_granted", lease["kind"])
        self.assertEqual(self.attempt_id, lease["attempt_id"])
        self.assertEqual("builder-a", lease["principal"])
        self.assertEqual("workspace_write", lease["capability"])
        self.assertEqual("openrouter-api", lease["secret_alias"])
        self.assertEqual(30, lease["ttl_seconds"])
        self.assertEqual("2026-08-30T18:30:30.000Z", lease["expires_at"])
        path = self.root.resolve_relative("credentials/leases.jsonl")
        self.assertNotIn(secret_fixture, path.read_bytes())

    def test_issue_refuses_missing_wildcard_or_nonpositive_binding_coordinates(self) -> None:
        cases = (
            {"attempt_id": ""},
            {"principal": "*"},
            {"capability": "*"},
            {"secret_alias": "*"},
            {"ttl_seconds": 0},
            {"authority_epoch": 0},
            {"authority_record_id": ""},
            {"capability_set_bound_id": ""},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.issue(**changes)
                self.assertEqual("credential_lease_binding_invalid", caught.exception.code)
        self.assertEqual([], self.ledger.records())

    def test_expiry_is_evaluated_without_an_appended_expiry_record(self) -> None:
        lease = self.issue(ttl_seconds=2)

        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.active(lease["id"], now=NOW + timedelta(seconds=2))

        self.assertEqual("credential_lease_expired", caught.exception.code)
        self.assertEqual(["credential_lease_granted"], [row["kind"] for row in self.ledger.records()])

    def test_consumption_requires_one_live_grant_and_can_happen_once(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.consume(
                "credential-lease-" + uuid7_hex(),
                "helper_response",
                now=NOW,
            )
        self.assertEqual("credential_lease_unknown", caught.exception.code)
        lease = self.issue()

        receipt = self.ledger.consume(
            lease["id"], "helper_response", now=NOW + timedelta(seconds=1)
        )

        self.assertEqual("credential_lease_consumed", receipt["kind"])
        self.assertEqual(lease["id"], receipt["lease_id"])
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.consume(
                lease["id"], "helper_response", now=NOW + timedelta(seconds=2)
            )
        self.assertEqual("credential_lease_already_consumed", caught.exception.code)

    def test_alias_revocation_appends_once_and_blocks_new_consumption(self) -> None:
        lease = self.issue()

        revoked = self.ledger.revoke_alias(
            "openrouter-api", "builder-a", 1, now=NOW + timedelta(seconds=1)
        )
        replay = self.ledger.revoke_alias(
            "openrouter-api", "builder-a", 1, now=NOW + timedelta(seconds=2)
        )

        self.assertEqual(1, len(revoked))
        self.assertEqual("credential_lease_revoked", revoked[0]["kind"])
        self.assertEqual([], replay)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.consume(
                lease["id"], "inherited_fd", now=NOW + timedelta(seconds=3)
            )
        self.assertEqual("credential_lease_revoked", caught.exception.code)


class CredentialLeaseServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "alpha", create=True)
        self.assertIsNotNone(
            CredentialLeaseService,
            "floati.credential_leases must provide the internal launch service",
        )
        self.assertIsNotNone(
            CredentialDelivery,
            "floati.credential_leases must provide secret-safe delivery values",
        )
        self.ledger = CredentialLeaseLedger(self.root)
        self.service = CredentialLeaseService(self.root)
        self.authority = AuthorityGrantStore(self.root).grant_exact(
            "openrouter-api", "builder-a", 1, NOW
        )
        self.attempt_id = "attempt-" + uuid7_hex()
        self.snapshot = self._persisted_snapshot()

    def _persisted_snapshot(self) -> dict[str, object]:
        effective_grants = [{
            "capability_name": "workspace_write",
            "grant_id": "capability-grant-" + uuid7_hex(),
            "physical_position": 1,
        }]
        snapshot = {
            "schema_version": 1,
            "id": "capability-set-bound-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-30T18:30:00.000Z",
            "kind": "capability_set_bound",
            "run_id": "run-" + uuid7_hex(),
            "item_id": "work-" + uuid7_hex(),
            "attempt_id": self.attempt_id,
            "fence_token": "a" * 64,
            "chosen_worker": "builder-a",
            "policy_digest": "b" * 64,
            "routing_rank": 0,
            "evaluated_at_testimony": "2026-08-30T18:30:00.000Z",
            "grant_ledger_high_watermark": 1,
            "effective_grants": effective_grants,
            "capability_digest": capability_set_digest(effective_grants),
        }
        append_record(
            self.root,
            "runs/events.jsonl",
            snapshot,
            allowed_kinds=set(RUN_KINDS),
        )
        return snapshot

    def policy(self, isolation: str | None) -> Policy:
        text = VALID_POLICY
        if isolation is not None:
            text = text.replace(
                "max_concurrency = 1",
                f'max_concurrency = 1\nsecret_isolation = "{isolation}"',
                1,
            )
        policy_root = self.base / f"policy-{isolation or 'absent'}"
        policy_root.mkdir(exist_ok=True)
        path = policy_root / "FLOATI.toml"
        path.write_text(text, encoding="utf-8")
        return Policy.load(path)

    def issue(self, **overrides):
        arguments = {
            "snapshot": self.snapshot,
            "attempt_id": self.attempt_id,
            "principal": "builder-a",
            "capability": "workspace_write",
            "secret_alias": "openrouter-api",
            "authority_epoch": 1,
            "ttl_seconds": 30,
            "now": NOW + timedelta(seconds=1),
        }
        arguments.update(overrides)
        return self.service.issue(**arguments)

    def helper(self, body: str) -> tuple[str, ...]:
        return (sys.executable, "-c", body)

    def test_issue_requires_persisted_snapshot_and_current_alias_authority(self) -> None:
        lease = self.issue()

        self.assertEqual(self.snapshot["id"], lease["capability_set_bound_id"])
        cases = (
            ({"principal": "bob"}, "credential_lease_snapshot_mismatch"),
            ({"capability": "review"}, "credential_lease_capability_missing"),
            ({"authority_epoch": 2}, "credential_lease_authority_mismatch"),
            ({"snapshot": dict(self.snapshot, id="capability-set-bound-" + uuid7_hex())},
             "credential_lease_snapshot_missing"),
        )
        for changes, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.issue(**changes)
                self.assertEqual(code, caught.exception.code)

    def test_delivery_refuses_absent_none_and_released_authority_before_consumption(self) -> None:
        lease = self.issue()
        helper = self.helper("import sys; sys.stdin.readline(); print('opaque-value')")
        for policy, code in (
            (self.policy(None), "credential_lease_adapter_undeclared"),
            (self.policy("none"), "credential_lease_delivery_unavailable"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.service.deliver(
                        lease["id"], policy, "codex", helper, now=NOW + timedelta(seconds=2)
                    )
                self.assertEqual(code, caught.exception.code)
        AuthorityGrantStore(self.root).release(
            "openrouter-api", "builder-a", 1, NOW + timedelta(seconds=3)
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.service.deliver(
                lease["id"], self.policy("helper"), "codex", helper,
                now=NOW + timedelta(seconds=4),
            )
        self.assertEqual("credential_lease_authority_inactive", caught.exception.code)
        self.assertEqual(
            ["credential_lease_granted"],
            [row["kind"] for row in self.ledger.records()],
        )

    def test_helper_response_records_consumption_before_secret_resolution(self) -> None:
        lease = self.issue()
        ledger_path = self.root.resolve_relative("credentials/leases.jsonl")
        secret = b"receipt-first-secret-79db"
        body = (
            "import pathlib,sys; "
            "alias=sys.stdin.readline(); "
            "data=pathlib.Path(sys.argv[1]).read_bytes(); "
            "assert alias == 'openrouter-api\\n'; "
            "assert b'credential_lease_consumed' in data; "
            "sys.stdout.buffer.write(b'receipt-first-secret-79db\\n')"
        )

        delivery = self.service.deliver(
            lease["id"],
            self.policy("helper"),
            "codex",
            (sys.executable, "-c", body, str(ledger_path)),
            now=NOW + timedelta(seconds=2),
        )

        self.assertEqual("helper_response", delivery.mode)
        self.assertEqual(secret, delivery.helper_response)
        self.assertIsNone(delivery.inherited_fd)
        self.assertNotIn(secret.decode(), repr(delivery))

    def test_process_delivery_exposes_only_an_inheritable_fd(self) -> None:
        lease = self.issue()
        secret = b"fd-only-secret-52c1"

        delivery = self.service.deliver(
            lease["id"],
            self.policy("process"),
            "codex",
            self.helper(
                "import sys; sys.stdin.readline(); "
                "sys.stdout.buffer.write(b'fd-only-secret-52c1\\n')"
            ),
            now=NOW + timedelta(seconds=2),
        )
        self.addCleanup(delivery.close)

        self.assertEqual("inherited_fd", delivery.mode)
        self.assertIsNone(delivery.helper_response)
        self.assertIsInstance(delivery.inherited_fd, int)
        self.assertTrue(os.get_inheritable(delivery.inherited_fd))
        self.assertEqual(secret, os.read(delivery.inherited_fd, 4096))
        self.assertNotIn(secret.decode(), repr(delivery))

    def test_helper_failure_never_discloses_stdout_or_stderr(self) -> None:
        lease = self.issue()
        stdout_secret = "stdout-secret-33b0"
        stderr_secret = "stderr-secret-e407"
        helper = self.helper(
            "import sys; sys.stdin.readline(); "
            f"print('{stdout_secret}'); "
            f"sys.stderr.write('{stderr_secret}'); sys.exit(7)"
        )

        with self.assertRaises(ProtocolRefusal) as caught:
            self.service.deliver(
                lease["id"], self.policy("helper"), "codex", helper,
                now=NOW + timedelta(seconds=2),
            )

        self.assertEqual("credential_lease_helper_refused", caught.exception.code)
        self.assertNotIn(stdout_secret, caught.exception.detail)
        self.assertNotIn(stderr_secret, caught.exception.detail)
        self.assertNotIn(stdout_secret.encode(), self.root.resolve_relative(
            "credentials/leases.jsonl"
        ).read_bytes())
        self.assertNotIn(stderr_secret.encode(), self.root.resolve_relative(
            "credentials/leases.jsonl"
        ).read_bytes())


class CredentialLeasePublicSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "alpha", create=True)
        self.registry = Registry(self.root)
        self.registry.register("architect-a", "Codex")
        self.registry.register("builder-a", "Codex")
        template = load_shipped_role_templates(Path("roles/shipped"))["architect"]
        append_record(
            self.root,
            self.registry.relative_path,
            {
                "schema_version": 0,
                "id": "registry-role-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-08-30T18:30:00.000Z",
                "kind": "registry_role_record",
                "node_id": "architect-a",
                "template_role": "architect",
                "template_version": template.template_version,
                "template_sha256": template.digest,
                "answers": {
                    question.key: "fixture" for question in template.questions
                },
                "state": "active",
                "predecessor_role_record_id": None,
            },
            allowed_kinds=REGISTRY_KINDS,
        )
        self.grants = AuthorityGrantService(self.root)
        self.lease_service = CredentialLeaseService(self.root)
        self.lease_ledger = CredentialLeaseLedger(self.root)
        self.attempt_id = "attempt-" + uuid7_hex()
        self.snapshot = self._persisted_snapshot()

    def _persisted_snapshot(self) -> dict[str, object]:
        effective_grants = [{
            "capability_name": "workspace_write",
            "grant_id": "capability-grant-" + uuid7_hex(),
            "physical_position": 1,
        }]
        snapshot = {
            "schema_version": 1,
            "id": "capability-set-bound-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-30T18:30:00.000Z",
            "kind": "capability_set_bound",
            "run_id": "run-" + uuid7_hex(),
            "item_id": "work-" + uuid7_hex(),
            "attempt_id": self.attempt_id,
            "fence_token": "a" * 64,
            "chosen_worker": "builder-a",
            "policy_digest": "b" * 64,
            "routing_rank": 0,
            "evaluated_at_testimony": "2026-08-30T18:30:00.000Z",
            "grant_ledger_high_watermark": 1,
            "effective_grants": effective_grants,
            "capability_digest": capability_set_digest(effective_grants),
        }
        append_record(
            self.root, "runs/events.jsonl", snapshot,
            allowed_kinds=set(RUN_KINDS),
        )
        return snapshot

    def issue(self, alias: str, epoch: int, when: datetime):
        return self.lease_service.issue(
            self.snapshot,
            self.attempt_id,
            "builder-a",
            "workspace_write",
            alias,
            epoch,
            30,
            now=when,
        )

    def test_public_grant_creates_alias_authority_only_and_revoke_blocks_delivery(self) -> None:
        granted = self.grants.grant(
            "architect-a", "builder-a", "openrouter-api", 1, now=NOW
        )

        self.assertEqual("authority_grant", granted["record"]["kind"])
        self.assertFalse(
            self.root.resolve_relative("credentials/leases.jsonl").exists()
        )
        lease = self.issue("openrouter-api", 1, NOW + timedelta(seconds=1))

        revoked = self.grants.revoke(
            "architect-a", "builder-a", "openrouter-api", 1,
            now=NOW + timedelta(seconds=2),
        )

        revocations = revoked.get("credential_lease_revocations")
        self.assertIsNotNone(
            revocations, "public revoke must project credential lease revocations"
        )
        self.assertEqual(1, len(revocations))
        self.assertEqual(
            "credential_lease_revoked",
            revocations[0]["kind"],
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.lease_ledger.active(lease["id"], now=NOW + timedelta(seconds=3))
        self.assertEqual("credential_lease_revoked", caught.exception.code)

    def test_exact_public_revoke_replay_repairs_missing_lease_projection(self) -> None:
        self.grants.grant(
            "architect-a", "builder-a", "second-api", 1, now=NOW
        )
        lease = self.issue("second-api", 1, NOW + timedelta(seconds=1))
        AuthorityGrantStore(self.root).release(
            "second-api", "builder-a", 1, NOW + timedelta(seconds=2)
        )
        self.assertEqual(
            ["credential_lease_granted"],
            [row["kind"] for row in self.lease_ledger.records()],
        )

        repaired = self.grants.revoke(
            "architect-a", "builder-a", "second-api", 1,
            now=NOW + timedelta(seconds=3),
        )
        replay = self.grants.revoke(
            "architect-a", "builder-a", "second-api", 1,
            now=NOW + timedelta(seconds=4),
        )

        repaired_rows = repaired.get("credential_lease_revocations")
        replay_rows = replay.get("credential_lease_revocations")
        self.assertIsNotNone(
            repaired_rows, "public replay must repair missing lease projection"
        )
        self.assertIsNotNone(
            replay_rows, "exact replay must expose its empty lease projection"
        )
        self.assertEqual(lease["id"], repaired_rows[0]["lease_id"])
        self.assertEqual([], replay_rows)


if __name__ == "__main__":
    unittest.main()
