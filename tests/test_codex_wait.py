"""RED-first contracts for the Floati-native Codex Stop waiter."""

from __future__ import annotations

from floati import fixture_ids as public_ids

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from floati.registry import Registry
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


class CodexWaitParticipationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bus_home = self.base / "demo-fleet"
        self.bus_home.mkdir()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        map_path = self.bus_home / "codex-wait" / "workspaces.v0.json"
        map_path.parent.mkdir()
        map_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": "demo-fleet",
                    "mappings": [],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_unbound_workspace_is_silent_instant_exit_without_node_state(self) -> None:
        """An unmapped global hook must not guess identity or touch node state."""
        from floati.codex_wait import run_stop_waiter

        stdout = io.StringIO()
        stderr = io.StringIO()
        before = {
            path.relative_to(self.bus_home).as_posix()
            for path in self.bus_home.rglob("*")
        }
        started = time.monotonic()
        status = run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={"cwd": str(self.workspace), "session_id": "thread-one"},
            stdout=stdout,
            stderr=stderr,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(0, status)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())
        self.assertLess(elapsed, 0.25)
        self.assertEqual(
            before,
            {
                path.relative_to(self.bus_home).as_posix()
                for path in self.bus_home.rglob("*")
            },
        )

    def test_waiter_never_opens_foreign_bus_root(self) -> None:
        """Ambient transport variables cannot redirect an unbound Floati waiter."""
        from floati.codex_wait import run_stop_waiter

        foreign_root = self.base / "foreign-bus"
        foreign_root.mkdir()
        observed: list[Path] = []
        real_open = Path.open

        def recording_open(path: Path, *args: object, **kwargs: object):
            observed.append(Path(path).resolve(strict=False))
            return real_open(path, *args, **kwargs)

        with mock.patch.dict(
            os.environ,
            {
                "AGENT_BUS_ROOT": str(foreign_root),
                "FLOATI_BUS_ROOT": str(foreign_root),
                "CODEX_BUS_AGENT": "foreign-node",
            },
            clear=False,
        ), mock.patch.object(Path, "open", recording_open):
            status = run_stop_waiter(
                bus_home=self.bus_home,
                hook_payload={"cwd": str(self.workspace), "session_id": "thread-two"},
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(0, status)
        self.assertTrue(observed)
        for path in observed:
            with self.subTest(path=path):
                self.assertFalse(path == foreign_root or foreign_root in path.parents)


class CodexWaitContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bus_home = self.base / "demo-fleet"
        self.root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        Registry(self.root).register(public_ids.builder('floati'), "worker")
        self.workspace = self.base / "workspace"
        self.nested = self.workspace / "nested"
        self.nested.mkdir(parents=True)
        (self.nested / "child").mkdir()

    def write_map(self, mappings: list[dict[str, str]]) -> None:
        path = self.bus_home / "codex-wait" / "workspaces.v0.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": "demo-fleet",
                    "mappings": mappings,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )

    def test_longest_prefix_map_binding_resolves_through_active_registry(self) -> None:
        from floati.codex_wait_contract import resolve_participant

        self.write_map(
            [
                {"workspace": str(self.workspace), "node_id": public_ids.builder('floati')},
                {"workspace": str(self.nested), "node_id": public_ids.builder('floati')},
            ]
        )
        participant = resolve_participant(self.bus_home, self.nested / "child")

        self.assertIsNotNone(participant)
        assert participant is not None
        self.assertEqual(self.nested, participant.binding.workspace)
        self.assertEqual(public_ids.builder('floati'), participant.binding.node_id)
        self.assertEqual(self.root.tenant_home, participant.root.tenant_home)

    def test_bound_workspace_without_consent_is_silent(self) -> None:
        from floati.codex_wait import run_stop_waiter

        self.write_map(
            [{"workspace": str(self.workspace), "node_id": public_ids.builder('floati')}]
        )
        consent_path = (
            self.bus_home
            / "receipts"
            / "codex-wait-consent"
            / public_ids.ledger(public_ids.builder('floati'))
        )
        consent_path.unlink(missing_ok=True)
        stdout = io.StringIO()
        status = run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={"cwd": str(self.workspace), "session_id": "thread-three"},
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, status)
        self.assertEqual("", stdout.getvalue())
        self.assertFalse((self.bus_home / "state" / "codex-wait").exists())

    def test_consent_requires_deadline_strictly_below_hook_timeout(self) -> None:
        from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant

        self.write_map(
            [{"workspace": str(self.workspace), "node_id": public_ids.builder('floati')}]
        )
        participant = resolve_participant(self.bus_home, self.workspace)
        assert participant is not None
        ledger = CodexWaitConsentLedger(participant.root)

        with self.assertRaisesRegex(Exception, "deadline"):
            ledger.arm(
                participant.binding,
                hook_timeout_seconds=60,
                wait_deadline_seconds=60,
                idempotency_key="equal-deadline",
            )

    def test_explicit_arm_and_takeover_are_predecessor_bound(self) -> None:
        from floati import codex_wait_contract

        self.assertTrue(
            hasattr(codex_wait_contract, "CodexWaitSessionLedger"),
            "the ruled armed-session ledger is absent",
        )
        self.write_map(
            [{"workspace": str(self.workspace), "node_id": public_ids.builder('floati')}]
        )
        participant = codex_wait_contract.resolve_participant(
            self.bus_home, self.workspace
        )
        assert participant is not None
        consent = codex_wait_contract.CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="session-consent",
        )
        ledger = codex_wait_contract.CodexWaitSessionLedger(self.root)

        first = ledger.arm(
            participant.binding,
            consent,
            "seat-one",
            idempotency_key="arm-one",
        )
        second = ledger.arm(
            participant.binding,
            consent,
            "seat-two",
            idempotency_key="arm-two",
        )

        self.assertEqual("arm", first["operation"])
        self.assertIsNone(first["predecessor_receipt_id"])
        self.assertEqual("takeover", second["operation"])
        self.assertEqual(first["id"], second["predecessor_receipt_id"])
        self.assertEqual("seat-two", second["acting_session_id"])

    def test_legacy_claim_allows_exactly_one_organic_session(self) -> None:
        from floati import codex_wait_contract

        self.assertTrue(
            hasattr(codex_wait_contract, "CodexWaitSessionLedger"),
            "the ruled armed-session ledger is absent",
        )
        self.write_map(
            [{"workspace": str(self.workspace), "node_id": public_ids.builder('floati')}]
        )
        participant = codex_wait_contract.resolve_participant(
            self.bus_home, self.workspace
        )
        assert participant is not None
        consent = codex_wait_contract.CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="legacy-consent",
        )
        ledger = codex_wait_contract.CodexWaitSessionLedger(self.root)

        first = ledger.participate(participant.binding, consent, "seat-one")
        second = ledger.participate(participant.binding, consent, "seat-two")

        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual("claim", first["operation"])
        self.assertEqual("seat-one", first["acting_session_id"])
        self.assertIsNone(second)
        rows = read_records_snapshot(
            self.root,
            public_ids.compose('receipts/codex-wait-session/', public_ids.ledger(public_ids.builder('floati'))),
            allowed_kinds={"codex_wait_session_receipt"},
        )
        self.assertEqual(1, len(rows))

    def test_workspace_map_growth_does_not_reopen_organic_claiming(self) -> None:
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger,
            CodexWaitSessionLedger,
            resolve_participant,
        )

        self.write_map(
            [{"workspace": str(self.workspace), "node_id": public_ids.builder('floati')}]
        )
        original = resolve_participant(self.bus_home, self.workspace)
        assert original is not None
        original_consent = CodexWaitConsentLedger(self.root).arm(
            original.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="map-growth-original-consent",
        )
        ledger = CodexWaitSessionLedger(self.root)
        first = ledger.participate(original.binding, original_consent, "seat-one")
        self.assertIsNotNone(first)

        second_workspace = self.base / "second-workspace"
        second_workspace.mkdir()
        self.write_map(
            sorted(
                [
                {"workspace": str(self.workspace), "node_id": public_ids.builder('floati')},
                {"workspace": str(second_workspace), "node_id": public_ids.builder('floati')},
                ],
                key=lambda row: (row["workspace"], row["node_id"]),
            )
        )
        evolved = resolve_participant(self.bus_home, self.workspace)
        assert evolved is not None
        evolved_consent = CodexWaitConsentLedger(self.root).require_armed(
            evolved.binding
        )

        self.assertIsNone(
            ledger.participate(evolved.binding, evolved_consent, "seat-two")
        )
        retained = ledger.participate(evolved.binding, evolved_consent, "seat-one")
        self.assertIsNotNone(retained)
        assert retained is not None
        self.assertEqual("seat-one", retained["acting_session_id"])

    def test_armed_session_receipt_matches_the_closed_v1_schema(self) -> None:
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger,
            CodexWaitSessionLedger,
            resolve_participant,
        )

        self.write_map(
            [{"workspace": str(self.workspace), "node_id": public_ids.builder('floati')}]
        )
        participant = resolve_participant(self.bus_home, self.workspace)
        assert participant is not None
        consent = CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="schema-consent",
        )
        receipt = CodexWaitSessionLedger(self.root).arm(
            participant.binding,
            consent,
            "seat-schema",
            idempotency_key="schema-arm",
        )
        schema = Path("schemas/v1/codex-wait-session-receipt.schema.json")

        self.assertTrue(schema.is_file(), "the closed v1 session schema is absent")
        validate_json_schema(receipt, schema)


class CodexWaitRuntimeTests(CodexWaitContractTests):
    def setUp(self) -> None:
        super().setUp()
        Registry(self.root).register("architect", "architect")
        self.write_map(
            [{"workspace": str(self.workspace), "node_id": public_ids.builder('floati')}]
        )
        from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant

        participant = resolve_participant(self.bus_home, self.workspace)
        assert participant is not None
        self.participant = participant
        CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="runtime-consent",
        )

    def arm_session(self, session_id: str) -> dict:
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger,
            CodexWaitSessionLedger,
        )

        consent = CodexWaitConsentLedger(self.root).require_armed(
            self.participant.binding
        )
        return CodexWaitSessionLedger(self.root).arm(
            self.participant.binding,
            consent,
            session_id,
            idempotency_key="runtime-arm-" + session_id,
        )

    def bus_bytes(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.bus_home).as_posix(): path.read_bytes()
            for path in self.bus_home.rglob("*")
            if path.is_file()
        }

    def send(self, key: str) -> dict:
        return EventLog(self.root).send(
            "architect",
            public_ids.builder('floati'),
            "floati",
            "a" * 40,
            "docs/evidence/ping.md",
            "live ping",
            idempotency_key=key,
        )

    def test_fresh_envelope_emits_one_block_and_records_wake_after_flush(self) -> None:
        from floati.codex_wait import run_stop_waiter

        message = self.send("runtime-ping")
        stdout = io.StringIO()
        status = run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={"cwd": str(self.workspace), "session_id": "thread-live"},
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, status)
        decision = json.loads(stdout.getvalue())
        self.assertEqual("block", decision["decision"])
        self.assertIn(message["id"], decision["reason"])
        attempts = read_records_snapshot(
            self.root,
            public_ids.compose('receipts/wakes/', public_ids.ledger(public_ids.builder('floati'))),
            allowed_kinds={"wake_attempt_receipt"},
        )
        self.assertEqual(1, len(attempts))
        self.assertEqual("woke", attempts[0]["outcome"])
        self.assertEqual([message["id"]], attempts[0]["item_ids"])
        deliveries = read_records_snapshot(
            self.root,
            public_ids.compose('receipts/deliveries/', public_ids.ledger(public_ids.builder('floati'))),
            allowed_kinds={"delivery_receipt", "wake_hold_receipt"},
        )
        self.assertEqual(["wake_hold_receipt"], [row["kind"] for row in deliveries])
        self.assertFalse((self.bus_home / "receipts" / "acks" / public_ids.ledger(public_ids.builder('floati'))).exists())

    def test_non_armed_session_cannot_steal_a_fresh_wake(self) -> None:
        from floati.codex_wait import run_stop_waiter

        self.arm_session("seat-primary")
        self.send("two-session-theft")
        before = self.bus_bytes()
        stdout = io.StringIO()
        stderr = io.StringIO()

        status = run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={
                "cwd": str(self.workspace),
                "session_id": "spawned-peer",
            },
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(0, status)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(before, self.bus_bytes())
        self.assertFalse(
            (self.bus_home / "receipts" / "wakes" / public_ids.ledger(public_ids.builder('floati'))).exists()
        )
        self.assertFalse(
            (self.bus_home / "receipts" / "deliveries" / public_ids.ledger(public_ids.builder('floati'))).exists()
        )

    def test_non_armed_breaker_invocations_are_byte_identical(self) -> None:
        from floati.codex_wait import _breaker_tripped, run_stop_waiter

        self.arm_session("seat-primary")
        breaker = (
            self.bus_home
            / "state"
            / "codex-wait"
            / public_ids.builder('floati')
            / "breaker.json"
        )
        self.assertFalse(_breaker_tripped(self.root, public_ids.builder('floati'), now=1000.0))
        before = breaker.read_bytes()
        clock = [0.0]

        for _ in range(25):
            run_stop_waiter(
                bus_home=self.bus_home,
                hook_payload={
                    "cwd": str(self.workspace),
                    "session_id": "spawned-peer",
                },
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                monotonic=lambda: clock[0],
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                wall_time=lambda: 1000.0,
            )

        self.assertEqual(before, breaker.read_bytes())

    def test_held_retry_does_not_emit_a_second_wake(self) -> None:
        from floati.codex_wait import run_stop_waiter

        self.send("runtime-held")
        first = io.StringIO()
        second = io.StringIO()
        clock = [0.0]
        payload = {"cwd": str(self.workspace), "session_id": "thread-held"}
        run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload=payload,
            stdout=first,
            stderr=io.StringIO(),
        )
        run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload=payload,
            stdout=second,
            stderr=io.StringIO(),
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )

        self.assertIn('"decision": "block"', first.getvalue())
        self.assertIn("deadline exhausted", second.getvalue())
        attempts = read_records_snapshot(
            self.root,
            public_ids.compose('receipts/wakes/', public_ids.ledger(public_ids.builder('floati'))),
            allowed_kinds={"wake_attempt_receipt"},
        )
        self.assertEqual(1, len(attempts))

    def test_held_only_keeps_waiting_and_surfaces_later_fresh_work(self) -> None:
        from floati.codex_wait import run_stop_waiter

        self.send("runtime-held-first")
        payload = {"cwd": str(self.workspace), "session_id": "thread-held-later"}
        run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload=payload,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        clock = [0.0]
        later: list[dict] = []

        def sleep(seconds: float) -> None:
            clock[0] += seconds
            if not later:
                later.append(self.send("runtime-held-later"))

        stdout = io.StringIO()
        run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload=payload,
            stdout=stdout,
            stderr=io.StringIO(),
            monotonic=lambda: clock[0],
            sleep=sleep,
        )

        self.assertEqual(1.0, clock[0])
        self.assertIn(later[0]["id"], stdout.getvalue())
        attempts = read_records_snapshot(
            self.root,
            public_ids.compose('receipts/wakes/', public_ids.ledger(public_ids.builder('floati'))),
            allowed_kinds={"wake_attempt_receipt"},
        )
        self.assertEqual(2, len(attempts))

    def test_clean_deadline_emits_rearm_and_exhaustion_receipt(self) -> None:
        from floati.codex_wait import run_stop_waiter

        clock = [0.0]

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        stdout = io.StringIO()
        status = run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={"cwd": str(self.workspace), "session_id": "thread-idle"},
            stdout=stdout,
            stderr=io.StringIO(),
            monotonic=monotonic,
            sleep=sleep,
            poll_interval_seconds=1.0,
        )

        self.assertEqual(0, status)
        decision = json.loads(stdout.getvalue())
        self.assertEqual("block", decision["decision"])
        self.assertIn("deadline exhausted", decision["reason"])
        rows = read_records_snapshot(
            self.root,
            public_ids.compose('receipts/codex-wait-exhaustion/', public_ids.ledger(public_ids.builder('floati'))),
            allowed_kinds={"codex_wait_exhaustion_receipt"},
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("rearmed", rows[0]["outcome"])
        self.assertEqual(2, rows[0]["waited_seconds"])

    def test_idle_waiter_reads_each_nonempty_ledger_once_then_uses_its_cursor(self) -> None:
        """Measures complete-ledger reads across repeated polls in one waiter."""
        from floati.codex_wait import run_stop_waiter

        self.send("incremental-reader-held")
        payload = {
            "cwd": str(self.workspace),
            "session_id": "thread-incremental-reader",
        }
        run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload=payload,
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        measured_paths = {
            self.root.resolve_relative("events.jsonl").resolve(strict=False),
            self.root.resolve_relative(
                public_ids.compose(
                    "receipts/deliveries/",
                    public_ids.ledger(public_ids.builder("floati")),
                )
            ).resolve(strict=False),
        }
        read_counts = {path: 0 for path in measured_paths}
        real_read_bytes = Path.read_bytes

        def recording_read_bytes(path: Path) -> bytes:
            resolved = path.resolve(strict=False)
            if resolved in read_counts:
                read_counts[resolved] += 1
            return real_read_bytes(path)

        clock = [0.0]
        with mock.patch.object(Path, "read_bytes", recording_read_bytes):
            run_stop_waiter(
                bus_home=self.bus_home,
                hook_payload=payload,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                monotonic=lambda: clock[0],
                sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                poll_interval_seconds=1.0,
            )

        self.assertEqual(
            {path: 1 for path in measured_paths},
            read_counts,
            "one idle waiter replayed a complete nonempty ledger after its first poll",
        )

    def test_legacy_escape_marker_cannot_disarm_the_armed_session(self) -> None:
        from floati.codex_wait import run_stop_waiter

        self.arm_session("thread-detached")
        self.send("runtime-marker")
        digest = __import__("hashlib").sha256(b"thread-detached").hexdigest()
        marker = self.bus_home / "state" / "codex-wait" / "disabled" / digest
        marker.parent.mkdir(parents=True)
        marker.write_text("disabled\n", encoding="utf-8")
        stdout = io.StringIO()
        status = run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={"cwd": str(self.workspace), "session_id": "thread-detached"},
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, status)
        self.assertIn('"decision": "block"', stdout.getvalue())
        attempts = read_records_snapshot(
            self.root,
            public_ids.compose('receipts/wakes/', public_ids.ledger(public_ids.builder('floati'))),
            allowed_kinds={"wake_attempt_receipt"},
        )
        self.assertEqual("thread-detached", attempts[0]["acting_session_id"])

    def test_trusted_waiter_records_the_exact_codex_daemon_binding(self) -> None:
        from floati import wake_daemon_adapters as adapters
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
        from floati.codex_wait import run_stop_waiter

        executable = self.base / "codex-target"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        link = self.base / "codex"
        link.symlink_to(executable)
        clock = [0.0]
        prior = adapters.CODEX_EXECUTABLE
        adapters.CODEX_EXECUTABLE = link
        self.addCleanup(setattr, adapters, "CODEX_EXECUTABLE", prior)

        run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={"cwd": str(self.workspace), "session_id": "thread-bound"},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )

        coordinate = DaemonCoordinate(self.root, public_ids.builder('floati'), "codex")
        binding = AdapterBindingStore(self.root).read(coordinate)
        self.assertEqual("thread-bound", binding["session_id"])
        self.assertEqual(str(self.workspace), binding["workspace"])
        self.assertEqual(str(executable), binding["executable"])
        self.assertEqual(adapters.adapter_contract_digest("codex"), binding["adapter_digest"])

    def test_malformed_session_never_creates_a_daemon_binding(self) -> None:
        from floati import wake_daemon_adapters as adapters
        from floati.codex_wait import run_stop_waiter

        executable = self.base / "codex-target"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        prior = adapters.CODEX_EXECUTABLE
        adapters.CODEX_EXECUTABLE = executable
        self.addCleanup(setattr, adapters, "CODEX_EXECUTABLE", prior)

        run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={"cwd": str(self.workspace), "session_id": "not a session"},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

        self.assertFalse((self.bus_home / "state" / "wake-daemon" / "adapters").exists())

    def test_breaker_trips_after_twenty_invocations_in_one_window(self) -> None:
        from floati.codex_wait import _breaker_tripped

        outcomes = [
            _breaker_tripped(self.root, public_ids.builder('floati'), now=1000.0)
            for _ in range(21)
        ]

        self.assertEqual([False] * 20 + [True], outcomes)
        state = json.loads(
            (
                self.bus_home
                / "state"
                / "codex-wait"
                / public_ids.builder('floati')
                / "breaker.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(21, len(state["hits"]))


class CodexWaitLedgerReopenTests(CodexWaitRuntimeTests):
    """IN-6d — the waiter must follow its consent ledger by PATH, not by one snapshot.

    ``run_stop_waiter`` reads the consent receipt exactly once, before the poll
    loop, and every later decision rides that one in-memory snapshot: the wait
    deadline and the ``consent_receipt_id`` written into each session claim.
    The consent ledger path is never re-stated and its ``(st_dev, st_ino)`` is
    never observed, so a ledger repair, rotation or restore that replaces the
    file under a running waiter is invisible to it.
    """

    CONSENT_RELATIVE = public_ids.compose(
        "receipts/codex-wait-consent/",
        public_ids.ledger(public_ids.builder("floati")),
    )
    REOPEN_KIND = "codex_wait_reopen_fact"
    REOPEN_FIELDS = {
        "schema_version",
        "kind",
        "tenant_id",
        "timestamp",
        "node_id",
        "session_digest",
        "ledger",
        "before",
        "after",
        "waited_seconds",
        "outcome",
        "invocation_id",
    }

    def consent_ledger_path(self) -> Path:
        return self.root.resolve_relative(self.CONSENT_RELATIVE)

    def reopen_facts_path(self) -> Path:
        return self.root.resolve_relative(
            Path("state/codex-wait")
            / public_ids.builder("floati")
            / "reopen.jsonl"
        )

    def reopen_facts(self) -> list[dict]:
        path = self.reopen_facts_path()
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    @staticmethod
    def identity(path: Path) -> dict[str, int]:
        status = path.stat()
        return {"device": status.st_dev, "inode": status.st_ino}

    def replace_ledger(self, path: Path, payload: bytes) -> tuple[dict, dict]:
        """Repair/rotate/restore exactly as an operator would: a new inode."""

        before = self.identity(path)
        temporary = path.with_name(path.name + ".repaired")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        after = self.identity(path)
        self.assertNotEqual(before, after)
        return before, after

    def exhaustion_rows(self) -> list[dict]:
        return read_records_snapshot(
            self.root,
            public_ids.compose(
                "receipts/codex-wait-exhaustion/",
                public_ids.ledger(public_ids.builder("floati")),
            ),
            allowed_kinds={"codex_wait_exhaustion_receipt"},
        )

    def exit_rows(self) -> list[dict]:
        relative = public_ids.compose(
            "receipts/wake-waiter-exit/",
            public_ids.ledger(public_ids.builder("floati")),
        )
        if not self.root.resolve_relative(relative).exists():
            return []
        return read_records_snapshot(
            self.root, relative, allowed_kinds={"wake_waiter_exit_receipt"}
        )

    def run_with_replacement(self, payload: bytes) -> tuple[io.StringIO, tuple[dict, dict]]:
        from floati.codex_wait import run_stop_waiter

        consent_path = self.consent_ledger_path()
        clock = [0.0]
        swapped: list[tuple[dict, dict]] = []

        def sleep(seconds: float) -> None:
            clock[0] += seconds
            if not swapped:
                swapped.append(self.replace_ledger(consent_path, payload))

        stdout = io.StringIO()
        status = run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={
                "cwd": str(self.workspace),
                "session_id": "thread-reopen",
            },
            stdout=stdout,
            stderr=io.StringIO(),
            monotonic=lambda: clock[0],
            sleep=sleep,
            poll_interval_seconds=1.0,
        )
        self.assertEqual(0, status)
        self.assertEqual(1, len(swapped), "the consent ledger was never replaced")
        return stdout, swapped[0]

    def test_repaired_consent_ledger_is_reopened_and_the_wait_continues_in_place(
        self,
    ) -> None:
        """A repair that lengthens the deadline must be adopted from the ORIGINAL start."""

        from floati.codex_wait_contract import CodexWaitConsentLedger

        consent_path = self.consent_ledger_path()
        original = consent_path.read_bytes()
        CodexWaitConsentLedger(self.root).arm(
            self.participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=5,
            idempotency_key="repaired-consent",
        )
        repaired = consent_path.read_bytes()
        self.assertNotEqual(original, repaired)
        self.replace_ledger(consent_path, original)

        stdout, (before, after) = self.run_with_replacement(repaired)

        decision = json.loads(stdout.getvalue())
        self.assertEqual("block", decision["decision"])
        self.assertIn("deadline exhausted", decision["reason"])
        rows = self.exhaustion_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(
            5,
            rows[0]["waited_seconds"],
            "the waiter exhausted on the pre-repair deadline it can no longer read",
        )
        facts = self.reopen_facts()
        self.assertEqual(1, len(facts), "no typed reopen record was written")
        fact = facts[0]
        self.assertEqual(self.REOPEN_FIELDS, set(fact))
        self.assertEqual(1, fact["schema_version"])
        self.assertEqual(self.REOPEN_KIND, fact["kind"])
        self.assertEqual("demo-fleet", fact["tenant_id"])
        self.assertEqual(public_ids.builder("floati"), fact["node_id"])
        self.assertEqual(
            Path(self.CONSENT_RELATIVE).as_posix(), fact["ledger"]
        )
        self.assertEqual(before, fact["before"])
        self.assertEqual(after, fact["after"])
        self.assertEqual("rearmed", fact["outcome"])
        self.assertEqual(
            1,
            fact["waited_seconds"],
            "the reopen must record the position the wait continued from",
        )

    def test_restored_consent_ledger_without_this_workspace_releases_the_seat(
        self,
    ) -> None:
        """A restore that removes consent must stop the hold, not run to exhaustion."""

        stdout, (before, after) = self.run_with_replacement(b"")

        self.assertEqual(
            "",
            stdout.getvalue(),
            "the waiter kept blocking the turn against a consent receipt that is gone",
        )
        self.assertEqual([], self.exhaustion_rows())
        exits = self.exit_rows()
        self.assertEqual(1, len(exits))
        self.assertEqual("consent_withdrawn", exits[0]["reason_code"])
        self.assertEqual(1, exits[0]["waited_seconds"])
        facts = self.reopen_facts()
        self.assertEqual(1, len(facts), "no typed reopen record was written")
        fact = facts[0]
        self.assertEqual(self.REOPEN_FIELDS, set(fact))
        self.assertEqual(self.REOPEN_KIND, fact["kind"])
        self.assertEqual(before, fact["before"])
        self.assertEqual(after, fact["after"])
        self.assertEqual("consent_withdrawn", fact["outcome"])
        self.assertEqual(1, fact["waited_seconds"])

    def test_an_unreplaced_consent_ledger_records_no_reopen(self) -> None:
        """The watch must not fire on ordinary in-place appends or a quiet wait."""

        from floati.codex_wait import run_stop_waiter

        clock = [0.0]
        run_stop_waiter(
            bus_home=self.bus_home,
            hook_payload={"cwd": str(self.workspace), "session_id": "thread-quiet"},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            poll_interval_seconds=1.0,
        )

        self.assertEqual([], self.reopen_facts())
        rows = self.exhaustion_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["waited_seconds"])


if __name__ == "__main__":
    unittest.main()
