"""AD-1 herdr WORK-HOST contract — RED until HerdrWorkerHost exists.

This file is the C-row RED candidate for board row 10. It does not promote
``HerdrClientAdapter`` into a WorkerAdapter (08-14 ruling approach B refused).
Work-column close is host-composition: host_kind=herdr plus a closed inner
adapter identity in {codex, claude, pi}. A standalone worker name ``herdr``
never exists.

The live executable for this row is OPERATOR-DECLARED in
``tests/harness_declarations.json`` and is never searched for. Where it is not
declared, or the declared path is not one canonical executable on this host,
the live test still runs and asserts the typed absence instead. It never skips.
"""

from __future__ import annotations

import importlib
import subprocess
import unittest

from floati.adapters.herdr import HERDR_PROTOCOL_VERSION, HerdrClientAdapter
from tests import harness_declaration
from tests.test_roster_adapters import ROSTER


class ObservationClientFenceTests(unittest.TestCase):
    """GREEN fences: the observation source must stay observation-only."""

    def test_client_has_no_worker_spawn_or_drive(self) -> None:
        self.assertFalse(hasattr(HerdrClientAdapter, "spawn"))
        self.assertFalse(hasattr(HerdrClientAdapter, "drive"))
        self.assertEqual(HerdrClientAdapter.name, "herdr-client")

    def test_client_is_not_on_f3_headless_roster(self) -> None:
        names = [name for name, _cls in ROSTER]
        self.assertNotIn("herdr", names)
        self.assertNotIn("herdr-client", names)
        self.assertNotIn("herdr-host", names)


class LiveExecutableTests(unittest.TestCase):
    """C-row: live executable named and launched."""

    def test_named_herdr_binary_reports_version(self) -> None:
        executable = harness_declaration.live_executable_or_typed_absence(
            self, "herdr"
        )
        if executable is None:
            return
        completed = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("herdr", completed.stdout)


class HerdrWorkHostRedTests(unittest.TestCase):
    """RED: the work host is not on main. Failures here are the candidate."""

    def test_herdr_worker_host_module_imports(self) -> None:
        try:
            module = importlib.import_module("floati.adapters.herdr_host")
        except ImportError as exc:
            self.fail(
                "AD-1 RED: floati.adapters.herdr_host is absent. "
                f"Work-column dash is still honest. {exc}"
            )
        self.assertTrue(hasattr(module, "HerdrWorkerHost"))

    def test_host_is_not_a_standalone_herdr_worker_identity(self) -> None:
        try:
            host = importlib.import_module("floati.adapters.herdr_host").HerdrWorkerHost
        except ImportError as exc:
            self.fail(f"AD-1 RED: HerdrWorkerHost missing ({exc})")
        self.assertNotEqual(getattr(host, "name", None), "herdr")
        self.assertEqual(getattr(host, "host_kind", None), "herdr")
        self.assertTrue(hasattr(host, "spawn"))
        self.assertTrue(hasattr(host, "drive"))

    def test_host_inner_adapter_is_the_closed_set(self) -> None:
        try:
            host_cls = importlib.import_module("floati.adapters.herdr_host").HerdrWorkerHost
        except ImportError as exc:
            self.fail(f"AD-1 RED: HerdrWorkerHost missing ({exc})")
        allowed = frozenset({"codex", "claude", "pi"})
        self.assertEqual(frozenset(host_cls.INNER_ADAPTERS), allowed)

    def test_observation_client_pin_is_still_protocol_19(self) -> None:
        self.assertEqual(HERDR_PROTOCOL_VERSION, 19)

    def test_host_pins_measured_protocol_20(self) -> None:
        from floati.adapters.herdr_host import HERDR_HOST_PROTOCOL_VERSION, protocol_pins

        self.assertEqual(HERDR_HOST_PROTOCOL_VERSION, 20)
        observation, host = protocol_pins()
        self.assertEqual(observation["protocol"], 19)
        self.assertEqual(host["protocol"], 20)
        self.assertEqual(host["component"], "herdr-host")
        self.assertNotEqual(
            importlib.import_module("floati.adapters.herdr_host").HerdrWorkerHost.name,
            "herdr",
        )


class _FakeSocket:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout = None
        self.address = None

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def connect(self, address: object) -> None:
        self.address = address

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, _size: int) -> bytes:
        if not self.responses:
            return b""
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


PING20 = (
    b'{"id":"ping-1","result":{"type":"pong","version":"0.8.2",'
    b'"protocol":20,"capabilities":{}}}\n'
)
PING21 = (
    b'{"id":"ping-1","result":{"type":"pong","version":"0.9.0",'
    b'"protocol":21,"capabilities":{}}}\n'
)
PING19 = (
    b'{"id":"ping-1","result":{"type":"pong","version":"0.8.2",'
    b'"protocol":19,"capabilities":{}}}\n'
)
PANE = (
    b'{"id":"pget-1","result":{"type":"pane_info","pane":{"pane_id":"w1:p1",'
    b'"workspace_id":"w1","tab_id":"t1","agent_status":"idle","revision":1}}}\n'
)
PLACED = (
    b'{"id":"start-1","result":{"type":"placed","pane_id":"w1:p1",'
    b'"inner_adapter":"codex","host_kind":"herdr"}}\n'
)


class HerdrWorkHostContractTests(unittest.TestCase):
    def host(self, responses: list[bytes]):
        from floati.adapters.herdr import HerdrReceiptLedger, HerdrTargetRegistration
        from floati.adapters.herdr_host import HerdrHostConfig, HerdrWorkerHost

        channel = _FakeSocket(responses)

        def factory(_family: int, _socket_type: int) -> _FakeSocket:
            return channel

        registration = HerdrTargetRegistration(
            host="127.0.0.1",
            port=3773,
            pane_id="w1:p1",
            registration_id="fixture-herdr-host",
        )
        ledger = HerdrReceiptLedger()
        worker = HerdrWorkerHost(
            HerdrHostConfig(
                host="127.0.0.1",
                port=3773,
                pane_id="w1:p1",
                inner_adapter="codex",
                target_registration=registration,
            ),
            receipt_sink=ledger.append,
            socket_factory=factory,
        )
        return worker, channel, ledger

    def test_spawn_drive_preserve_inner_identity(self) -> None:
        import json

        worker, channel, ledger = self.host([PING20, PANE, PLACED, PING20, PANE])
        worker.arm()
        handle = worker.spawn({"inner_adapter": "codex"}, deadline_seconds=2.0)
        self.assertEqual("codex", handle.inner_adapter)
        self.assertEqual("w1:p1", handle.pane_id)
        self.assertEqual(20, handle.protocol)
        self.assertNotEqual(worker.name, "herdr")
        events = worker.drive(handle, {}, deadline_seconds=2.0)
        self.assertEqual("herdr", events[0]["host_kind"])
        self.assertEqual("codex", events[0]["inner_adapter"])
        methods = [json.loads(raw.decode("utf-8"))["method"] for raw in channel.sent]
        self.assertIn("agent.start", methods)
        for raw in channel.sent:
            frame = json.loads(raw.decode("utf-8"))
            self.assertEqual({"id", "method", "params"}, set(frame))
            self.assertIsInstance(frame["params"], dict)
        ping, pane_get, start = [json.loads(raw.decode("utf-8")) for raw in channel.sent[:3]]
        self.assertEqual(ping["method"], "ping")
        self.assertEqual(ping["params"], {})
        self.assertEqual(pane_get["method"], "pane.get")
        self.assertEqual(pane_get["params"], {"pane_id": "w1:p1"})
        self.assertNotIn("pane_id", pane_get)
        self.assertEqual(start["method"], "agent.start")
        self.assertEqual(start["params"]["pane_id"], "w1:p1")
        self.assertEqual(start["params"]["inner_adapter"], "codex")
        kinds = [record["kind"] for record in ledger.records()]
        self.assertIn("herdr_host_spawn", kinds)
        self.assertIn("herdr_host_drive", kinds)

    def test_protocol_above_pin_is_vs1_skew_naming_both_versions(self) -> None:
        from floati.errors import ProtocolRefusal

        worker, _channel, _ledger = self.host([PING21])
        worker.arm()
        with self.assertRaises(ProtocolRefusal) as caught:
            worker.spawn({"inner_adapter": "codex"}, deadline_seconds=2.0)
        self.assertEqual("version_skew", caught.exception.code)
        self.assertIn("20", caught.exception.detail)
        self.assertIn("21", caught.exception.detail)
        self.assertIsNotNone(caught.exception.remedy)

    def test_protocol_below_pin_is_mismatch_not_skew(self) -> None:
        from floati.errors import ProtocolRefusal

        worker, _channel, _ledger = self.host([PING19])
        worker.arm()
        with self.assertRaises(ProtocolRefusal) as caught:
            worker.spawn({"inner_adapter": "codex"}, deadline_seconds=2.0)
        self.assertEqual("herdr_protocol_mismatch", caught.exception.code)
        self.assertIn("20", caught.exception.detail)
        self.assertIn("19", caught.exception.detail)

    def test_effect_context_is_refused(self) -> None:
        from floati.worker_errors import WorkerAdapterFailure

        worker, _channel, _ledger = self.host([PING20, PANE, PLACED])
        worker.arm()
        with self.assertRaises(WorkerAdapterFailure) as caught:
            worker.spawn(
                {"inner_adapter": "codex", "effect_context": {"present": True}},
                deadline_seconds=2.0,
            )
        self.assertEqual("effect_worker_isolation_unavailable", caught.exception.code)

    def test_doctor_projects_both_pins(self) -> None:
        from floati.adapters.herdr_host import protocol_pins

        observation, host = protocol_pins()
        self.assertEqual(
            (observation["component"], observation["protocol"]),
            ("herdr-client", 19),
        )
        self.assertEqual(
            (host["component"], host["protocol"]),
            ("herdr-host", 20),
        )


class HerdrHostDialectTests(unittest.TestCase):
    """AD-1h-2: captured unix dialect, not the flat TCP fixture dialect."""

    def test_unix_socket_path_uses_af_unix_and_one_connect_per_rpc(self) -> None:
        from floati.adapters.herdr import HerdrReceiptLedger, HerdrTargetRegistration
        from floati.adapters.herdr_host import HerdrHostConfig, HerdrWorkerHost

        channel = _FakeSocket([PING20, PANE, PLACED])
        families: list[int] = []

        def factory(family: int, _socket_type: int) -> _FakeSocket:
            families.append(family)
            return channel

        sock = "\x2ftmp/fixture-herdr.sock"
        worker = HerdrWorkerHost(
            HerdrHostConfig(
                host="127.0.0.1",
                port=3773,
                pane_id="w1:p1",
                inner_adapter="codex",
                socket_path=sock,
                target_registration=HerdrTargetRegistration(
                    host="127.0.0.1",
                    port=3773,
                    pane_id="w1:p1",
                    registration_id="fixture-herdr-host-unix",
                ),
            ),
            receipt_sink=HerdrReceiptLedger().append,
            socket_factory=factory,
        )
        worker.arm()
        worker.spawn({"inner_adapter": "codex"}, deadline_seconds=2.0)
        import socket as socket_module

        self.assertEqual(families, [socket_module.AF_UNIX] * 3)
        self.assertEqual(channel.address, sock)

    def test_missing_params_peer_is_invalid_not_a_pong(self) -> None:
        from floati.adapters.herdr import HerdrReceiptLedger, HerdrTargetRegistration
        from floati.adapters.herdr_host import HerdrHostConfig, HerdrWorkerHost
        from floati.errors import ProtocolRefusal

        missing = (
            b'{"id":"","error":{"code":"invalid_request",'
            b'"message":"invalid request: missing field `params`"}}\n'
        )
        channel = _FakeSocket([missing])

        def factory(_family: int, _socket_type: int) -> _FakeSocket:
            return channel

        worker = HerdrWorkerHost(
            HerdrHostConfig(
                host="127.0.0.1",
                port=3773,
                pane_id="w1:p1",
                inner_adapter="codex",
                target_registration=HerdrTargetRegistration(
                    host="127.0.0.1",
                    port=3773,
                    pane_id="w1:p1",
                    registration_id="fixture-herdr-host",
                ),
            ),
            receipt_sink=HerdrReceiptLedger().append,
            socket_factory=factory,
        )
        worker.arm()
        with self.assertRaises(ProtocolRefusal) as caught:
            worker.spawn({"inner_adapter": "codex"}, deadline_seconds=2.0)
        self.assertEqual("herdr_protocol_invalid", caught.exception.code)
