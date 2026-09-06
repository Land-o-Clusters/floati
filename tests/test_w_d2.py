from __future__ import annotations

from tests.test_cli import LAUNCHER

from floati import fixture_ids as public_ids

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_control import WakeController
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).parents[1]


class WakeStatusBreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "demo-fleet", create=True)
        Registry(self.root).register(public_ids.builder("a"), "Codex")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def _bind(self, harness: str, session_id: str) -> None:
        from floati.wake_daemon_adapters import adapter_contract_digest
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate

        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), harness)
        AdapterBindingStore(self.root).write(
            coordinate,
            session_id=session_id,
            workspace=self.workspace,
            executable=REPOSITORY_ROOT / "scripts/floati",
            adapter_version="1",
            adapter_digest=adapter_contract_digest(harness),
            binding_epoch=1,
        )

    def _write_runtime(
        self,
        harness: str,
        session_id: str,
        *,
        circuit_state: str,
        consecutive_refusals: int,
        last_reason_code: str,
        current_backoff: int = 1440,
    ) -> Path:
        from floati.wake_daemon_contract import DaemonCoordinate

        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), harness)
        payload = {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": coordinate.node_id,
            "harness": harness,
            "coordinate_digest": coordinate.digest,
            "daemon_instance_id": "daemon-test",
            "activation_epoch": 1,
            "cycle_index": 514,
            "current_wake_key": None,
            "consecutive_refusals": consecutive_refusals,
            "circuit_state": circuit_state,
            "next_poll_at": 0,
            "current_backoff": current_backoff,
            "wake_timestamps": [],
            "session_digest": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
            "last_state": "backpressure",
            "last_reason_code": last_reason_code,
            "last_lifecycle_receipt_id": "receipt-test",
        }
        path = self.root.resolve_relative(
            Path("state/wake-daemon/runtime") / f"{coordinate.digest}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def _write_notice(
        self,
        harness: str,
        session_id: str,
        *,
        consecutive_refusals: int,
        last_reason_code: str,
    ) -> None:
        from floati.wake_daemon import WAKE_BREAKER_REMEDY
        from floati.wake_daemon_contract import DaemonCoordinate

        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), harness)
        notice = {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": coordinate.node_id,
            "harness": harness,
            "coordinate_digest": coordinate.digest,
            "session_id": session_id,
            "session_digest": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
            "consecutive_refusals": consecutive_refusals,
            "current_backoff": 1440,
            "last_reason_code": last_reason_code,
            "cycle_index": 396,
            "remedy": WAKE_BREAKER_REMEDY,
        }
        path = self.root.resolve_relative(
            Path("state/wake-daemon/notices") / f"{coordinate.digest}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(notice) + "\n", encoding="utf-8")

    def test_status_reports_open_breaker_from_runtime(self) -> None:
        """RED: breaker state is not on wake status; only a refusal remedy names it."""
        from floati.wake_daemon import _BREAKER_THRESHOLD

        session_id = "worker-cursor-session"
        self._bind("cursor", session_id)
        self._write_runtime(
            "cursor",
            session_id,
            circuit_state="open",
            consecutive_refusals=369,
            last_reason_code="wake_daemon_circuit_open",
        )

        status = WakeController(self.root).status(
            public_ids.builder("a"), "status-session"
        )

        self.assertIn("breaker", status)
        breaker = status["breaker"]
        self.assertEqual("runtime", breaker["source"])
        self.assertEqual(_BREAKER_THRESHOLD, breaker["threshold"])
        self.assertEqual(1, len(breaker["coordinates"]))
        coordinate = breaker["coordinates"][0]
        self.assertEqual("cursor", coordinate["harness"])
        self.assertEqual("open", coordinate["state"])
        self.assertEqual(369, coordinate["consecutive_refusals"])
        self.assertEqual("wake_daemon_circuit_open", coordinate["last_trip_reason"])
        self.assertIn("open", status["display"])
        self.assertIn("wake_daemon_circuit_open", status["display"])

    def test_status_surfaces_runtime_not_the_lagging_notice(self) -> None:
        """NOTICE-LAG-1: the one-shot notice freezes the trip count."""
        session_id = "worker-cursor-session"
        self._bind("cursor", session_id)
        self._write_runtime(
            "cursor",
            session_id,
            circuit_state="open",
            consecutive_refusals=369,
            last_reason_code="wake_daemon_circuit_open",
        )
        self._write_notice(
            "cursor",
            session_id,
            consecutive_refusals=251,
            last_reason_code="wake_daemon_circuit_open",
        )

        status = WakeController(self.root).status(
            public_ids.builder("a"), "status-session"
        )

        self.assertIn("breaker", status)
        coordinate = status["breaker"]["coordinates"][0]
        self.assertEqual(369, coordinate["consecutive_refusals"])
        self.assertNotEqual(251, coordinate["consecutive_refusals"])
        self.assertEqual("runtime", status["breaker"]["source"])

    def test_status_names_unbound_breaker_as_typed_absence(self) -> None:
        from floati.wake_daemon import _BREAKER_THRESHOLD

        status = WakeController(self.root).status(
            public_ids.builder("a"), "status-session"
        )

        self.assertIn("breaker", status)
        breaker = status["breaker"]
        self.assertEqual("runtime", breaker["source"])
        self.assertEqual(_BREAKER_THRESHOLD, breaker["threshold"])
        self.assertEqual([], breaker["coordinates"])
        self.assertIn("unbound", status["display"])

    def test_status_does_not_reset_an_open_breaker(self) -> None:
        session_id = "worker-cursor-session"
        self._bind("cursor", session_id)
        runtime_path = self._write_runtime(
            "cursor",
            session_id,
            circuit_state="open",
            consecutive_refusals=19,
            last_reason_code="wake_daemon_adapter_timeout",
        )
        before = runtime_path.read_bytes()

        WakeController(self.root).status(public_ids.builder("a"), "status-session")

        self.assertEqual(before, runtime_path.read_bytes())
        self.assertEqual(
            "open",
            json.loads(runtime_path.read_text(encoding="utf-8"))["circuit_state"],
        )

    def test_cli_status_artifact_carries_the_breaker(self) -> None:
        session_id = "worker-cursor-session"
        self._bind("cursor", session_id)
        self._write_runtime(
            "cursor",
            session_id,
            circuit_state="closed",
            consecutive_refusals=0,
            last_reason_code="idle",
        )
        completed = subprocess.run(
            [
                str(LAUNCHER),
                "wake",
                "status",
                "--root",
                str(self.root.path),
                "--as",
                public_ids.builder("a"),
                "--session",
                "cli-session",
            ],
            cwd=REPOSITORY_ROOT,
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        evidence = json.loads(completed.stdout)["evidence"]
        self.assertIn("breaker", evidence)
        self.assertEqual("closed", evidence["breaker"]["coordinates"][0]["state"])
        self.assertIsNone(
            evidence.get("reset"),
            "wake status must not grow a reset path",
        )

    def _runtime_path(self, harness: str) -> Path:
        from floati.wake_daemon_contract import DaemonCoordinate

        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), harness)
        return self.root.resolve_relative(
            Path("state/wake-daemon/runtime") / f"{coordinate.digest}.json"
        )

    def test_underivable_runtime_missing_names_its_reason(self) -> None:
        self._bind("cursor", "worker-cursor-session")

        status = WakeController(self.root).status(
            public_ids.builder("a"), "status-session"
        )

        coordinate = status["breaker"]["coordinates"][0]
        self.assertEqual("underivable", coordinate["state"])
        self.assertIn("reason", coordinate)
        self.assertEqual("runtime_missing", coordinate["reason"])

    def test_underivable_runtime_symlink_names_its_reason(self) -> None:
        self._bind("cursor", "worker-cursor-session")
        path = self._runtime_path("cursor")
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.base / "elsewhere.json"
        target.write_text("{}\n", encoding="utf-8")
        path.symlink_to(target)

        status = WakeController(self.root).status(
            public_ids.builder("a"), "status-session"
        )

        coordinate = status["breaker"]["coordinates"][0]
        self.assertEqual("underivable", coordinate["state"])
        self.assertIn("reason", coordinate)
        self.assertEqual("runtime_symlink", coordinate["reason"])

    def test_underivable_runtime_malformed_names_its_reason(self) -> None:
        self._bind("cursor", "worker-cursor-session")
        path = self._runtime_path("cursor")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        status = WakeController(self.root).status(
            public_ids.builder("a"), "status-session"
        )

        coordinate = status["breaker"]["coordinates"][0]
        self.assertEqual("underivable", coordinate["state"])
        self.assertIn("reason", coordinate)
        self.assertEqual("runtime_malformed", coordinate["reason"])

    def test_wake_status_copy_names_underivable_and_its_three_reasons(self) -> None:
        """Catches F2: breaker copy names open/closed but not underivable reasons."""
        from floati.helptext import HELP

        surfaces = (
            HELP["wake status"],
            (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8"),
            (REPOSITORY_ROOT / "docs" / "AGENT-OPERATIONS.md").read_text(encoding="utf-8"),
            (REPOSITORY_ROOT / "docs" / "COPY-LEDGER.md").read_text(encoding="utf-8"),
        )
        for index, text in enumerate(surfaces):
            with self.subTest(surface=index):
                lowered = text.lower()
                self.assertIn("underivable", lowered)
                self.assertIn("runtime_missing", lowered)
                self.assertIn("runtime_symlink", lowered)
                self.assertIn("runtime_malformed", lowered)


if __name__ == "__main__":
    unittest.main()
