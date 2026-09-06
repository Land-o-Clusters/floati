from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.cli import main
from floati.cursor import SparseCursor
from floati.doctor import Doctor
from floati.events import EventLog
from floati.framing import encode_frame
from floati.graph import HarborGraph
from floati.records import READER_VERSION
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import (
    AdapterBinding,
    WakeAdapterResult,
    adapter_contract_digest,
)
from floati.wake_daemon_contract import (
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
)
from tests.temp_roots import REAL_TEMP_ROOT


FUTURE_KIND = "future_registry_kind"
FUTURE_ID = "future-registry-01a073ab8d0e70008000000000000000"


class _Adapter:
    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.store = AdapterBindingStore(root)
        self.coordinate = coordinate

    def exact_binding(self) -> AdapterBinding:
        return AdapterBinding.from_record(self.store.read(self.coordinate))

    def observe_session(self, binding: AdapterBinding) -> str:
        return "unknown"

    def request_wake(
        self,
        binding: AdapterBinding,
        reason: str,
        deadline_seconds: int,
        envelopes: object = None,
    ) -> WakeAdapterResult:
        return WakeAdapterResult("woke", None, 0, "e" * 64)


class Skew1UnknownRegistryKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register("sender", "architect")
        registry.register(public_ids.builder("a"), "worker")
        self.log = EventLog(self.root)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.coordinate = DaemonCoordinate(
            self.root, public_ids.builder("a"), "cursor"
        )
        self.adapter = _Adapter(self.root, self.coordinate)

    def append_unknown_registry_kind(self) -> None:
        path = self.root.resolve_relative("registry/entries.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(
                encode_frame(
                    {
                        "schema_version": 2,
                        "id": FUTURE_ID,
                        "tenant_id": self.root.tenant_id,
                        "timestamp": "2026-09-05T22:23:37.000Z",
                        "kind": FUTURE_KIND,
                        "payload": {"newer": True},
                    }
                )
            )

    def skip_receipt(self) -> dict[str, object]:
        receipt = Registry(self.root).skip_receipt()
        self.assertIsInstance(receipt, dict)
        assert isinstance(receipt, dict)
        return receipt

    def assert_skip_receipt(self, receipt: dict[str, object]) -> None:
        self.assertEqual(FUTURE_KIND, receipt["kind"])
        self.assertEqual(READER_VERSION, receipt["reader_schema_version"])
        self.assertEqual(FUTURE_ID, receipt["first_id"])
        self.assertEqual("registry/entries.jsonl", receipt["ledger"])

    def bind_and_consent(self) -> None:
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
            idempotency_key="skew-1-consent",
        )

    def daemon(self):
        from floati.wake_daemon import WakeDaemon

        return WakeDaemon(self.coordinate, self.adapter)

    def test_daemon_cycle_skips_unknown_registry_kind(self) -> None:
        """RED today: run_cycle raises record_kind_invalid (ARCH-1 daemon 33)."""

        self.bind_and_consent()
        self.append_unknown_registry_kind()

        result = self.daemon().run_cycle(100.0)

        self.assertIn(result["state"], {"idle", "woke", "backpressure"})
        self.assert_skip_receipt(self.skip_receipt())

    def test_registry_projection_skips_unknown_kind(self) -> None:
        """RED today: HarborGraph dies on the unknown registry row."""

        self.append_unknown_registry_kind()

        artifact = HarborGraph(self.root).artifact()
        nodes = {str(row["id"]) for row in artifact["nodes"]}
        self.assertEqual({"sender", public_ids.builder("a")}, nodes)
        self.assertEqual(
            tuple(sorted(("sender", public_ids.builder("a")))),
            Registry(self.root).active_node_ids(),
        )
        self.assert_skip_receipt(self.skip_receipt())

    def test_ack_skips_unknown_registry_kind(self) -> None:
        """RED today: ack exits 33 malformed_evidence (ACK-33)."""

        sent = self.log.send(
            "sender",
            public_ids.builder("a"),
            "floati",
            "a" * 40,
            "docs/rulings/2026-08-29-version-skew-guards.md",
            "known mail survives registry skew",
            idempotency_key="skew-1-known-mail",
        )
        messages, _delivery = self.log.present(public_ids.builder("a"))
        self.assertEqual([sent["message"]["id"]], [row["id"] for row in messages])
        self.append_unknown_registry_kind()
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            status = main(
                [
                    "ack",
                    "--root",
                    str(self.root.path),
                    "--as",
                    public_ids.builder("a"),
                    "--id",
                    str(sent["message"]["id"]),
                    "--session",
                    "session-skew-1",
                ]
            )

        self.assertEqual(0, status)
        artifact = json.loads(stdout.getvalue())
        self.assertEqual("ok", artifact["status"])
        self.assertNotEqual("malformed_evidence", artifact["status"])
        self.assertEqual(
            [sent["message"]["id"]],
            sorted(SparseCursor(self.root).acked_ids(public_ids.builder("a"))),
        )
        self.assert_skip_receipt(self.skip_receipt())

    def test_doctor_lists_reader_older_than_ledger_newest_kind(self) -> None:
        """RED today: doctor exits 33 instead of naming the older reader."""

        self.append_unknown_registry_kind()

        artifact, _return_code = Doctor(Path.cwd(), self.root.path, ref="HEAD").artifact()

        self.assertFalse(
            any(row["code"] == "delivery_health_unavailable" for row in artifact["findings"])
        )
        self.assertFalse(
            any("record_kind_invalid" in str(row.get("detail", "")) for row in artifact["findings"])
        )
        finding = next(row for row in artifact["findings"] if row["code"] == "version_skew")
        skew = finding["vocabulary_skew"]
        self.assertEqual(READER_VERSION, skew["reader_version"])
        self.assertLess(int(str(skew["reader_version"])), int(str(skew["ledger_version"])))
        self.assertIn(FUTURE_KIND, skew["unknown_kinds"])
        older = finding["older_readers"]
        self.assertEqual(
            [{"reader_schema_version": READER_VERSION, "ledger_newest_kind": FUTURE_KIND}],
            older,
        )
        self.assert_skip_receipt(self.skip_receipt())


if __name__ == "__main__":
    unittest.main()
