from __future__ import annotations

from floati import fixture_ids as public_ids

import dataclasses
import json
import tempfile
import unittest
import fcntl
import multiprocessing
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.approvals import ApprovalLedger
from floati.admission import AdmissionPlan
from floati.capabilities import CapabilityGrantLedger
from floati.contracts import TaskContract, contract_digest
from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.policy import CapabilitySelector, Policy
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.scheduler import RetryPolicy, RunScheduler
from floati.sequencer import SequencerClient, SequencerConfig, SequencerService
from tests.test_policy import VALID_POLICY

try:
    from floati.capability_binding import CapabilityBinder, capability_set_digest
except ModuleNotFoundError:
    CapabilityBinder = capability_set_digest = None

try:
    from floati.admission import AdmissionBinder
except ImportError:
    AdmissionBinder = None


NOW = datetime(2026, 8, 8, 13, 0, 0, tzinfo=timezone.utc)


def _race_binding(
    root_path, policy_path, run_id, item_id, attempt_id, grant_id,
    action, start, queue,
):
    try:
        root = FloatiRoot.open_direct_home(Path(root_path), create=False)
        start.wait(5)
        if action == "bind":
            policy = Policy.load(Path(policy_path))
            result = CapabilityBinder(
                RunLedger(root), CapabilityGrantLedger(root)
            ).bind(
                run_id, item_id, attempt_id, public_ids.worker('alpha'), "codex", policy, 0,
                now=NOW + timedelta(seconds=11),
            )
        else:
            result = CapabilityGrantLedger(root).revoke(
                grant_id, "operator_revoked", now=NOW + timedelta(seconds=11)
            )
        queue.put(("ok", result["kind"]))
    except ProtocolRefusal as exc:
        queue.put(("refused", exc.code))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


def _hold_grant_lock(root_path, ready):
    path = Path(root_path) / "capabilities" / "grants.jsonl.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()
        time.sleep(2)


def _race_dispatch(root_path, policy_path, snapshot_id, start, queue):
    try:
        root = FloatiRoot.open_direct_home(Path(root_path), create=False)
        policy = Policy.load(Path(policy_path))
        binder = CapabilityBinder(RunLedger(root), CapabilityGrantLedger(root))
        start.wait(5)
        result = binder.dispatch(
            snapshot_id, [public_ids.worker('alpha')], "policy.route", policy,
            now=NOW + timedelta(seconds=20),
        )
        queue.put(("ok", result["id"]))
    except ProtocolRefusal as exc:
        queue.put(("refused", exc.code))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


class CapabilityBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(AdmissionBinder, "floati.admission must provide AdmissionBinder")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.root = FloatiRoot.open_direct_home(base / "alpha", create=True)
        policy_text = VALID_POLICY.replace("max_concurrency = 1", "max_concurrency = 2")
        self.policy_path = base / "FLOATI.toml"
        self.policy_path.write_text(policy_text, encoding="utf-8")
        self.policy = Policy.load(self.policy_path)
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "Codex")
        registry.register(public_ids.reviewer(), "Claude")
        authority = AuthorityGrantStore(self.root).claim(
            "approve-build", public_ids.reviewer(), 300, 300, NOW
        )
        self.approvals = ApprovalLedger(self.root)
        self.grants = CapabilityGrantLedger(self.root)
        for offset, capability in enumerate(("review", "workspace_write"), start=1):
            request = self.approvals.request(
                public_ids.worker('alpha'), capability, public_ids.compose('worker:', public_ids.worker('alpha')), 120,
                "approve-build", authority["epoch"], now=NOW + timedelta(seconds=offset),
            )
            decision = self.approvals.decide(
                request["id"], public_ids.reviewer(), "approved", None,
                granted_scope=public_ids.compose('worker:', public_ids.worker('alpha')), granted_ttl_seconds=90,
                now=NOW + timedelta(seconds=offset + 2),
            )
            self.grants.grant(
                public_ids.worker('alpha'), capability, self.policy, request["id"], decision["id"],
                now=NOW + timedelta(seconds=offset + 4),
            )
        self.ledger = RunLedger(self.root)
        self.scheduler = RunScheduler(self.ledger)
        self.run_id = "run-" + uuid7_hex()
        self.item_id = "work-" + uuid7_hex()
        self.contract = TaskContract.create(
            objective="bind dispatch capabilities", non_goals=["no bearer tokens"],
            areas_to_avoid=[{"path": "bundle/c7.1", "region": "all"}],
            input_hashes={"brief": "a" * 64},
            acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"}, risk_class="low",
            retry_policy={"max_attempts": 1, "backoff": {
                "base_delay_ms": 0, "cap_delay_ms": 0, "strategy": "fixed",
            }}, dependencies=[],
        )
        plan_value = {
            "schema_version": 0,
            "workers": [{"node_id": public_ids.worker('alpha'), "worker_profile": "codex"}],
            "max_active_attempts": 2,
            "budget_reservations": [{"budget_id": "build", "amount": 1}],
            "items": [{
                "item_id": self.item_id,
                "contract": self.contract.canonical(),
                "capability_selector": "review_write",
                "requires_cancellation": True,
                "requires_callback": True,
                "workspace_key": "workspace-bind",
                "concurrency_key": "concurrency-bind",
                "retry_class": "transient",
                "effect_safety": "idempotent",
                "merge_gate": None,
            }],
            "dependency_edges": [],
        }
        self.plan_path = base / "admission-plan.json"
        self.plan_path.write_text(json.dumps(plan_value, separators=(",", ":")), encoding="utf-8")
        self.plan = AdmissionPlan.load(self.plan_path)
        self._append("run_created", "run-created-", plan_digest=self.plan.digest,
                     policy_digest=self.policy.digest, item_ids=[self.item_id], dependency_edges=[])
        self._append("task_contract", "task-contract-", item_id=self.item_id,
                     **self.contract.canonical(), contract_digest=contract_digest(self.contract))
        self._append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
        self._append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=[public_ids.worker('alpha')])
        AdmissionBinder.bind(
            self.ledger, self.run_id, self.plan, self.policy,
            now=NOW + timedelta(seconds=9),
        )
        self.opened = self.scheduler.open_attempt(
            self.run_id, self.item_id,
            RetryPolicy(1, 0, 0, strategy="fixed"), 1,
            now=NOW + timedelta(seconds=10),
        )
        self.assertIsNotNone(CapabilityBinder, "floati.capability_binding must provide the ruled coordinator")
        self.binder = CapabilityBinder(self.ledger, self.grants)

    def _append(self, kind: str, prefix: str, **fields):
        return self.ledger.append({
            "schema_version": 0, "id": prefix + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": "2026-08-08T13:00:00.000Z",
            "kind": kind, "run_id": self.run_id, **fields,
        })

    def _legacy_open_attempt(self):
        run_id = "run-" + uuid7_hex()
        item_id = "work-" + uuid7_hex()
        contract = TaskContract.create(
            objective="preserve true legacy dispatch evidence",
            non_goals=["no admission backfill"],
            areas_to_avoid=[{"path": "schemas/v0", "region": "all"}],
            input_hashes={"brief": "f" * 64},
            acceptance_checks={"tests.legacy": "python3 -m unittest"},
            constraints={"network": "dark"}, risk_class="low",
            retry_policy={"max_attempts": 1, "backoff": {
                "base_delay_ms": 0, "cap_delay_ms": 0, "strategy": "fixed",
            }}, dependencies=[],
        )

        def append(kind, prefix, **fields):
            return self.ledger.append({
                "schema_version": 0, "id": prefix + uuid7_hex(),
                "tenant_id": "alpha", "timestamp": "2026-08-08T13:00:00.000Z",
                "kind": kind, "run_id": run_id, **fields,
            })

        append(
            "run_created", "run-created-", plan_digest="f" * 64,
            item_ids=[item_id], dependency_edges=[],
        )
        append(
            "task_contract", "task-contract-", item_id=item_id,
            **contract.canonical(), contract_digest=contract_digest(contract),
        )
        append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
        append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=[public_ids.worker('alpha')])
        opened = RunScheduler(self.ledger).open_attempt(
            run_id, item_id, RetryPolicy(1, 0, 0, strategy="fixed"), 1,
            now=NOW + timedelta(seconds=10),
        )
        return run_id, item_id, opened, append

    def test_snapshot_digest_and_dispatch_bind_exact_attempt_fence_and_grants(self) -> None:
        """Catches caller-declared capability sets or a snapshot drifting across an attempt fence."""
        snapshot = self.binder.bind(
            self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
            self.policy, 0, now=NOW + timedelta(seconds=11),
        )
        self.assertEqual("capability_set_bound", snapshot["kind"])
        self.assertEqual(self.opened["fence_token"], snapshot["fence_token"])
        self.assertEqual(2, len(snapshot["effective_grants"]))
        self.assertEqual(capability_set_digest(snapshot["effective_grants"]), snapshot["capability_digest"])
        dispatch = self.binder.dispatch(
            snapshot["id"], [public_ids.worker('alpha')], "policy.route", self.policy,
            now=NOW + timedelta(seconds=12),
        )
        self.assertEqual(snapshot["id"], dispatch["capability_set_bound_id"])
        self.assertEqual(snapshot["capability_digest"], dispatch["capability_digest"])
        projected = self.ledger.project().run(self.run_id)
        self.assertEqual("enforced_v1", projected["dispatches"][self.opened["attempt_id"]]["capability_enforcement"])

    def test_generic_binding_record_intent_cannot_reuse_stale_grant_testimony(self) -> None:
        """Catches a service minting binder authority around caller-constructed durable fields."""
        class CaptureClient:
            def __init__(self) -> None:
                self.record = None

            def append(self, _record):
                raise AssertionError("capability binding must use a typed intent")

            def append_intent(self, owner, record, policy=None):
                self.assert_owner = owner
                self.record = dict(record)
                return {"record": dict(record)}

        capture = CaptureClient()
        managed = RunLedger(self.root, sequencer_client=capture)
        captured = CapabilityBinder(managed, self.grants).bind(
            self.run_id,
            self.item_id,
            self.opened["attempt_id"],
            public_ids.worker('alpha'),
            "codex",
            self.policy,
            0,
            now=NOW + timedelta(seconds=11),
        )
        self.assertEqual("capability_binding", capture.assert_owner)
        self.assertEqual(2, len(captured["effective_grants"]))
        revoked = next(
            row for row in self.grants.records()
            if row["kind"] == "capability_grant"
            and row["capability_name"] == "workspace_write"
        )
        self.grants.revoke(
            revoked["id"], "operator_revoked", now=NOW + timedelta(seconds=12)
        )

        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01),
            clock=lambda: NOW + timedelta(seconds=11),
        )
        stop = threading.Event()
        worker = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
        worker.start()
        self.addCleanup(stop.set)
        self.addCleanup(worker.join, 3)
        self.addCleanup(service.close)
        client = SequencerClient(service.socket_path, service.epoch, "stale-binder")
        with self.assertRaises(ProtocolRefusal) as caught:
            client.append_intent("capability_binding", captured)
        self.assertEqual("intent_evidence_required", caught.exception.code)
        self.assertEqual({}, self.ledger.project().run(self.run_id)["capability_sets"])

    def test_managed_capability_binding_is_evaluated_and_appended_by_same_service_ledger(self) -> None:
        """Catches service evaluation minting authority on a different ledger than its append sink."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01),
            clock=lambda: NOW + timedelta(seconds=11),
        )
        stop = threading.Event()
        worker = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
        worker.start()

        def cleanup() -> None:
            stop.set()
            worker.join(3)
            service.close()

        self.addCleanup(cleanup)
        managed = RunLedger(
            self.root,
            sequencer_client=SequencerClient(
                service.socket_path, service.epoch, "managed-capability-binder"
            ),
        )
        bound = CapabilityBinder(managed, self.grants).bind(
            self.run_id,
            self.item_id,
            self.opened["attempt_id"],
            public_ids.worker('alpha'),
            "codex",
            self.policy,
            0,
            now=NOW + timedelta(seconds=11),
        )

        self.assertEqual("capability_set_bound", bound["kind"])
        self.assertEqual(bound, RunLedger(self.root).records()[-1])

    def test_managed_capability_binding_retry_after_lost_response_reuses_operation(self) -> None:
        """Catches a public binder retry minting a second snapshot after durable success."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01),
            clock=lambda: NOW + timedelta(seconds=11),
        )
        stop = threading.Event()
        worker = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
        worker.start()

        def cleanup() -> None:
            stop.set()
            worker.join(3)
            service.close()

        self.addCleanup(cleanup)

        class RecordingClient(SequencerClient):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.last_response = None

            def bind_capability(self, *args, **kwargs):
                response = super().bind_capability(*args, **kwargs)
                self.last_response = response
                return response

        client = RecordingClient(
            service.socket_path, service.epoch, "lost-managed-capability-response"
        )
        managed = RunLedger(self.root, sequencer_client=client)
        original = service._send_response
        dropped_response = None

        def drop_once(channel, response):
            nonlocal dropped_response
            if response.get("status") == "ok" and dropped_response is None:
                dropped_response = response
                channel.close()
                return
            return original(channel, response)

        service._send_response = drop_once
        with self.assertRaises(ProtocolRefusal) as caught:
            CapabilityBinder(managed, self.grants).bind(
                self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
                self.policy, 0, now=NOW + timedelta(seconds=11),
            )
        self.assertEqual("sequencer_response_lost", caught.exception.code)
        service._send_response = original

        retry = CapabilityBinder(managed, self.grants).bind(
            self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
            self.policy, 0, now=NOW + timedelta(seconds=12),
        )

        self.assertEqual(dropped_response, client.last_response)
        self.assertEqual(retry, client.last_response["record"])
        self.assertEqual(
            1,
            sum(record["kind"] == "capability_set_bound" for record in RunLedger(self.root).records()),
        )

    def test_service_clock_rejects_backdated_capability_binding_after_expiry(self) -> None:
        """Catches service grant evaluation trusting caller time after grants expire."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01),
            clock=lambda: NOW + timedelta(seconds=200),
        )
        stop = threading.Event()
        worker = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
        worker.start()

        def cleanup() -> None:
            stop.set()
            worker.join(3)
            service.close()

        self.addCleanup(cleanup)
        valid = self.grants.effective(public_ids.worker('alpha'), self.policy.digest, NOW + timedelta(seconds=11))
        self.assertEqual({"review", "workspace_write"}, {name for name, _, _ in valid.grant_triples})
        managed = RunLedger(
            self.root,
            sequencer_client=SequencerClient(
                service.socket_path, service.epoch, "backdated-managed-capability-binder"
            ),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            CapabilityBinder(managed, self.grants).bind(
                self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
                self.policy, 0, now=NOW + timedelta(seconds=11),
            )
        self.assertEqual("capability_selector_unsatisfied", caught.exception.code)
        self.assertEqual({}, RunLedger(self.root).project().run(self.run_id)["capability_sets"])

    def test_evaluated_record_id_preclaim_cannot_return_an_unrelated_durable_record(self) -> None:
        """Catches deterministic service IDs aliasing unrelated direct-mode bindings."""
        from floati.sequencer import _policy_evidence, _semantic_uuid

        victim_admission_run = "run-" + uuid7_hex()
        admission_operation = "admission_binding_evaluation"
        admission_intent = {
            "run_id": victim_admission_run,
            "plan": self.plan.canonical(),
            "policy": _policy_evidence(self.policy),
        }
        admission_record_id = "run-admission-bound-" + _semantic_uuid(
            admission_operation, admission_intent
        )
        planted_run = "run-" + uuid7_hex()

        def append_to_planted_run(kind, prefix, **fields):
            return self.ledger.append(
                {
                    "schema_version": 0,
                    "id": prefix + uuid7_hex(),
                    "tenant_id": "alpha",
                    "timestamp": "2026-08-08T13:00:00.000Z",
                    "kind": kind,
                    "run_id": planted_run,
                    **fields,
                }
            )

        append_to_planted_run(
            "run_created",
            "run-created-",
            plan_digest=self.plan.digest,
            policy_digest=self.policy.digest,
            item_ids=[self.item_id],
            dependency_edges=[],
        )
        append_to_planted_run(
            "task_contract",
            "task-contract-",
            item_id=self.item_id,
            **self.contract.canonical(),
            contract_digest=contract_digest(self.contract),
        )
        append_to_planted_run(
            "run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest
        )
        append_to_planted_run(
            "worker_pool_bound", "run-worker-pool-bound-", worker_ids=[public_ids.worker('alpha')]
        )
        planted_admission = AdmissionBinder(self.ledger)._bind(
            planted_run,
            self.plan,
            self.policy,
            now=NOW + timedelta(seconds=9),
            record_id=admission_record_id,
        )
        self.assertEqual(planted_run, planted_admission["run_id"])

        victim_capability_run = "run-" + uuid7_hex()
        capability_operation = "capability_binding_evaluation"
        capability_intent = {
            "run_id": victim_capability_run,
            "item_id": self.item_id,
            "attempt_id": self.opened["attempt_id"],
            "chosen_worker": public_ids.worker('alpha'),
            "worker_profile": "codex",
            "policy": _policy_evidence(self.policy),
            "routing_rank": 0,
        }
        capability_record_id = "capability-set-bound-" + _semantic_uuid(
            capability_operation, capability_intent
        )
        planted_capability = self.binder.bind(
            self.run_id,
            self.item_id,
            self.opened["attempt_id"],
            public_ids.worker('alpha'),
            "codex",
            self.policy,
            0,
            now=NOW + timedelta(seconds=11),
            _record_id=capability_record_id,
        )
        self.assertEqual(self.run_id, planted_capability["run_id"])
        before_hostile_requests = RunLedger(self.root).records()

        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01),
            clock=lambda: NOW + timedelta(seconds=12),
        )
        stop = threading.Event()
        worker = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
        worker.start()

        def cleanup() -> None:
            stop.set()
            worker.join(3)
            service.close()

        self.addCleanup(cleanup)
        managed = RunLedger(
            self.root,
            sequencer_client=SequencerClient(
                service.socket_path, service.epoch, "evaluated-id-preclaim"
            ),
        )
        outcomes = {}
        try:
            AdmissionBinder.bind(
                managed,
                victim_admission_run,
                self.plan,
                self.policy,
                now=NOW + timedelta(seconds=12),
            )
        except ProtocolRefusal as exc:
            outcomes["admission"] = ("refused", exc.code)
        else:
            outcomes["admission"] = ("ok", None)
        try:
            CapabilityBinder(managed, self.grants).bind(
                victim_capability_run,
                self.item_id,
                self.opened["attempt_id"],
                public_ids.worker('alpha'),
                "codex",
                self.policy,
                0,
                now=NOW + timedelta(seconds=12),
            )
        except ProtocolRefusal as exc:
            outcomes["capability"] = ("refused", exc.code)
        else:
            outcomes["capability"] = ("ok", None)

        self.assertEqual(
            {"admission": "refused", "capability": "refused"},
            {name: outcome[0] for name, outcome in outcomes.items()},
        )
        self.assertEqual(before_hostile_requests, RunLedger(self.root).records())

    def test_evaluated_retry_rejects_exact_direct_preclaims_but_recovers_service_origin(self) -> None:
        """Catches field correlation being mistaken for durable service provenance."""
        from floati.sequencer import _epoch_semantic_uuid, _policy_evidence

        def seed_admission_run(run_id):
            def append(kind, prefix, **fields):
                return self.ledger.append(
                    {
                        "schema_version": 0,
                        "id": prefix + uuid7_hex(),
                        "tenant_id": "alpha",
                        "timestamp": "2026-08-08T13:00:00.000Z",
                        "kind": kind,
                        "run_id": run_id,
                        **fields,
                    }
                )

            append(
                "run_created",
                "run-created-",
                plan_digest=self.plan.digest,
                policy_digest=self.policy.digest,
                item_ids=[self.item_id],
                dependency_edges=[],
            )
            append(
                "task_contract",
                "task-contract-",
                item_id=self.item_id,
                **self.contract.canonical(),
                contract_digest=contract_digest(self.contract),
            )
            append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
            append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=[public_ids.worker('alpha')])

        def admission_intent(run_id):
            return {
                "run_id": run_id,
                "plan": self.plan.canonical(),
                "policy": _policy_evidence(self.policy),
            }

        exact_admission_run = "run-" + uuid7_hex()
        genuine_admission_run = "run-" + uuid7_hex()
        seed_admission_run(exact_admission_run)
        seed_admission_run(genuine_admission_run)
        def start_service(sequencer_id):
            service = SequencerService(
                self.root,
                sequencer_id,
                config=SequencerConfig(select_timeout=0.01),
                clock=lambda: NOW + timedelta(seconds=200),
            )
            stop = threading.Event()
            worker = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
            worker.start()
            return service, stop, worker

        service, stop, worker = start_service("sequencer-a")
        self.addCleanup(stop.set)
        self.addCleanup(worker.join, 3)
        self.addCleanup(service.close)
        client = SequencerClient(service.socket_path, service.epoch, "exact-preclaim")
        genuine = client.bind_admission(
            genuine_admission_run, self.plan, self.policy, "ignored"
        )
        epoch_record_id = service._lease.record["id"]
        stop.set()
        worker.join(3)
        service.close()

        admission_operation = "admission_binding_evaluation"
        exact_admission_intent = admission_intent(exact_admission_run)
        capability_operation = "capability_binding_evaluation"
        exact_capability_intent = {
            "run_id": self.run_id,
            "item_id": self.item_id,
            "attempt_id": self.opened["attempt_id"],
            "chosen_worker": public_ids.worker('alpha'),
            "worker_profile": "codex",
            "policy": _policy_evidence(self.policy),
            "routing_rank": 0,
        }
        before_preclaims = RunLedger(self.root).records()
        outcomes = {}
        try:
            AdmissionBinder(self.ledger)._bind(
                exact_admission_run,
                self.plan,
                self.policy,
                now=NOW + timedelta(seconds=11),
                record_id="run-admission-bound-"
                + _epoch_semantic_uuid(
                    epoch_record_id, admission_operation, exact_admission_intent
                ),
            )
        except ProtocolRefusal as exc:
            outcomes["admission"] = ("refused", exc.code)
        else:
            outcomes["admission"] = ("ok", None)
        try:
            self.binder.bind(
                self.run_id,
                self.item_id,
                self.opened["attempt_id"],
                public_ids.worker('alpha'),
                "codex",
                self.policy,
                0,
                now=NOW + timedelta(seconds=11),
                _record_id="capability-set-bound-"
                + _epoch_semantic_uuid(
                    epoch_record_id, capability_operation, exact_capability_intent
                ),
            )
        except ProtocolRefusal as exc:
            outcomes["capability"] = ("refused", exc.code)
        else:
            outcomes["capability"] = ("ok", None)

        self.assertEqual(
            {"admission": "evaluated_service_only", "capability": "evaluated_service_only"},
            {name: outcome[1] for name, outcome in outcomes.items()},
        )
        self.assertEqual(before_preclaims, RunLedger(self.root).records())
        restarted, restarted_stop, restarted_worker = start_service("sequencer-b")
        self.addCleanup(restarted_stop.set)
        self.addCleanup(restarted_worker.join, 3)
        self.addCleanup(restarted.close)
        retry = SequencerClient(
            restarted.socket_path, restarted.epoch, "genuine-retry"
        ).bind_admission(genuine_admission_run, self.plan, self.policy, "ignored")

        self.assertEqual(genuine["record"], retry["record"])
        self.assertEqual(
            {"admission": "refused", "capability": "refused"},
            {name: outcome[0] for name, outcome in outcomes.items()},
        )
        self.assertFalse(
            self.root.resolve_relative("sequencer/evaluated-provenance.key").exists()
        )
        self.assertEqual(
            [],
            [
                path.name
                for path in self.root.resolve_relative("sequencer").iterdir()
                if path.is_file()
                and path.name not in {"epochs.jsonl", "epochs.jsonl.lock", "owner.lock"}
            ],
        )

    def test_live_service_retained_context_cannot_preclaim_evaluated_ids(self) -> None:
        """Retained service internals cannot authorize binders outside evaluation."""
        from floati.sequencer import (
            _ServiceOwnerSink,
            _epoch_semantic_uuid,
            _policy_evidence,
        )

        def seed_admission_run(run_id):
            def append(kind, prefix, **fields):
                return self.ledger.append(
                    {
                        "schema_version": 0,
                        "id": prefix + uuid7_hex(),
                        "tenant_id": "alpha",
                        "timestamp": "2026-08-08T13:00:00.000Z",
                        "kind": kind,
                        "run_id": run_id,
                        **fields,
                    }
                )

            append(
                "run_created",
                "run-created-",
                plan_digest=self.plan.digest,
                policy_digest=self.policy.digest,
                item_ids=[self.item_id],
                dependency_edges=[],
            )
            append(
                "task_contract",
                "task-contract-",
                item_id=self.item_id,
                **self.contract.canonical(),
                contract_digest=contract_digest(self.contract),
            )
            append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
            append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=[public_ids.worker('alpha')])

        def start_service(sequencer_id):
            service = SequencerService(
                self.root,
                sequencer_id,
                config=SequencerConfig(select_timeout=0.01),
                clock=lambda: NOW + timedelta(seconds=11),
            )
            stop = threading.Event()
            worker = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
            worker.start()
            return service, stop, worker

        forged_admission_run = "run-" + uuid7_hex()
        genuine_admission_run = "run-" + uuid7_hex()
        forged_capability_run = "run-" + uuid7_hex()
        seed_admission_run(forged_admission_run)
        seed_admission_run(genuine_admission_run)
        seed_admission_run(forged_capability_run)
        forged_capability_ledger = RunLedger(self.root)
        AdmissionBinder.bind(
            forged_capability_ledger,
            forged_capability_run,
            self.plan,
            self.policy,
            now=NOW + timedelta(seconds=9),
        )
        forged_opened = RunScheduler(forged_capability_ledger).open_attempt(
            forged_capability_run,
            self.item_id,
            RetryPolicy(1, 0, 0, strategy="fixed"),
            1,
            now=NOW + timedelta(seconds=10),
        )
        admission_intent = {
            "run_id": forged_admission_run,
            "plan": self.plan.canonical(),
            "policy": _policy_evidence(self.policy),
        }
        capability_intent = {
            "run_id": forged_capability_run,
            "item_id": self.item_id,
            "attempt_id": forged_opened["attempt_id"],
            "chosen_worker": public_ids.worker('alpha'),
            "worker_profile": "codex",
            "policy": _policy_evidence(self.policy),
            "routing_rank": 0,
        }
        service, stop, worker = start_service("forged-sink-a")
        try:
            client = SequencerClient(service.socket_path, service.epoch, "forged-sink")
            genuine = client.bind_admission(
                genuine_admission_run, self.plan, self.policy, "ignored"
            )
            genuine_capability = client.bind_capability(
                self.run_id,
                self.item_id,
                self.opened["attempt_id"],
                public_ids.worker('alpha'),
                "codex",
                self.policy,
                0,
                "ignored",
            )
            epoch_record_id = service._lease.record["id"]
            before_preclaims = RunLedger(self.root).records()
            outcomes = {}
            with self.assertRaises(ProtocolRefusal) as fresh_sink:
                _ServiceOwnerSink(service)
            self.assertEqual("evaluated_service_only", fresh_sink.exception.code)
            retained_ledger = getattr(
                service, "_SequencerService__evaluation_ledger", None
            )
            retained_sink = getattr(
                service, "_SequencerService__evaluation_sink", None
            )
            admission_ledger = retained_ledger or RunLedger(self.root)
            admission_capability = retained_sink or object()
            try:
                AdmissionBinder(admission_ledger)._bind(
                    forged_admission_run,
                    self.plan,
                    self.policy,
                    now=NOW + timedelta(seconds=11),
                    record_id="run-admission-bound-"
                    + _epoch_semantic_uuid(
                        epoch_record_id,
                        "admission_binding_evaluation",
                        admission_intent,
                    ),
                    _service_capability=admission_capability,
                )
            except ProtocolRefusal as exc:
                outcomes["admission"] = exc.code
            else:
                outcomes["admission"] = "ok"
            capability_ledger = retained_ledger or RunLedger(self.root)
            capability_capability = retained_sink or object()
            try:
                CapabilityBinder(
                    capability_ledger, CapabilityGrantLedger(self.root)
                ).bind(
                    forged_capability_run,
                    self.item_id,
                    forged_opened["attempt_id"],
                    public_ids.worker('alpha'),
                    "codex",
                    self.policy,
                    0,
                    now=NOW + timedelta(seconds=11),
                    _record_id="capability-set-bound-"
                    + _epoch_semantic_uuid(
                        epoch_record_id,
                        "capability_binding_evaluation",
                        capability_intent,
                    ),
                    _service_capability=capability_capability,
                )
            except ProtocolRefusal as exc:
                outcomes["capability"] = exc.code
            else:
                outcomes["capability"] = "ok"
        finally:
            stop.set()
            worker.join(3)
            service.close()

        restarted, restarted_stop, restarted_worker = start_service("forged-sink-b")
        try:
            retry = SequencerClient(
                restarted.socket_path, restarted.epoch, "genuine-retry"
            ).bind_admission(genuine_admission_run, self.plan, self.policy, "ignored")
        finally:
            restarted_stop.set()
            restarted_worker.join(3)
            restarted.close()

        self.assertEqual(genuine["record"], retry["record"])
        self.assertEqual("capability_set_bound", genuine_capability["record"]["kind"])
        self.assertEqual(
            {
                "admission": "evaluated_service_only",
                "capability": "evaluated_service_only",
            },
            outcomes,
        )
        self.assertIsNone(retained_ledger)
        self.assertIsNone(retained_sink)
        self.assertEqual(before_preclaims, RunLedger(self.root).records())

    def test_complete_selector_is_required_and_refusal_appends_no_snapshot(self) -> None:
        """Catches a partial selector or empty-set fallback becoming dispatch authority."""
        workspace_grant = next(
            row for row in self.grants.records()
            if row["kind"] == "capability_grant" and row["capability_name"] == "workspace_write"
        )
        self.grants.revoke(
            workspace_grant["id"], "operator_revoked", now=NOW + timedelta(seconds=10)
        )
        before = list(self.ledger.records())
        with self.assertRaises(ProtocolRefusal) as caught:
            self.binder.bind(
                self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
                self.policy, 0, now=NOW + timedelta(seconds=11),
            )
        self.assertEqual("capability_selector_unsatisfied", caught.exception.code)
        self.assertEqual(before, self.ledger.records())

    def test_forged_policy_selector_cache_cannot_narrow_required_grants(self) -> None:
        """Catches stale policy bytes blessing a caller-narrowed live selector."""
        workspace_grant = next(
            row for row in self.grants.records()
            if row["kind"] == "capability_grant" and row["capability_name"] == "workspace_write"
        )
        self.grants.revoke(
            workspace_grant["id"], "operator_revoked", now=NOW + timedelta(seconds=10)
        )
        forged = dataclasses.replace(
            self.policy,
            capability_selectors={
                "review_write": CapabilitySelector("review_write", ("review",))
            },
        )
        before = self.ledger.records()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.binder.bind(
                self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
                forged, 0, now=NOW + timedelta(seconds=11),
            )
        self.assertEqual("policy_integrity_invalid", caught.exception.code)
        self.assertEqual(before, self.ledger.records())

    def test_snapshot_is_single_use_and_attempt_start_does_not_recheck_expiry(self) -> None:
        """Catches snapshot reuse or a second clock-dependent authorization at attempt start."""
        snapshot = self.binder.bind(
            self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
            self.policy, 0, now=NOW + timedelta(seconds=11),
        )
        dispatch = self.binder.dispatch(snapshot["id"], [public_ids.worker('alpha')], "policy.route", self.policy, now=NOW + timedelta(seconds=12))
        with self.assertRaises(ProtocolRefusal) as reuse:
            self.binder.dispatch(snapshot["id"], [public_ids.worker('alpha')], "policy.route", self.policy, now=NOW + timedelta(seconds=13))
        self.assertEqual("capability_snapshot_consumed", reuse.exception.code)
        started = self.scheduler.start_attempt(
            self.run_id, self.item_id, self.opened["attempt_id"], dispatch["id"],
            now=NOW + timedelta(seconds=200),
        )
        self.assertEqual(dispatch["id"], started["dispatch_decision_id"])

    def test_dispatch_rejects_forged_current_policy_without_consuming_snapshot(self) -> None:
        """Catches dispatch trusting stale policy cache fields or consuming authority on refusal."""
        snapshot = self.binder.bind(
            self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
            self.policy, 0, now=NOW + timedelta(seconds=11),
        )
        forged = dataclasses.replace(self.policy, limits={**self.policy.limits, "max_active_attempts": 1})
        before = self.ledger.records()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.binder.dispatch(
                snapshot["id"], [public_ids.worker('alpha')], "policy.route", forged,
                now=NOW + timedelta(seconds=12),
            )
        self.assertEqual("policy_integrity_invalid", caught.exception.code)
        self.assertEqual(before, self.ledger.records())

    def test_admission_bound_run_refuses_v0_dispatch_without_mutation(self) -> None:
        """Catches a schema-v0 dispatch bypassing limits and snapshot enforcement."""
        before = self.ledger.records()
        with self.assertRaises(ProtocolRefusal) as caught:
            self._append(
                "dispatch_decision", "run-dispatch-decision-", item_id=self.item_id,
                attempt_id=self.opened["attempt_id"], eligible_workers=[public_ids.worker('alpha')],
                chosen_worker=public_ids.worker('alpha'), capability_digest="c" * 64,
                reason_code="policy.route", policy_digest=self.policy.digest,
                routing_rank=0, scheduler_epoch=1,
            )
        self.assertEqual("dispatch_version_required", caught.exception.code)
        self.assertEqual(before, self.ledger.records())

    def test_true_legacy_v0_dispatch_replays_as_explicitly_unenforced(self) -> None:
        """Catches legacy evidence being silently upgraded or rejected by the new boundary."""
        run_id, item_id, opened, append = self._legacy_open_attempt()
        legacy = append(
            "dispatch_decision", "run-dispatch-decision-", item_id=item_id,
            attempt_id=opened["attempt_id"], eligible_workers=[public_ids.worker('alpha')],
            chosen_worker=public_ids.worker('alpha'), capability_digest="c" * 64,
            reason_code="policy.route", policy_digest=self.policy.digest,
            routing_rank=0, scheduler_epoch=1,
        )
        projected = self.ledger.project().run(run_id)["dispatches"][opened["attempt_id"]]
        self.assertEqual(legacy["id"], projected["id"])
        self.assertEqual("legacy_unenforced", projected["capability_enforcement"])

    def test_snapshot_cannot_race_behind_an_existing_dispatch(self) -> None:
        """Catches a stale binder precheck appending authorization after dispatch physical truth."""
        run_id, item_id, opened, append = self._legacy_open_attempt()
        append(
            "dispatch_decision", "run-dispatch-decision-", item_id=item_id,
            attempt_id=opened["attempt_id"], eligible_workers=[public_ids.worker('alpha')],
            chosen_worker=public_ids.worker('alpha'), capability_digest="c" * 64,
            reason_code="policy.route", policy_digest=self.policy.digest,
            routing_rank=0, scheduler_epoch=1,
        )
        before = self.ledger.records()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.binder.bind(
                run_id, item_id, opened["attempt_id"], public_ids.worker('alpha'), "codex",
                self.policy, 0, now=NOW + timedelta(seconds=11),
            )
        self.assertEqual("capability_snapshot_late", caught.exception.code)
        self.assertEqual(before, self.ledger.records())

    def test_hostile_and_reordered_snapshot_dispatch_frames_fail_closed(self) -> None:
        """Covers semantic corruption and physical reordering for both new run kinds."""
        snapshot = self.binder.bind(
            self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
            self.policy, 0, now=NOW + timedelta(seconds=11),
        )
        dispatch = self.binder.dispatch(
            snapshot["id"], [public_ids.worker('alpha')], "policy.route", self.policy, now=NOW + timedelta(seconds=12)
        )
        records = self.ledger.records()
        path = self.root.resolve_relative(self.ledger.relative_path)
        snapshot_index = next(
            index for index, row in enumerate(records) if row["id"] == snapshot["id"]
        )
        dispatch_index = next(
            index for index, row in enumerate(records) if row["id"] == dispatch["id"]
        )

        hostile_snapshot = list(records)
        hostile_snapshot[snapshot_index] = dict(snapshot, chosen_worker="mallory")
        path.write_bytes(b"".join(encode_frame(row) for row in hostile_snapshot))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.project()
        self.assertEqual("capability_snapshot_binding_invalid", caught.exception.code)

        hostile_dispatch = list(records)
        hostile_dispatch[dispatch_index] = dict(dispatch, capability_digest="e" * 64)
        path.write_bytes(b"".join(encode_frame(row) for row in hostile_dispatch))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.project()
        self.assertEqual("capability_dispatch_mismatch", caught.exception.code)

        reordered = list(records)
        reordered[snapshot_index], reordered[dispatch_index] = (
            reordered[dispatch_index], reordered[snapshot_index]
        )
        path.write_bytes(b"".join(encode_frame(row) for row in reordered))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.project()
        self.assertEqual("capability_snapshot_missing", caught.exception.code)

    def test_twelve_process_snapshot_revoke_race_has_one_physical_order(self) -> None:
        """Catches a revoke interleaving inside an unlocked or timestamp-ordered snapshot."""
        workspace_grant = next(
            row for row in self.grants.records()
            if row["kind"] == "capability_grant" and row["capability_name"] == "workspace_write"
        )
        context = multiprocessing.get_context("fork")
        start = context.Event()
        queue = context.Queue()
        actions = ["bind"] * 6 + ["revoke"] * 6
        processes = [
            context.Process(
                target=_race_binding,
                args=(
                    str(self.root.path), str(self.policy_path), self.run_id,
                    self.item_id, self.opened["attempt_id"], workspace_grant["id"],
                    action, start, queue,
                ),
            )
            for action in actions
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(8)
            self.assertEqual(0, process.exitcode)
        results = [queue.get(timeout=1) for _ in processes]
        self.assertNotIn("error", {status for status, _ in results})
        self.assertEqual(1, sum(value == "capability_revoked" for _, value in results))
        self.assertLessEqual(sum(value == "capability_set_bound" for _, value in results), 1)
        run = self.ledger.project().run(self.run_id)
        snapshots = list(run["capability_sets"].values())
        if snapshots:
            self.assertEqual(2, snapshots[0]["grant_ledger_high_watermark"])
            self.assertIn(
                workspace_grant["id"],
                {row["grant_id"] for row in snapshots[0]["effective_grants"]},
            )
        revocations = [row for row in self.grants.records() if row["kind"] == "capability_revoked"]
        self.assertEqual(3, self.grants.records().index(revocations[0]) + 1)

    def test_twelve_processes_compete_for_one_remaining_run_slot(self) -> None:
        """Catches stale prechecks letting multiple v1 dispatches consume the final run slot."""
        run_id = "run-" + uuid7_hex()
        item_ids = sorted("work-" + uuid7_hex() for _ in range(13))
        contract = TaskContract.create(
            objective="race one remaining dispatch slot",
            non_goals=["no grant acquisition in run transaction"],
            areas_to_avoid=[{"path": "schemas/v0", "region": "all"}],
            input_hashes={"brief": "e" * 64},
            acceptance_checks={"tests.race": "python3 -m unittest"},
            constraints={"network": "dark"},
            risk_class="low",
            retry_policy={"max_attempts": 1, "backoff": {
                "base_delay_ms": 0, "cap_delay_ms": 0, "strategy": "fixed",
            }},
            dependencies=[],
        )
        plan_value = {
            "schema_version": 0,
            "workers": [{"node_id": public_ids.worker('alpha'), "worker_profile": "codex"}],
            "max_active_attempts": 2,
            "budget_reservations": [{"budget_id": "build", "amount": 1}],
            "items": [
                {
                    "item_id": item_id,
                    "contract": contract.canonical(),
                    "capability_selector": "review_write",
                    "requires_cancellation": True,
                    "requires_callback": True,
                    "workspace_key": f"workspace-{index:02d}",
                    "concurrency_key": f"concurrency-{index:02d}",
                    "retry_class": "transient",
                    "effect_safety": "idempotent",
                    "merge_gate": None,
                }
                for index, item_id in enumerate(item_ids)
            ],
            "dependency_edges": [],
        }
        plan_path = self.policy_path.parent / "race-admission-plan.json"
        plan_path.write_text(json.dumps(plan_value, separators=(",", ":")), encoding="utf-8")
        plan = AdmissionPlan.load(plan_path)

        def append(kind, prefix, **fields):
            return self.ledger.append({
                "schema_version": 0,
                "id": prefix + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-08T13:00:00.000Z",
                "kind": kind,
                "run_id": run_id,
                **fields,
            })

        append(
            "run_created", "run-created-", plan_digest=plan.digest,
            policy_digest=self.policy.digest, item_ids=item_ids, dependency_edges=[],
        )
        for item_id in item_ids:
            append(
                "task_contract", "task-contract-", item_id=item_id,
                **contract.canonical(), contract_digest=contract_digest(contract),
            )
        append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
        append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=[public_ids.worker('alpha')])
        AdmissionBinder.bind(
            self.ledger, run_id, plan, self.policy, now=NOW + timedelta(seconds=9)
        )
        scheduler = RunScheduler(self.ledger)
        race_binder = CapabilityBinder(self.ledger, self.grants)
        snapshots = []
        for index, item_id in enumerate(item_ids):
            opened = scheduler.open_attempt(
                run_id, item_id, RetryPolicy(1, 0, 0, strategy="fixed"), 1,
                now=NOW + timedelta(seconds=10),
            )
            snapshots.append(
                race_binder.bind(
                    run_id, item_id, opened["attempt_id"], public_ids.worker('alpha'), "codex",
                    self.policy, 0, now=NOW + timedelta(seconds=11, milliseconds=index),
                )
            )
        race_binder.dispatch(
            snapshots[0]["id"], [public_ids.worker('alpha')], "policy.route", self.policy,
            now=NOW + timedelta(seconds=12),
        )

        context = multiprocessing.get_context("fork")
        start = context.Event()
        queue = context.Queue()
        candidates = snapshots[1:]
        processes = [
            context.Process(
                target=_race_dispatch,
                args=(str(self.root.path), str(self.policy_path), snapshot["id"], start, queue),
            )
            for snapshot in candidates
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(8)
            self.assertEqual(0, process.exitcode)
        results = [queue.get(timeout=1) for _ in processes]
        self.assertNotIn("error", {status for status, _ in results})
        self.assertEqual(1, sum(status == "ok" for status, _ in results))
        self.assertEqual(
            ["run_concurrency_exhausted"] * 11,
            sorted(value for status, value in results if status == "refused"),
        )
        run = self.ledger.project().run(run_id)
        candidate_ids = {snapshot["id"] for snapshot in candidates}
        self.assertEqual(1, len(candidate_ids & set(run["capability_set_consumers"])))
        self.assertEqual(2, len(run["dispatches"]))

    def test_bounded_grant_lock_timeout_refuses_without_run_append(self) -> None:
        """Catches the binder proceeding unlocked after bounded lock acquisition fails."""
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        holder = context.Process(target=_hold_grant_lock, args=(str(self.root.path), ready))
        holder.start()
        self.assertTrue(ready.wait(2))
        before = self.ledger.records()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.binder.bind(
                self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
                self.policy, 0, now=NOW + timedelta(seconds=11),
            )
        self.assertEqual("ledger_lock_timeout", caught.exception.code)
        self.assertEqual(before, self.ledger.records())
        holder.join(4)
        self.assertEqual(0, holder.exitcode)

    def test_snapshot_and_v1_dispatch_short_writes_roll_back_exactly(self) -> None:
        """Catches either new run-ledger kind leaving a partial durable frame."""
        path = self.root.resolve_relative("runs/events.jsonl")
        before = path.read_bytes()
        with patch("floati.jsonl.os.write", return_value=1):
            with self.assertRaises(DurabilityFailure) as snapshot_failure:
                self.binder.bind(
                    self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
                    self.policy, 0, now=NOW + timedelta(seconds=11),
                )
        self.assertEqual("short_write", snapshot_failure.exception.code)
        self.assertEqual(before, path.read_bytes())

        snapshot = self.binder.bind(
            self.run_id, self.item_id, self.opened["attempt_id"], public_ids.worker('alpha'), "codex",
            self.policy, 0, now=NOW + timedelta(seconds=11),
        )
        before = path.read_bytes()
        with patch("floati.jsonl.os.write", return_value=1):
            with self.assertRaises(DurabilityFailure) as dispatch_failure:
                self.binder.dispatch(
                    snapshot["id"], [public_ids.worker('alpha')], "policy.route", self.policy,
                    now=NOW + timedelta(seconds=12),
                )
        self.assertEqual("short_write", dispatch_failure.exception.code)
        self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
