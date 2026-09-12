"""SKEW-2-F1: the cycle_exception receipt names the exception, never a traceback.

A daemon cycle that raises records one `cycle_exception` lifecycle receipt
(SKEW-2 Am.1) whose `reason_code` is the fixed code. That receipt named
neither the exception's type nor its message, so the ledger could say only
"something raised". The receipt now carries the exception's type and one
bounded, terminal-safe message beside the fixed code — never a traceback.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids
from floati.jsonl import read_records
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import (
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
)
from floati.wake_daemon_roll import lifecycle_relative
from tests.temp_roots import REAL_TEMP_ROOT

EXCEPTION_MESSAGE_BOUND = 128


class CycleExceptionFixture:
    """One bound daemon whose cycles fault with the exception under test."""

    def __init__(self, base: Path) -> None:
        self.root = FloatiRoot.open_direct_home(base / "fleet", create=True)
        Registry(self.root).register(public_ids.builder("a"), "worker")
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.executable = base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "cursor")
        adapter = _IdleAdapter(self.root, self.coordinate)
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=1,
        )
        DaemonConsentLedger(self.root).consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=4,
            max_backoff_seconds=8,
            activation_epoch=1,
            idempotency_key="skew2-f1-consent",
        )
        from floati.wake_daemon import WakeDaemon

        self.daemon = WakeDaemon(self.coordinate, adapter)

    def cycle_exception_rows(self) -> list[dict]:
        lifecycle = read_records(
            self.root,
            lifecycle_relative(self.coordinate.node_id),
            allowed_kinds={
                "wake_daemon_lifecycle_receipt",
                "wake_daemon_consent_receipt",
            },
        )
        return [
            row
            for row in lifecycle
            if row.get("kind") == "wake_daemon_lifecycle_receipt"
            and row.get("event") == "cycle_exception"
        ]

    def serve_one_exception(self, exception: BaseException) -> None:
        calls = {"count": 0}

        def fake_run_cycle(now: float) -> dict:
            calls["count"] += 1
            if calls["count"] == 1:
                raise exception
            return {"next_poll_at": now + 1.0, "state": "idle"}

        with mock.patch.object(self.daemon, "run_cycle", side_effect=fake_run_cycle):
            self.daemon.serve(
                lambda: calls["count"] >= 2,
                clock=lambda: 100.0,
                sleep=lambda delay: None,
            )


class _IdleAdapter:
    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.coordinate = coordinate
        self.store = AdapterBindingStore(root)

    def exact_binding(self):
        from floati.wake_daemon_adapters import AdapterBinding

        return AdapterBinding.from_record(self.store.read(self.coordinate))

    def observe_session(self, binding: object) -> str:
        return "unknown"

    def request_wake(
        self,
        binding: object,
        reason: str,
        deadline_seconds: int,
        envelopes: object = None,
    ):
        from floati.wake_daemon_adapters import WakeAdapterResult

        return WakeAdapterResult("woke", None, 0, "e" * 64)


class CycleExceptionReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = CycleExceptionFixture(Path(self.temporary.name))

    def test_runtime_error_receipt_names_type_and_bounded_message(self) -> None:
        """RED today: RuntimeError('boom') leaves a receipt naming neither."""

        self.fixture.serve_one_exception(RuntimeError("boom"))

        rows = self.fixture.cycle_exception_rows()
        self.assertEqual(1, len(rows))
        row = rows[-1]
        self.assertEqual("unknown", row["state"])
        self.assertEqual(
            "wake_daemon_cycle_exception",
            row["reason_code"],
            "the fixed code stays the fixed code",
        )
        self.assertEqual("RuntimeError", row["exception_type"])
        self.assertEqual("boom", row["exception_message"])

    def test_typed_refusal_receipt_names_the_refusal_and_its_detail(self) -> None:
        from floati.errors import IntegrityFailure

        self.fixture.serve_one_exception(
            IntegrityFailure(
                "consumption_state_unavailable", "synthetic cycle fault"
            )
        )

        rows = self.fixture.cycle_exception_rows()
        self.assertEqual(1, len(rows))
        row = rows[-1]
        self.assertEqual("consumption_state_unavailable", row["reason_code"])
        self.assertEqual("IntegrityFailure", row["exception_type"])
        self.assertEqual("synthetic cycle fault", row["exception_message"])

    def test_raising_str_does_not_kill_the_recovery(self) -> None:
        """SKEW-2-F1 Am.1: an exception whose __str__ raises is still recorded.

        The recovery itself called str(exc) on the fault, so a raising
        __str__ crashed the handler and killed serve() — the exact silence
        the recovery exists to prevent. The receipt must still name the
        type, with the type name as the bounded message (a typed absence:
        the message was unobtainable), and the daemon must keep polling.
        """

        class BrokenStr(Exception):
            def __str__(self) -> str:
                raise RuntimeError("str is broken")

        self.fixture.serve_one_exception(BrokenStr())

        rows = self.fixture.cycle_exception_rows()
        self.assertEqual(1, len(rows))
        row = rows[-1]
        self.assertEqual("wake_daemon_cycle_exception", row["reason_code"])
        self.assertEqual("BrokenStr", row["exception_type"])
        self.assertEqual("BrokenStr", row["exception_message"])

    def test_control_char_class_name_does_not_kill_the_recovery(self) -> None:
        """SKEW-2-F1 Am.2 RED: an unprintable class name crashed the write.

        The guard covered str(exc) only — the exception's TYPE flowed
        unguarded into the receipt writer, whose field validator refuses
        control and bidirectional-control characters, so a class named
        with \\x01 or U+202E crashed _recover_cycle_exception exactly as
        before: no receipt, one cycle, silence.
        """

        broken = type("Bad\x01Name", (RuntimeError,), {})

        self.fixture.serve_one_exception(broken("boom"))

        rows = self.fixture.cycle_exception_rows()
        self.assertEqual(1, len(rows))
        row = rows[-1]
        self.assertEqual("wake_daemon_cycle_exception", row["reason_code"])
        self.assertNotIn("\x01", row["exception_type"])
        self.assertEqual("BadName", row["exception_type"])
        self.assertEqual("boom", row["exception_message"])

    def test_a_refused_write_leaves_a_minimal_receipt_naming_the_refusal(self) -> None:
        """SKEW-2-F1 Am.2 RED: the receipt write refusing is itself recorded.

        The recovery may never raise: when the full receipt refuses, a
        minimal typed receipt names the refusal as its reason_code. The
        full attempt is the one carrying the exception fields, so the
        stub refuses exactly that call and delegates the rest to the
        real writer.
        """

        from floati.errors import ProtocolRefusal

        real_record = self.fixture.daemon.lifecycle.record

        def flaky(*args: object, **kwargs: object) -> dict:
            if "exception_type" in kwargs:
                raise ProtocolRefusal(
                    "wake_daemon_receipt_refused", "synthetic receipt refusal"
                )
            return real_record(*args, **kwargs)

        with mock.patch.object(
            self.fixture.daemon.lifecycle, "record", side_effect=flaky
        ) as records:
            self.fixture.serve_one_exception(RuntimeError("boom"))

        self.assertGreaterEqual(records.call_count, 2)
        rows = self.fixture.cycle_exception_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("wake_daemon_receipt_refused", rows[-1]["reason_code"])
        self.assertNotIn("exception_type", rows[-1])

    def test_a_failing_receipt_write_still_continues_the_loop(self) -> None:
        """SKEW-2-F1 Am.2 RED: even a doubled write failure never raises."""

        with mock.patch.object(
            self.fixture.daemon.lifecycle,
            "record",
            side_effect=OSError("disk is unhappy"),
        ):
            self.fixture.serve_one_exception(RuntimeError("boom"))

        rows = self.fixture.cycle_exception_rows()
        self.assertEqual([], rows, "no receipt could land; the loop continues anyway")

    def test_oversized_message_is_bounded(self) -> None:
        """The message is testimony, bounded like every other text field."""

        self.fixture.serve_one_exception(RuntimeError("x" * 5000))

        row = self.fixture.cycle_exception_rows()[-1]
        self.assertLessEqual(len(row["exception_message"]), EXCEPTION_MESSAGE_BOUND)
        self.assertTrue(row["exception_message"].startswith("x" * 16))

    def test_message_is_never_terminal_unsafe(self) -> None:
        """A multi-line exception leaves one terminal-safe bounded line."""

        self.fixture.serve_one_exception(RuntimeError("boom\nsecret\tnext"))

        row = self.fixture.cycle_exception_rows()[-1]
        message = row["exception_message"]
        self.assertNotIn("\n", message)
        self.assertNotIn("\t", message)
        self.assertTrue(message.startswith("boom"))

    def test_plain_event_receipts_keep_their_exact_shape(self) -> None:
        """Non-exception receipts do not grow the fields: one ledger carries
        both shapes, and both still validate through the reader."""

        from floati.wake_daemon_contract import DaemonLifecycleLedger

        self.fixture.serve_one_exception(RuntimeError("boom"))
        DaemonLifecycleLedger(self.fixture.root).record(
            self.fixture.coordinate,
            daemon_instance_id="skew2-f1-instance",
            activation_epoch=1,
            event="started",
            state="running",
            reason_code=None,
            adapter_digest=adapter_contract_digest("cursor"),
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="skew2-f1-plain-row",
        )

        lifecycle = read_records(
            self.fixture.root,
            lifecycle_relative(self.fixture.coordinate.node_id),
            allowed_kinds={
                "wake_daemon_lifecycle_receipt",
                "wake_daemon_consent_receipt",
            },
        )
        exception_rows = [
            row
            for row in lifecycle
            if row.get("event") == "cycle_exception"
        ]
        plain_rows = [
            row
            for row in lifecycle
            if row.get("event") == "started"
        ]
        self.assertEqual(1, len(exception_rows))
        self.assertEqual(1, len(plain_rows))
        self.assertNotIn("exception_type", plain_rows[-1])
        self.assertNotIn("exception_message", plain_rows[-1])
        self.assertIn("exception_type", exception_rows[-1])
        self.assertIn("exception_message", exception_rows[-1])


if __name__ == "__main__":
    unittest.main()
