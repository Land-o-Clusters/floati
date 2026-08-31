from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema


SCHEMA_ROOT = Path("schemas/v0")
SCHEMA_V1_ROOT = Path("schemas/v1")


class WakeDaemonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        Registry(self.root).register(public_ids.builder('a'), "Cursor")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)

    def coordinate(self, harness: str = "cursor"):
        from floati.wake_daemon_contract import DaemonCoordinate

        return DaemonCoordinate(self.root, public_ids.builder('a'), harness)

    def test_consent_is_closed_root_bound_and_idempotent(self) -> None:
        from floati.wake_daemon_contract import DaemonConsentLedger

        coordinate = self.coordinate()
        ledger = DaemonConsentLedger(self.root)
        receipt = ledger.consent(
            coordinate,
            adapter_version="1",
            adapter_digest="a" * 64,
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key="consent-1",
        )
        replay = ledger.consent(
            coordinate,
            adapter_version="1",
            adapter_digest="a" * 64,
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key="consent-1",
        )

        self.assertEqual(receipt, replay)
        self.assertEqual("active", receipt["state"])
        validate_json_schema(
            receipt,
            SCHEMA_ROOT / "wake-daemon-consent-receipt.schema.json",
        )
        self.assertEqual(receipt, ledger.require_active(coordinate))

        revoked = ledger.revoke(coordinate, idempotency_key="revoke-1")
        self.assertEqual("revoked", revoked["state"])
        with self.assertRaisesRegex(ProtocolRefusal, "consent_absent"):
            ledger.require_active(coordinate)

    def test_consent_rejects_invalid_bounds_and_changed_replay(self) -> None:
        from floati.wake_daemon_contract import DaemonConsentLedger

        coordinate = self.coordinate()
        ledger = DaemonConsentLedger(self.root)
        cases = (
            {"min_poll_seconds": True},
            {"min_poll_seconds": 0},
            {"max_poll_seconds": 0},
            {"max_backoff_seconds": 29},
            {"activation_epoch": 0},
            {"adapter_digest": "wrong"},
        )
        base = {
            "adapter_version": "1",
            "adapter_digest": "a" * 64,
            "min_poll_seconds": 1,
            "max_poll_seconds": 30,
            "max_backoff_seconds": 120,
            "activation_epoch": 1,
            "idempotency_key": "bounds",
        }
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ProtocolRefusal):
                    ledger.consent(coordinate, **dict(base, **changes))

        ledger.consent(coordinate, **base)
        with self.assertRaisesRegex(ProtocolRefusal, "idempotency"):
            ledger.consent(coordinate, **dict(base, max_poll_seconds=31))
        with self.assertRaisesRegex(ProtocolRefusal, "epoch"):
            ledger.consent(
                coordinate,
                **dict(base, idempotency_key="bounds-next"),
            )

        same_path_root = FloatiRoot.open_direct_home(self.root.path)
        foreign_coordinate = self.coordinate()
        with self.assertRaisesRegex(ProtocolRefusal, "another root"):
            DaemonConsentLedger(same_path_root).consent(
                foreign_coordinate,
                **dict(base, activation_epoch=2, idempotency_key="foreign-root"),
            )

    def test_grok_build_consent_receipt_is_closed(self) -> None:
        from floati.wake_daemon_contract import DaemonConsentLedger

        try:
            receipt = DaemonConsentLedger(self.root).consent(
                self.coordinate("grok-build"),
                adapter_version="1",
                adapter_digest="a" * 64,
                min_poll_seconds=1,
                max_poll_seconds=30,
                max_backoff_seconds=120,
                activation_epoch=1,
                idempotency_key=public_ids.compose(public_ids.verifier(), '-consent-1'),
            )
        except ProtocolRefusal as exc:
            self.fail(f"grok-build consent was refused: {exc}")

        validate_json_schema(
            receipt,
            SCHEMA_V1_ROOT / "wake-daemon-consent-receipt.schema.json",
        )
        self.assertEqual(1, receipt["schema_version"])
        self.assertEqual("grok-build", receipt["harness"])

    def test_coordinate_requires_active_node_and_supported_harness(self) -> None:
        from floati.wake_daemon_contract import DaemonCoordinate

        for node, harness in (("missing", "cursor"), (public_ids.builder('a'), "claude")):
            with self.subTest(node=node, harness=harness):
                with self.assertRaises(ProtocolRefusal):
                    DaemonCoordinate(self.root, node, harness)

    def test_coordinate_accepts_grok_build_harness(self) -> None:
        try:
            coordinate = self.coordinate("grok-build")
        except ProtocolRefusal as exc:
            self.fail(f"grok-build coordinate was refused: {exc}")

        self.assertEqual("grok-build", coordinate.harness)
        self.assertEqual(public_ids.builder('a'), coordinate.node_id)

    def test_binding_is_exact_digest_bound_and_closed(self) -> None:
        from floati.wake_daemon_contract import AdapterBindingStore

        coordinate = self.coordinate()
        store = AdapterBindingStore(self.root)
        binding = store.write(
            coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest="b" * 64,
            binding_epoch=1,
        )

        self.assertEqual(binding, store.read(coordinate))
        validate_json_schema(
            binding,
            SCHEMA_ROOT / "wake-daemon-adapter-record.schema.json",
        )
        self.assertEqual("cursor-session-1", binding["session_id"])
        self.assertEqual("fleet-alpha", binding["tenant_id"])
        self.assertEqual(0o600, store.path(coordinate).stat().st_mode & 0o777)

        tampered = dict(binding, tenant_id="foreign")
        store.path(coordinate).write_text(json.dumps(tampered) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(IntegrityFailure, "binding identity"):
            store.read(coordinate)

    def test_binding_record_with_an_unknown_key_is_invalid(self) -> None:
        """WD-R5b-F1 keeps the binding store closed to unknown fields."""
        from floati.wake_daemon_contract import AdapterBindingStore

        coordinate = self.coordinate()
        store = AdapterBindingStore(self.root)
        binding = store.write(
            coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest="b" * 64,
            binding_epoch=1,
        )
        widened = dict(binding, resume_note="accidental widening")
        store.path(coordinate).write_text(
            json.dumps(widened) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(IntegrityFailure, "binding shape is invalid"):
            store.read(coordinate)

    def test_binding_resume_state_is_optional_and_closed(self) -> None:
        from floati.wake_daemon_contract import AdapterBindingStore

        coordinate = self.coordinate()
        store = AdapterBindingStore(self.root)
        legacy = store.write(
            coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest="b" * 64,
            binding_epoch=1,
        )
        self.assertNotIn("resume_state", legacy)
        validate_json_schema(
            legacy,
            SCHEMA_ROOT / "wake-daemon-adapter-record.schema.json",
        )

        recorded = store.write(
            coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest="b" * 64,
            binding_epoch=1,
            resume_state="resume_unproven",
        )
        self.assertEqual("resume_unproven", recorded["resume_state"])
        validate_json_schema(
            recorded,
            SCHEMA_ROOT / "wake-daemon-adapter-record.schema.json",
        )

        with self.assertRaisesRegex(ProtocolRefusal, "resume_state"):
            store.write(
                coordinate,
                session_id="cursor-session-1",
                workspace=self.workspace,
                executable=self.executable,
                adapter_version="1",
                adapter_digest="b" * 64,
                binding_epoch=1,
                resume_state="resume_imagined",
            )

        tampered = dict(recorded, extra="nope")
        store.path(coordinate).write_text(json.dumps(tampered) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(IntegrityFailure, "binding shape"):
            store.read(coordinate)

    def test_binding_refuses_digest_drift_and_symlinked_executable(self) -> None:
        from floati.wake_daemon_contract import AdapterBindingStore

        coordinate = self.coordinate()
        store = AdapterBindingStore(self.root)
        store.write(
            coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest="b" * 64,
            binding_epoch=1,
        )
        self.executable.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ProtocolRefusal, "digest"):
            store.read(coordinate)

        other = self.base / "other-agent"
        other.symlink_to(self.executable)
        with self.assertRaises(ProtocolRefusal):
            store.write(
                coordinate,
                session_id="cursor-session-2",
                workspace=self.workspace,
                executable=other,
                adapter_version="1",
                adapter_digest="b" * 64,
                binding_epoch=2,
            )

    def test_grok_build_binding_record_is_closed(self) -> None:
        from floati.wake_daemon_contract import AdapterBindingStore

        binding = AdapterBindingStore(self.root).write(
            self.coordinate("grok-build"),
            session_id=public_ids.compose(public_ids.verifier(), '-session-1'),
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest="b" * 64,
            binding_epoch=1,
        )

        validate_json_schema(
            binding,
            SCHEMA_V1_ROOT / "wake-daemon-adapter-record.schema.json",
        )
        self.assertEqual(1, binding["schema_version"])
        self.assertEqual("grok-build", binding["harness"])

    def test_lifecycle_receipt_is_closed(self) -> None:
        from floati.wake_daemon_contract import DaemonLifecycleLedger

        coordinate = self.coordinate()
        receipt = DaemonLifecycleLedger(self.root).record(
            coordinate,
            daemon_instance_id="daemon-1",
            activation_epoch=1,
            event="installed",
            state="installed",
            reason_code=None,
            adapter_digest="b" * 64,
            plist_digest="c" * 64,
            session_digest="d" * 64,
            predecessor_receipt_id=None,
            idempotency_key="install-1",
        )
        validate_json_schema(
            receipt,
            SCHEMA_ROOT / "wake-daemon-lifecycle-receipt.schema.json",
        )

        pause_unknown = DaemonLifecycleLedger(self.root).record(
            coordinate,
            daemon_instance_id="daemon-1",
            activation_epoch=1,
            event="pause_unknown",
            state="pause_unknown",
            reason_code="wake_control_state_invalid",
            adapter_digest="b" * 64,
            plist_digest=None,
            session_digest="d" * 64,
            predecessor_receipt_id=receipt["id"],
            idempotency_key="pause-unknown-1",
        )
        validate_json_schema(
            pause_unknown,
            SCHEMA_ROOT / "wake-daemon-lifecycle-receipt.schema.json",
        )

    def test_grok_build_lifecycle_receipt_is_closed(self) -> None:
        from floati.wake_daemon_contract import DaemonLifecycleLedger

        receipt = DaemonLifecycleLedger(self.root).record(
            self.coordinate("grok-build"),
            daemon_instance_id=public_ids.compose(public_ids.verifier(), '-daemon-1'),
            activation_epoch=1,
            event="installed",
            state="installed",
            reason_code=None,
            adapter_digest="b" * 64,
            plist_digest="c" * 64,
            session_digest="d" * 64,
            predecessor_receipt_id=None,
            idempotency_key=public_ids.compose(public_ids.verifier(), '-install-1'),
        )

        validate_json_schema(
            receipt,
            SCHEMA_V1_ROOT / "wake-daemon-lifecycle-receipt.schema.json",
        )
        self.assertEqual(1, receipt["schema_version"])
        self.assertEqual("grok-build", receipt["harness"])

    def test_wake_daemon_ledger_reads_legacy_v0_and_grok_v1_rows_together(self) -> None:
        from floati.wake_daemon_contract import DaemonConsentLedger

        ledger = DaemonConsentLedger(self.root)
        cursor = ledger.consent(
            self.coordinate("cursor"),
            adapter_version="1",
            adapter_digest="a" * 64,
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key="cursor-v0",
        )
        grok = ledger.consent(
            self.coordinate("grok-build"),
            adapter_version="1",
            adapter_digest="b" * 64,
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key=public_ids.compose(public_ids.verifier(), '-v1'),
        )

        self.assertEqual(0, cursor["schema_version"])
        self.assertEqual(1, grok["schema_version"])
        self.assertEqual(cursor, ledger.require_active(self.coordinate("cursor")))
        self.assertEqual(grok, ledger.require_active(self.coordinate("grok-build")))


if __name__ == "__main__":
    unittest.main()
