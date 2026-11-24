"""WAKE-RETIRE-KEY-1: repeated stale-epoch serve retirement must not relaunch.

Architect: calling `_wake_daemon_serve` twice with stale epoch 1 vs active 2
raises `wake_daemon_lifecycle_idempotency_conflict` on
`serve-startup-retired-...-1-2` because `predecessor_receipt_id` moved.
Remedy kind none -> CLI exit 20 -> launchd KeepAlive relaunches forever.

Property: a repeated retirement under the same stale epoch is idempotent
success (exit 0) or a refusal whose remedy clears it — never a relaunch
loop. The ledger guard is NOT loosened: a genuinely different retirement
still refuses.

Ways this check could be wrong, each with an adversarial case below:

1. Single-call only (WD-2 already covers one startup mismatch). The loop
   is the second KeepAlive spawn. Case: two calls, same stale pair.
2. Third retirement. A fix that appends a second key on retry still
   conflicts on the third. Case: three calls, still one receipt, all
   exit 0.
3. Epoch moving again. After 1-vs-2 retires, consent moves to 3; the
   supervisor still carries epoch 1. That is a different key (`...-1-3`),
   not a replay of `...-1-2`. Case: new retirement succeeds; old receipt
   stays; no conflict.
4. Concurrent loads. Two KeepAlive children overlapping. Case: two
   threads, both exit 0, still one `...-1-2` receipt.
5. Intervening lifecycle row. If predecessor is "whatever is last now",
   a cycle receipt between retries moves the pointer and re-conflicts.
   Case: after first retirement, record a cycle row, second serve still
   exit 0.
6. Loosening the ledger. Swallowing every conflict on that key hides a
   different payload. Case: same key, different reason_code, still
   `wake_daemon_lifecycle_idempotency_conflict`.
9. Pre-fix receipt. A first retirement written at 3c83f002 stores a
   non-null predecessor. After upgrade, record(None) conflicts and the
   seat still loops. Case: plant that shape, two serves, 0/0, one
   receipt, no write.
10. Matching epoch. A pin that always retires would stop a live
    supervisor. Case: equal epochs call serve, not the retire arm.
"""

from __future__ import annotations

import argparse
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from floati.admin_cli import _wake_daemon_serve
from floati.errors import ProtocolRefusal
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import DaemonLifecycleLedger
from tests.test_wd_2 import _ServeFixture, _lifecycle_rows, _retire_receipts


def _stale_args(fixture, *, epoch: int) -> argparse.Namespace:
    return argparse.Namespace(
        root=str(fixture.root.path),
        actor=str(fixture.node),
        harness="cursor",
        activation_epoch=epoch,
        zcode_node_executable=None,
        zcode_entry_executable=None,
    )


def _advance_consent(fixture, *, epoch: int, key: str) -> None:
    fixture.consent.consent(
        fixture.coordinate,
        adapter_version="1",
        adapter_digest=adapter_contract_digest("cursor"),
        min_poll_seconds=1,
        max_poll_seconds=30,
        max_backoff_seconds=120,
        activation_epoch=epoch,
        idempotency_key=key,
    )


def _serve(test: unittest.TestCase, args: argparse.Namespace, *, label: str):
    """Call serve; convert the measured KeepAlive refusal into FAIL, not ERROR."""

    try:
        return _wake_daemon_serve(args)
    except ProtocolRefusal as exc:
        test.fail(f"{label} refused {exc.code}: {exc}")


class WakeRetireKeyTests(_ServeFixture):
    """Consent epoch 2, serve still carrying epoch 1 — the KeepAlive subject."""

    def setUp(self) -> None:
        super().setUp()
        _advance_consent(self, epoch=2, key="wd2-consent-2")

    def test_second_stale_epoch_serve_is_idempotent_success(self) -> None:
        """RED: second KeepAlive spawn conflicts because predecessor moved."""

        args = _stale_args(self, epoch=1)
        first = _serve(self, args, label="first stale serve")
        second = _serve(self, args, label="second KeepAlive spawn")

        self.assertEqual(("ok", 0), (first[0], first[2]))
        self.assertEqual(("ok", 0), (second[0], second[2]))
        self.assertEqual("wake_daemon_epoch_moved", second[1]["reason_code"])
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")
        self.assertEqual(1, len(retired), retired)
        key = retired[0]["idempotency_key"]
        self.assertRegex(key, r"^serve-startup-retired-[0-9a-f]{16}-1-2$")

    def test_third_stale_epoch_serve_stays_one_receipt(self) -> None:
        args = _stale_args(self, epoch=1)
        codes = [
            _serve(self, args, label=f"stale serve {index}")[2]
            for index in (1, 2, 3)
        ]
        self.assertEqual([0, 0, 0], codes)
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")
        self.assertEqual(1, len(retired), retired)

    def test_epoch_moving_again_is_a_new_retirement_not_a_conflict(self) -> None:
        first = _serve(self, _stale_args(self, epoch=1), label="1-vs-2 retire")
        self.assertEqual(0, first[2])
        _advance_consent(self, epoch=3, key="wd2-consent-3")
        again = _serve(self, _stale_args(self, epoch=1), label="1-vs-3 retire")
        self.assertEqual(0, again[2])
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")
        keys = [row["idempotency_key"] for row in retired]
        self.assertEqual(2, len(retired), keys)
        self.assertTrue(any(key.endswith("-1-2") for key in keys), keys)
        self.assertTrue(any(key.endswith("-1-3") for key in keys), keys)

    def test_intervening_lifecycle_row_does_not_reopen_the_loop(self) -> None:
        args = _stale_args(self, epoch=1)
        self.assertEqual(0, _serve(self, args, label="first stale serve")[2])
        prior = _lifecycle_rows(self.root, self.node)[-1]
        DaemonLifecycleLedger(self.root).record(
            self.coordinate,
            daemon_instance_id="daemon-serve-cycle",
            activation_epoch=2,
            event="idle",
            state="idle",
            reason_code="idle",
            adapter_digest=adapter_contract_digest("cursor"),
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=str(prior["id"]),
            idempotency_key="wake-retire-key-intervening-cycle",
        )
        second = _serve(self, args, label="retry after intervening lifecycle")
        self.assertEqual(("ok", 0), (second[0], second[2]))
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")
        self.assertEqual(1, len(retired), retired)

    def test_concurrent_stale_serves_share_one_receipt(self) -> None:
        args = _stale_args(self, epoch=1)
        self.assertEqual(0, _serve(self, args, label="plant retirement")[2])
        results: list[object] = []

        def run() -> None:
            try:
                results.append(_wake_daemon_serve(args))
            except ProtocolRefusal as exc:
                results.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run)
            second = pool.submit(run)
            first.result()
            second.result()
        self.assertEqual(2, len(results))
        for result in results:
            self.assertIsInstance(result, tuple, result)
            status, artifact, code = result
            self.assertEqual("ok", status, artifact)
            self.assertEqual(0, code, artifact)
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")
        self.assertEqual(1, len(retired), retired)

    def test_different_payload_on_the_same_key_still_conflicts(self) -> None:
        args = _stale_args(self, epoch=1)
        self.assertEqual(0, _serve(self, args, label="plant retirement")[2])
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")[0]
        with self.assertRaises(ProtocolRefusal) as caught:
            DaemonLifecycleLedger(self.root).record(
                self.coordinate,
                daemon_instance_id=retired["daemon_instance_id"],
                activation_epoch=int(retired["activation_epoch"]),
                event="stopped",
                state="stopped",
                reason_code="wake_daemon_registry_inactive",
                adapter_digest=str(retired["adapter_digest"]),
                plist_digest=None,
                session_digest=None,
                predecessor_receipt_id=retired["predecessor_receipt_id"],
                idempotency_key=str(retired["idempotency_key"]),
            )
        self.assertEqual(
            "wake_daemon_lifecycle_idempotency_conflict", caught.exception.code
        )
        self.assertEqual("none", caught.exception.remedy["kind"])

    def _plant_prefix_startup_retire(
        self,
        *,
        reason_code: str,
        adapter_digest: str,
    ) -> dict:
        ledger = DaemonLifecycleLedger(self.root)
        prior = ledger.record(
            self.coordinate,
            daemon_instance_id="daemon-serve-cycle",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code="idle",
            adapter_digest=adapter_contract_digest("cursor"),
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="wake-retire-key-prefix-prior-" + reason_code,
        )
        key = f"serve-startup-retired-{self.coordinate.digest[:16]}-1-2"
        return ledger.record(
            self.coordinate,
            daemon_instance_id="daemon-serve-startup",
            activation_epoch=1,
            event="stopped",
            state="stopped",
            reason_code=reason_code,
            adapter_digest=adapter_digest,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=str(prior["id"]),
            idempotency_key=key,
        )

    def test_prefix_receipt_with_non_null_predecessor_is_already_recorded(self) -> None:
        """RED: C5 upgrade. Pre-fix CLI left a non-null predecessor."""

        args = _stale_args(self, epoch=1)
        planted = self._plant_prefix_startup_retire(
            reason_code="wake_daemon_epoch_moved",
            adapter_digest=adapter_contract_digest("cursor"),
        )
        first = _serve(self, args, label="upgrade KeepAlive after pre-fix receipt")
        second = _serve(self, args, label="second KeepAlive after pre-fix receipt")
        self.assertEqual(("ok", 0), (first[0], first[2]))
        self.assertEqual(("ok", 0), (second[0], second[2]))
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")
        self.assertEqual(1, len(retired), retired)
        self.assertEqual(planted["id"], retired[0]["id"])
        self.assertEqual(planted["predecessor_receipt_id"], retired[0]["predecessor_receipt_id"])

    def test_prefix_receipt_with_different_reason_code_still_refuses(self) -> None:
        """C6: a pre-fix predecessor plus a different payload is not already recorded."""

        args = _stale_args(self, epoch=1)
        self._plant_prefix_startup_retire(
            reason_code="wake_daemon_registry_inactive",
            adapter_digest=adapter_contract_digest("cursor"),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            _wake_daemon_serve(args)
        self.assertEqual(
            "wake_daemon_lifecycle_idempotency_conflict", caught.exception.code
        )
        self.assertEqual("none", caught.exception.remedy["kind"])

    def test_prefix_receipt_with_different_adapter_digest_still_refuses(self) -> None:
        """C6b: a pre-fix predecessor plus a different adapter_digest still conflicts."""

        args = _stale_args(self, epoch=1)
        self._plant_prefix_startup_retire(
            reason_code="wake_daemon_epoch_moved",
            adapter_digest="b" * 64,
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            _wake_daemon_serve(args)
        self.assertEqual(
            "wake_daemon_lifecycle_idempotency_conflict", caught.exception.code
        )
        self.assertEqual("none", caught.exception.remedy["kind"])

    def test_matching_epoch_goes_on_to_serve(self) -> None:
        """Blind spot named on R1: equal epochs must not take the retire arm."""

        served = {"n": 0}

        def fake_serve(self: object, stop_requested: object, **kwargs: object) -> None:
            served["n"] += 1

        args = argparse.Namespace(
            root=str(self.root.path),
            actor=str(self.node),
            harness="cursor",
            activation_epoch=2,
            zcode_node_executable=None,
            zcode_entry_executable=None,
        )
        with mock.patch("floati.wake_daemon.WakeDaemon.serve", fake_serve):
            status, artifact, code = _wake_daemon_serve(args)
        self.assertEqual(("ok", 0), (status, code))
        self.assertEqual(1, served["n"])
        self.assertEqual({"schema_version": 0, "state": "stopped"}, artifact)
        self.assertEqual(
            [],
            _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved"),
        )


if __name__ == "__main__":
    unittest.main()
