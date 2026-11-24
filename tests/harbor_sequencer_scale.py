from __future__ import annotations

import os
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.contracts import TaskContract, contract_digest
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.root import FloatiRoot
from floati.run_segments import RunStoreSnapshot
from floati.runtruth import RunLedger, RunProjection

try:
    from floati.sequencer_scale import ScaleConfig, run_scale_fixture
except (ImportError, ModuleNotFoundError):
    ScaleConfig = run_scale_fixture = None


def _queueable_unix_connects(backlog: int, ceiling: int) -> int:
    """Measure how many AF_UNIX connects this host parks before it refuses.

    CI-GREEN-24. The kernel, not the product, decides this. `listen(n)` is a
    REQUEST: measured on macOS, `listen(301)` parks exactly `SOMAXCONN` = 128
    connects and refuses the 129th with `ConnectionRefusedError` errno 61 -
    the identical error a macOS runner raised out of the scale fixture. So the
    fixture's pre-queue depth is a host fact, and reading `socket.SOMAXCONN`
    alone would not do: it is the cap that is silent, and only a real listener
    reports what the cap actually is here. Bounded by `ceiling` so the probe
    never outruns this process's descriptor limit on a host with a deep queue.
    """

    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "backlog-probe.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        held: list[socket.socket] = []
        try:
            listener.bind(path)
            listener.listen(backlog)
            for queued in range(ceiling):
                channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                channel.settimeout(5)
                try:
                    channel.connect(path)
                except OSError:
                    channel.close()
                    return queued
                held.append(channel)
            return ceiling
        finally:
            for channel in held:
                channel.close()
            listener.close()


class SequencerScaleTests(unittest.TestCase):
    @staticmethod
    def _run_created(tenant_id: str, ordinal: int) -> dict[str, object]:
        record_hex = list(f"{ordinal:032x}")
        record_hex[12] = "7"
        record_hex[16] = "8"
        item_hex = list(f"{ordinal + 1000:032x}")
        item_hex[12] = "7"
        item_hex[16] = "8"
        identity = "".join(record_hex)
        item_identity = "".join(item_hex)
        return {
            "schema_version": 0,
            "id": "run-created-" + identity,
            "tenant_id": tenant_id,
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "run_created",
            "run_id": "run-" + identity,
            "plan_digest": "a" * 64,
            "item_ids": ["work-" + item_identity],
            "dependency_edges": [],
        }

    @staticmethod
    def _task_contract(created: dict[str, object]) -> dict[str, object]:
        contract = TaskContract.create(
            objective="bind the exact durable item",
            non_goals=["no stale projection"],
            areas_to_avoid=[{"path": "bundle/c7.1", "region": "all"}],
            input_hashes={"brief": "a" * 64},
            acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"},
            risk_class="low",
            retry_policy={
                "max_attempts": 1,
                "backoff": {
                    "base_delay_ms": 0,
                    "cap_delay_ms": 0,
                    "strategy": "fixed",
                },
            },
            dependencies=[],
        )
        suffix = str(created["id"]).removeprefix("run-created-")
        return {
            "schema_version": 0,
            "id": "task-contract-" + suffix,
            "tenant_id": created["tenant_id"],
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "task_contract",
            "run_id": created["run_id"],
            "item_id": created["item_ids"][0],
            **contract.canonical(),
            "contract_digest": contract_digest(contract),
        }

    def test_active_cache_owns_appended_and_returned_records(self) -> None:
        """Catches caller mutation turning an unequal retry into cached success."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            ledger = RunLedger(root)
            ledger._store.activate(
                now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
            )
            candidate = self._run_created("alpha", 10)
            returned = ledger.append(candidate)
            segment = root.resolve_relative("runs/segments/00000000.jsonl")
            durable_bytes = segment.read_bytes()

            returned["plan_digest"] = "b" * 64
            with self.assertRaises(ProtocolRefusal) as same_ledger:
                ledger.append(dict(returned))
            self.assertEqual("duplicate_record_id", same_ledger.exception.code)
            self.assertEqual(durable_bytes, segment.read_bytes())

            with self.assertRaises(ProtocolRefusal) as fresh_ledger:
                RunLedger(root).append(dict(returned))
            self.assertEqual("duplicate_record_id", fresh_ledger.exception.code)
            self.assertEqual(durable_bytes, segment.read_bytes())

    def test_public_transaction_snapshot_cannot_poison_active_cache(self) -> None:
        """Catches callback mutation of snapshot internals changing durable identity."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            ledger = RunLedger(root)
            ledger._store.activate(
                now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
            )
            created = self._run_created("alpha", 11)
            self.assertEqual([created], ledger._append_governed_batch([created]))
            cached = ledger._managed_projection_cache
            self.assertIsNotNone(cached)
            cached_digest = cached[3].semantic_digest()
            segment = root.resolve_relative("runs/segments/00000000.jsonl")
            durable_bytes = segment.read_bytes()
            unequal = dict(created, plan_digest="b" * 64)

            def mutate_snapshot(snapshot):
                snapshot._located[0].record["plan_digest"] = "b" * 64
                return None, None

            ledger._store.transact(mutate_snapshot)

            with self.assertRaises(ProtocolRefusal) as same_ledger:
                ledger.append(unequal)
            self.assertEqual("duplicate_record_id", same_ledger.exception.code)
            self.assertEqual(durable_bytes, segment.read_bytes())
            self.assertEqual([created], ledger.records())
            self.assertEqual(
                cached_digest,
                ledger._managed_projection_cache[3].semantic_digest(),
            )

            with self.assertRaises(ProtocolRefusal) as fresh_ledger:
                RunLedger(root).append(unequal)
            self.assertEqual("duplicate_record_id", fresh_ledger.exception.code)
            self.assertEqual(durable_bytes, segment.read_bytes())

    def test_managed_projection_cache_binds_exact_validated_prefix(self) -> None:
        """Catches same-count valid segment replacement reusing a stale projection."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            ledger = RunLedger(root)
            ledger._store.activate(
                now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
            )
            created = self._run_created("alpha", 20)
            self.assertEqual([created], ledger._append_governed_batch([created]))

            replacement = self._run_created("alpha", 21)
            old_item = str(created["item_ids"][0])
            new_item = str(replacement["item_ids"][0])
            self.assertEqual(len(old_item), len(new_item))
            segment = root.resolve_relative("runs/segments/00000000.jsonl")
            replaced_bytes = segment.read_bytes().replace(
                old_item.encode("utf-8"), new_item.encode("utf-8")
            )
            self.assertNotEqual(segment.read_bytes(), replaced_bytes)
            segment.write_bytes(replaced_bytes)

            task = self._task_contract(created)
            before = segment.read_bytes()
            outcomes = ledger._append_governed_batch([task])
            self.assertIsInstance(outcomes[0], (ProtocolRefusal, IntegrityFailure))
            self.assertEqual(before, segment.read_bytes())
            self.assertEqual(1, RunLedger(root).project()._last_position)

    def test_projection_cache_identity_is_captured_with_the_append(self) -> None:
        """Catches post-append identity sampling binding a stale projection."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            ledger = RunLedger(root)
            ledger._store.activate(
                now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
            )
            created = self._run_created("alpha", 22)
            replacement = self._run_created("alpha", 23)
            old_item = str(created["item_ids"][0])
            new_item = str(replacement["item_ids"][0])
            segment = root.resolve_relative("runs/segments/00000000.jsonl")
            sample_transaction = ledger._store.transact

            def replace_before_identity_sample(decide):
                segment.write_bytes(
                    segment.read_bytes().replace(
                        old_item.encode("utf-8"), new_item.encode("utf-8")
                    )
                )
                return sample_transaction(decide)

            with mock.patch.object(
                ledger._store,
                "transact",
                side_effect=replace_before_identity_sample,
            ) as sampled_after_unlock:
                self.assertEqual([created], ledger._append_governed_batch([created]))
            self.assertEqual(0, sampled_after_unlock.call_count)
            if old_item.encode("utf-8") in segment.read_bytes():
                segment.write_bytes(
                    segment.read_bytes().replace(
                        old_item.encode("utf-8"), new_item.encode("utf-8")
                    )
                )

            task = self._task_contract(created)
            before = segment.read_bytes()
            outcomes = ledger._append_governed_batch([task])
            self.assertIsInstance(outcomes[0], (ProtocolRefusal, IntegrityFailure))
            self.assertEqual(before, segment.read_bytes())

    def test_exact_retry_after_intervening_commits_uses_validated_id_index(self) -> None:
        """Catches exact retry reverse-scanning the full validated snapshot."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            ledger = RunLedger(root)
            ledger._store.activate(
                now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
            )
            retried = ledger.append(self._run_created("alpha", 30))
            for ordinal in range(31, 39):
                ledger.append(self._run_created("alpha", ordinal))

            restarted = RunLedger(root)
            restarted.records()
            state = restarted._store._cached_state
            self.assertIsNotNone(state)
            snapshot = RunStoreSnapshot(state.located)
            snapshot._known = state.known

            class NoReverseTuple(tuple):
                def __reversed__(self):
                    raise AssertionError("validated ID lookup must not reverse-scan")

            snapshot._located = NoReverseTuple(snapshot._located)
            located = snapshot.lookup(str(retried["id"]))
            self.assertIsNotNone(located)
            self.assertEqual(retried, located.record)

    def test_restarted_exact_retry_uses_validated_id_index_without_projection_replay(self) -> None:
        """Catches response-loss retry latency growing with the durable run prefix."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            ledger = RunLedger(root)
            ledger._store.activate(now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc))
            first = self._run_created("alpha", 1)
            retried = self._run_created("alpha", 2)
            ledger.append(first)
            ledger.append(retried)

            restarted = RunLedger(root)
            with mock.patch.object(
                RunProjection,
                "empty",
                side_effect=AssertionError("exact retry must not rebuild run projection"),
            ):
                self.assertEqual(retried, restarted.append(retried))

    def test_full_client_fairness_phase_stays_within_kernel_backlog(self) -> None:
        """Catches pre-queuing more real sockets than the host can accept."""
        self.assertIsNotNone(ScaleConfig, "floati.sequencer_scale must expose ScaleConfig")
        self.assertIsNotNone(
            run_scale_fixture, "floati.sequencer_scale must expose run_scale_fixture"
        )
        # CI-GREEN-24. The fixture opens `client_count` real connections BEFORE
        # the service accepts anything, then dials `client_count + 1` more while
        # it drains - so in the worst case, where the accept loop is starved,
        # `2 * client_count + 1` connects are outstanding at once. Against a
        # kernel queue this host caps at SOMAXCONN that is a bet on the service
        # thread winning a scheduling race, and on a macOS runner it lost:
        # `ConnectionRefusedError: [Errno 61]` out of `_queue_socket_request`.
        # A refused connect on a full backlog is the KERNEL BEHAVING CORRECTLY,
        # so there is nothing here to fix in the product and nothing to skip.
        # Instead the host's real depth is measured and the run is sized to fit
        # it, which makes the phase deterministic rather than lucky: even if the
        # service accepts nothing at all, no connect is refused. The pinned
        # numbers are DERIVED from the size that survives, never hardcoded, so
        # they still fail if the fixture's own arithmetic changes.
        requested_clients = 100
        product_backlog = min(1024, requested_clients * 3 + 1)
        worst_case_outstanding = 2 * requested_clients + 1
        depth = _queueable_unix_connects(product_backlog, worst_case_outstanding)
        client_count = min(requested_clients, max(1, (depth - 1) // 2))
        print(
            f"[CI-GREEN-24] {self.id()}: host parks {depth} AF_UNIX connects on "
            f"listen({product_backlog}) (SOMAXCONN {socket.SOMAXCONN}); the phase "
            f"needs {2 * client_count + 1} outstanding, so client_count="
            f"{client_count} of {requested_clients}; host loadavg "
            f"{[round(value, 2) for value in os.getloadavg()]}"
        )
        self.assertLessEqual(
            2 * client_count + 1,
            depth,
            "the sized run must fit the measured backlog with the accept loop stopped",
        )

        artifact = run_scale_fixture(
            ScaleConfig(
                max_records=50,
                batch_size=10,
                client_count=client_count,
                item_count=1,
                lifecycle_record_count=100,
                restart_batch_ordinals=(3, 6, 9),
            )
        )

        self.assertEqual(
            2 * client_count + 1, artifact["fairness"]["real_socket_turns"]
        )
        self.assertLessEqual(
            artifact["fairness"]["max_service_turns"], client_count + 1
        )

    def test_injected_fast_scale_survives_restarts_and_replays_exactly(self) -> None:
        """Catches a reduced, lossy, duplicate, unfair, or daemon-only scale fixture."""
        self.assertIsNotNone(ScaleConfig, "floati.sequencer_scale must expose ScaleConfig")
        self.assertIsNotNone(
            run_scale_fixture, "floati.sequencer_scale must expose run_scale_fixture"
        )
        artifact = run_scale_fixture(
            ScaleConfig(
                max_records=25,
                batch_size=8,
                client_count=10,
                item_count=100,
                lifecycle_record_count=10_000,
                restart_batch_ordinals=(250, 625, 1000),
            )
        )

        self.assertEqual("passed", artifact["status"])
        self.assertEqual(10_000, artifact["counts"]["requested_lifecycle_records"])
        self.assertGreaterEqual(artifact["counts"]["valid_run_records"], 10_000)
        self.assertEqual(100, artifact["counts"]["admitted_items"])
        self.assertEqual(10, artifact["counts"]["client_principals"])
        self.assertEqual([250, 625, 1000], artifact["restart_points"]["batch_ordinals"])
        self.assertEqual(0, artifact["duplicates"]["record_ids"])
        self.assertEqual(0, artifact["acknowledgements"]["lost"])
        self.assertEqual(
            artifact["acknowledgements"]["unknown_responses"],
            artifact["acknowledgements"]["retry_resolutions"],
        )
        self.assertEqual(0, artifact["timeouts"]["ledger_lock"])
        self.assertEqual("real_socket_service", artifact["fairness"]["source"])
        self.assertGreaterEqual(artifact["fairness"]["real_socket_turns"], 10)
        self.assertLessEqual(artifact["fairness"]["max_service_turns"], 11)
        self.assertEqual(1, artifact["acknowledgements"]["real_response_losses"])
        self.assertEqual(
            artifact["acknowledgements"]["real_response_losses"],
            artifact["acknowledgements"]["real_response_loss_resolutions"],
        )
        self.assertGreaterEqual(artifact["segments"]["count"], 400)
        self.assertEqual(0, artifact["segments"]["sealed_digest_failures"])
        self.assertEqual(
            artifact["replay"]["sequencer_projection_digest"],
            artifact["replay"]["daemonless_projection_digest"],
        )
        self.assertTrue(artifact["replay"]["streaming_direct_equal"])


if __name__ == "__main__":
    unittest.main()
