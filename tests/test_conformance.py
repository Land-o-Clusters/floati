from __future__ import annotations

import contextlib
import inspect
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import conformance


class ConformanceRunnerTests(unittest.TestCase):
    def run_adapter(self, factory: str, *, root: Path = None, timeout: float = 10, call_timeout: float = 2) -> subprocess.CompletedProcess:
        selected_root = root or Path(self.temp.name)
        return subprocess.run(
            (
                sys.executable,
                "-m",
                "floati.conformance",
                "--adapter",
                f"tests.fixture_adapters:{factory}",
                "--root",
                str(selected_root),
                "--tenant",
                "alpha",
                "--call-timeout",
                str(call_timeout),
            ),
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_conformant_adapter_returns_artifact_zero(self) -> None:
        result = self.run_adapter("conformant")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual("conformant", payload["status"])
        self.assertGreaterEqual(payload["cases"], 16)

    def test_same_root_can_run_repeatedly_without_fixture_collision(self) -> None:
        first = self.run_adapter("conformant")
        second = self.run_adapter("conformant")
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)

    def test_isolated_adapter_process_announces_floati_identity(self) -> None:
        """Catches a shipped conformance child retaining the retired process label."""
        from floati.root import FloatiRoot

        root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "runtime-identity", create=True,
        )
        adapter = conformance._IsolatedAdapter(
            "tests.fixture_adapters:conformant", root, 1,
        )
        self.addCleanup(adapter.close)

        self.assertEqual("floati-conformance-adapter", adapter._process.name)

    def test_parser_errors_use_documented_configuration_exit(self) -> None:
        result = subprocess.run(
            (sys.executable, "-m", "floati.conformance", "--adapter", "missing"),
            cwd=Path.cwd(), text=True, capture_output=True, check=False,
        )
        self.assertEqual(20, result.returncode)
        self.assertEqual("configuration_refused", json.loads(result.stderr)["status"])

    def test_behavior_failure_has_its_own_exit(self) -> None:
        result = self.run_adapter("behavioral_failure")
        self.assertEqual(10, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("conformance_failed", json.loads(result.stderr)["status"])

    def test_adapter_that_fabricates_messages_without_events_ledger_fails(self) -> None:
        """Catches adapter-only message evidence that never durably appends events.jsonl."""
        result = self.run_adapter("no_event")
        self.assertEqual(10, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("conformance_failed", json.loads(result.stderr)["status"])

    def test_configuration_refusal_has_its_own_exit(self) -> None:
        result = self.run_adapter("conformant", root=Path("relative-root"))
        self.assertEqual(20, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("configuration_refused", json.loads(result.stderr)["status"])

    def test_dead_silent_absent_and_malformed_are_distinct(self) -> None:
        expected = {
            "dead": (30, "adapter_died"),
            "intentional_silence": (31, "intentional_silence"),
            "no_result": (32, "no_result"),
            "malformed": (33, "malformed_evidence"),
        }
        for factory, (exit_code, status) in expected.items():
            with self.subTest(factory=factory):
                result = self.run_adapter(factory)
                self.assertEqual(exit_code, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual(status, json.loads(result.stderr)["status"])

    def test_hang_and_process_death_are_bounded_adapter_death(self) -> None:
        for factory in ("hang", "process_death"):
            with self.subTest(factory=factory):
                result = self.run_adapter(factory, timeout=3, call_timeout=0.1)
                self.assertEqual(30, result.returncode, result.stderr)
                self.assertEqual("adapter_died", json.loads(result.stderr)["status"])


class LiveRootSmokeTests(unittest.TestCase):
    def test_live_root_smoke_accepts_no_caller_path(self) -> None:
        self.assertEqual(0, len(inspect.signature(conformance.run_live_root_smoke).parameters))

    def test_live_root_smoke_command_returns_conformant_artifact(self) -> None:
        result = subprocess.run(
            (sys.executable, "-m", "floati.conformance", "--live-root-smoke"),
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual("conformant", json.loads(result.stdout)["status"])

    def test_live_root_smoke_round_trip_denial_and_cleanup(self) -> None:
        real_temporary_directory = tempfile.TemporaryDirectory
        observations = []
        denial_snapshots = []

        def root_snapshot(home: Path) -> tuple:
            snapshot = []
            for entry in sorted(home.rglob("*"), key=lambda item: item.relative_to(home).as_posix()):
                relative = entry.relative_to(home).as_posix()
                if entry.is_symlink():
                    snapshot.append((relative, "symlink", entry.readlink().as_posix()))
                elif entry.is_file():
                    snapshot.append((relative, "regular", entry.read_bytes()))
                elif entry.is_dir():
                    snapshot.append((relative, "directory", None))
                else:
                    snapshot.append((relative, "other", None))
            return tuple(snapshot)

        class ObservedEventLog(conformance.EventLog):
            def send(self, sender: str, recipient: str, *args: object, **kwargs: object) -> dict:
                if (sender, recipient) not in {
                    ("stranger", "smoke-recipient"),
                    ("smoke-sender", "stranger"),
                    ("smoke-retired", "smoke-recipient"),
                }:
                    return super().send(sender, recipient, *args, **kwargs)
                before = root_snapshot(self.root.tenant_home)
                try:
                    return super().send(sender, recipient, *args, **kwargs)
                except conformance.ProtocolRefusal as exc:
                    denial_snapshots.append(
                        (exc.code, exc.detail, before, root_snapshot(self.root.tenant_home))
                    )
                    raise

        class ObservedTemporaryDirectory(real_temporary_directory):
            def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
                directory = Path(self.name)
                home = directory / "smoke-tenant"
                observations.append(
                    {
                        "directory": directory,
                        "events": self._records(home / "events.jsonl"),
                        "deliveries": self._records(home / "receipts/deliveries/smoke-recipient.jsonl"),
                        "acks": self._records(home / "receipts/acks/smoke-recipient.jsonl"),
                        "denials": self._records(home / "receipts/denials.jsonl"),
                    }
                )
                super().__exit__(exc_type, exc_value, traceback)

            @staticmethod
            def _records(path: Path) -> list:
                if not path.exists():
                    return []
                return [json.loads(line) for line in path.read_text().splitlines()]

        output = io.StringIO()
        with mock.patch.object(conformance, "EventLog", ObservedEventLog):
            with mock.patch.object(conformance, "TemporaryDirectory", ObservedTemporaryDirectory):
                with contextlib.redirect_stdout(output):
                    exit_code = conformance.run_live_root_smoke()

        self.assertEqual(0, exit_code)
        self.assertEqual("conformant", json.loads(output.getvalue())["status"])
        self.assertEqual(1, len(observations))
        observed = observations[0]
        self.assertFalse(observed["directory"].exists())
        self.assertEqual(1, len(observed["events"]))
        message = observed["events"][0]
        self.assertEqual("message_envelope", message["kind"])
        self.assertEqual("smoke-sender", message["sender"])
        self.assertEqual("smoke-recipient", message["recipient"])
        self.assertEqual("floati", message["repo"])
        self.assertEqual("a" * 40, message["sha"])
        self.assertEqual("docs/evidence/live-root-smoke.md", message["doc"])
        self.assertEqual([message["id"]], observed["deliveries"][0]["item_ids"])
        self.assertEqual([message["id"]], observed["acks"][0]["item_ids"])
        self.assertEqual(1, len(observed["deliveries"]))
        self.assertEqual(1, len(observed["acks"]))
        self.assertEqual([], observed["denials"])
        self.assertEqual(
            [
                (
                    "unknown_sender",
                    "message refused: unknown sender 'stranger'; registered active nodes: smoke-extra, smoke-recipient, smoke-sender",
                ),
                (
                    "recipient_unregistered",
                    "message refused: recipient 'stranger' is not registered; registered nodes: smoke-extra, smoke-recipient, smoke-sender",
                ),
                (
                    "unknown_sender",
                    "message refused: unknown sender 'smoke-retired'; registered active nodes: smoke-extra, smoke-recipient, smoke-sender",
                ),
            ],
            [(code, detail) for code, detail, _, _ in denial_snapshots],
        )
        self.assertEqual(3, len(denial_snapshots))
        for _, _, before, after in denial_snapshots:
            self.assertEqual(before, after)

    def test_live_root_smoke_mode_refuses_adapter_root_and_tenant_options(self) -> None:
        conflicting_options = (
            ("--adapter", "tests.fixture_adapters:conformant"),
            ("--root", str(Path(self._testMethodName).resolve())),
            ("--tenant", "alpha"),
        )
        for option in conflicting_options:
            with self.subTest(option=option[0]):
                result = subprocess.run(
                    (sys.executable, "-m", "floati.conformance", "--live-root-smoke", *option),
                    cwd=Path.cwd(),
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(20, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual("configuration_refused", json.loads(result.stderr)["status"])

    def test_live_root_smoke_mode_refuses_explicit_call_timeout(self) -> None:
        result = subprocess.run(
            (
                sys.executable,
                "-m",
                "floati.conformance",
                "--live-root-smoke",
                "--call-timeout",
                "2",
            ),
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(20, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual(1, len(result.stderr.splitlines()))
        artifact = json.loads(result.stderr)
        self.assertEqual("configuration_refused", artifact["status"])
        self.assertEqual("arguments_invalid", artifact["detail"])

    def test_adapter_mode_preserves_default_and_explicit_call_timeout(self) -> None:
        cases = ((None, 2.0), (0.25, 0.25))
        with tempfile.TemporaryDirectory() as temporary:
            for supplied, expected in cases:
                with self.subTest(supplied=supplied):
                    arguments = [
                        "--adapter",
                        "tests.fixture_adapters:conformant",
                        "--root",
                        temporary,
                        "--tenant",
                        "alpha",
                    ]
                    if supplied is not None:
                        arguments.extend(("--call-timeout", str(supplied)))
                    with mock.patch.object(conformance, "_IsolatedAdapter") as isolated:
                        with mock.patch.object(conformance, "run", return_value=0):
                            self.assertEqual(0, conformance.main(arguments))
                    self.assertEqual(expected, isolated.call_args.args[2])


class ACPFixtureConformanceTests(unittest.TestCase):
    def test_acp_fixture_mode_returns_conformant_artifact_and_honest_probe(self) -> None:
        result = subprocess.run(
            (sys.executable, "-m", "floati.conformance", "--acp-fixture"),
            cwd=Path.cwd(), text=True, capture_output=True, check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        artifact = json.loads(result.stdout)
        self.assertEqual("conformant", artifact["status"])
        self.assertEqual(4, artifact["cases"])
        self.assertIn(
            artifact["harness_status"],
            {"reference_harness_absent", "reference_harness_present_unlaunched"},
        )

    def test_acp_fixture_mode_refuses_unrelated_configuration(self) -> None:
        result = subprocess.run(
            (
                sys.executable, "-m", "floati.conformance", "--acp-fixture",
                "--root", str(Path.cwd()),
            ),
            cwd=Path.cwd(), text=True, capture_output=True, check=False,
        )

        self.assertEqual(20, result.returncode)
        self.assertEqual("configuration_refused", json.loads(result.stderr)["status"])


if __name__ == "__main__":
    unittest.main()
