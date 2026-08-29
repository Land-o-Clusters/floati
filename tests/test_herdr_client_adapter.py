from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.adapters.herdr import (
    HerdrClientAdapter,
    HerdrClientConfig,
    HerdrReceiptLedger,
    HerdrTargetRegistration,
    HerdrTokenSource,
    MAX_HERDR_FRAME_BYTES,
)
from floati.errors import ProtocolRefusal


PING = b'{"version":"0.8.2","protocol":19,"capabilities":[]}\n'
PANE = (
    b'{"pane_id":"w1:p1","workspace_id":"w1","tab_id":"t1",'
    b'"agent_status":"working","revision":7,"interactive_ready":true,'
    b'"terminal":"fixture-only-content"}\n'
)


class FakeSocket:
    def __init__(self, responses: list[bytes], connect_error: BaseException | None = None):
        self.responses = list(responses)
        self.connect_error = connect_error
        self.address = None
        self.sent: list[bytes] = []
        self.timeout = None
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def connect(self, address: object) -> None:
        self.address = address
        if self.connect_error is not None:
            raise self.connect_error

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, _size: int) -> bytes:
        if not self.responses:
            return b""
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


class HerdrClientAdapterTests(unittest.TestCase):
    def registration(self, **changes: object) -> HerdrTargetRegistration:
        values: dict[str, object] = {
            "host": "127.0.0.1",
            "port": 3773,
            "pane_id": "w1:p1",
            "registration_id": "fixture-herdr-target",
        }
        values.update(changes)
        return HerdrTargetRegistration(**values)

    def config(self, **changes: object) -> HerdrClientConfig:
        values: dict[str, object] = {
            "host": "127.0.0.1",
            "port": 3773,
            "pane_id": "w1:p1",
            "target_registration": None,
        }
        values.update(changes)
        if "target_registration" not in changes:
            values["target_registration"] = self.registration(
                host=values["host"],
                port=values["port"],
                pane_id=values["pane_id"],
            )
        return HerdrClientConfig(**values)

    def adapter(
        self,
        responses: list[bytes],
        *,
        config: HerdrClientConfig | None = None,
        connect_error: BaseException | None = None,
    ) -> tuple[HerdrClientAdapter, FakeSocket, HerdrReceiptLedger, list[tuple[int, int]]]:
        channel = FakeSocket(responses, connect_error=connect_error)
        calls: list[tuple[int, int]] = []

        def factory(family: int, socket_type: int) -> FakeSocket:
            calls.append((family, socket_type))
            return channel

        ledger = HerdrReceiptLedger()
        adapter = HerdrClientAdapter(
            config or self.config(),
            receipt_sink=ledger.append,
            socket_factory=factory,
        )
        return adapter, channel, ledger, calls

    def test_config_requires_literal_registered_loopback_and_bound_pane(self) -> None:
        for host in ("localhost", "127.0.0.2", "192.0.2.1", "::ffff:127.0.0.1"):
            with self.subTest(host=host):
                with self.assertRaises(ProtocolRefusal) as rejected:
                    self.config(host=host)
                self.assertEqual("herdr_loopback_required", rejected.exception.code)

        with self.assertRaises(ProtocolRefusal) as unregistered:
            self.config(target_registration=None)
        self.assertEqual("herdr_target_registration_required", unregistered.exception.code)

        with self.assertRaises(ProtocolRefusal) as mismatched:
            self.config(target_registration=self.registration(port=3774))
        self.assertEqual("herdr_target_registration_mismatch", mismatched.exception.code)

        for pane_id in ("", "title", "w1", "w1:p1:extra"):
            with self.subTest(pane_id=pane_id):
                with self.assertRaises(ProtocolRefusal) as rejected:
                    self.config(pane_id=pane_id)
                self.assertEqual("herdr_pane_binding_invalid", rejected.exception.code)

        for port in (0, 65536):
            with self.subTest(port=port):
                with self.assertRaises(ProtocolRefusal) as rejected:
                    self.config(port=port)
                self.assertEqual("herdr_port_invalid", rejected.exception.code)

        with self.assertRaises(ProtocolRefusal) as bool_port:
            self.config(port=True, target_registration=self.registration(port=0))
        self.assertEqual("herdr_port_invalid", bool_port.exception.code)

    def test_consent_is_required_and_arm_disarm_are_receipted(self) -> None:
        adapter, channel, ledger, _calls = self.adapter([PING, PANE])
        with self.assertRaises(ProtocolRefusal) as unarmed:
            adapter.observe()
        self.assertEqual("herdr_consent_required", unarmed.exception.code)
        self.assertEqual([], ledger.records())

        armed = adapter.arm()
        self.assertEqual("armed", armed["state"])
        with self.assertRaises(ProtocolRefusal) as duplicate:
            adapter.arm()
        self.assertEqual("herdr_already_armed", duplicate.exception.code)
        disarmed = adapter.disarm()
        self.assertEqual("disarmed", disarmed["state"])
        with self.assertRaises(ProtocolRefusal) as closed:
            adapter.observe()
        self.assertEqual("herdr_consent_required", closed.exception.code)
        self.assertTrue(channel.closed is False)
        self.assertEqual(
            ["armed", "disarmed"],
            [record["state"] for record in ledger.records() if record["kind"] == "herdr_client_consent"],
        )

    def test_success_uses_only_fixed_reads_and_never_receipts_token_or_content(self) -> None:
        adapter, channel, ledger, calls = self.adapter([PING, PANE])
        with mock.patch.dict(os.environ, {"HERDR_FIXTURE_TOKEN": "fixture-token-value"}, clear=False):
            adapter = HerdrClientAdapter(
                self.config(token_source=HerdrTokenSource.from_env("HERDR_FIXTURE_TOKEN")),
                receipt_sink=ledger.append,
                socket_factory=lambda family, socket_type: (calls.append((family, socket_type)) or channel),
            )
            adapter.arm()
            result = adapter.observe()

        self.assertEqual("observed", result["observation_outcome"])
        self.assertEqual("working", result["host_status"])
        self.assertEqual("w1:p1", result["pane_id"])
        self.assertNotIn("terminal", result)
        self.assertNotIn("fixture-only-content", json.dumps(result))
        self.assertNotIn("fixture-token-value", json.dumps(ledger.records()))
        requests = [json.loads(raw.decode("utf-8")) for raw in channel.sent]
        self.assertEqual(["ping", "pane.get"], [request["method"] for request in requests])
        self.assertTrue(all(request["token"] == "fixture-token-value" for request in requests))
        self.assertEqual([(socket.AF_INET, socket.SOCK_STREAM)], calls)
        self.assertEqual(("127.0.0.1", 3773), channel.address)
        self.assertTrue(channel.closed)
        kinds = [record["kind"] for record in ledger.records()]
        self.assertIn("herdr_client_consent", kinds)
        self.assertIn("herdr_client_connection", kinds)
        self.assertNotIn("herdr_client_payload", kinds)

    def test_file_token_source_is_passed_through_but_not_recorded(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            token_path = Path(temporary) / "herdr.token"
            token_path.write_text("file-token-value\n", encoding="utf-8")
            adapter, channel, ledger, _calls = self.adapter(
                [PING, PANE],
                config=self.config(token_source=HerdrTokenSource.from_file(token_path)),
            )
            adapter.arm()
            adapter.observe()

        requests = [json.loads(raw.decode("utf-8")) for raw in channel.sent]
        self.assertEqual("file-token-value", requests[0]["token"])
        self.assertNotIn("file-token-value", json.dumps(ledger.records()))

    def test_version_mismatch_stops_before_pane_read(self) -> None:
        adapter, channel, ledger, _calls = self.adapter(
            [b'{"version":"0.8.2","protocol":18,"capabilities":[]}\n']
        )
        adapter.arm()
        result = adapter.observe()
        self.assertEqual("version_mismatch", result["observation_outcome"])
        self.assertEqual(1, len(channel.sent))
        outcomes = [
            record.get("outcome")
            for record in ledger.records()
            if record["kind"] == "herdr_client_connection"
        ]
        self.assertIn("version_mismatch", outcomes)

    def test_absent_and_malformed_responses_are_typed_without_discovery(self) -> None:
        adapter, channel, _ledger, _calls = self.adapter(
            [PING, b'{"error":"pane_not_found"}\n']
        )
        adapter.arm()
        absent = adapter.observe()
        self.assertEqual("absent", absent["observation_outcome"])
        self.assertEqual(["ping", "pane.get"], [json.loads(raw)["method"] for raw in channel.sent])

        malformed_adapter, malformed_channel, _malformed_ledger, _malformed_calls = self.adapter(
            [b"{not-json}\n"]
        )
        malformed_adapter.arm()
        unavailable = malformed_adapter.observe()
        self.assertEqual("unavailable", unavailable["observation_outcome"])
        self.assertEqual(1, len(malformed_channel.sent))

    def test_connect_failure_is_receipted_and_name_resolution_is_not_used(self) -> None:
        adapter, channel, ledger, calls = self.adapter(
            [], connect_error=OSError("fixture connection refused")
        )
        adapter.arm()
        with mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS is forbidden")):
            result = adapter.observe()
        self.assertEqual("unavailable", result["observation_outcome"])
        self.assertEqual([(socket.AF_INET, socket.SOCK_STREAM)], calls)
        self.assertTrue(channel.closed)
        self.assertIn(
            "connect_failed",
            [
                record.get("reason")
                for record in ledger.records()
                if record["kind"] == "herdr_client_connection"
            ],
        )

    def test_ipv6_loopback_uses_literal_ipv6_endpoint(self) -> None:
        adapter, channel, _ledger, calls = self.adapter(
            [PING, PANE], config=self.config(host="::1", port=3774)
        )
        adapter.arm()
        adapter.observe()
        self.assertEqual([(socket.AF_INET6, socket.SOCK_STREAM)], calls)
        self.assertEqual(("::1", 3774, 0, 0), channel.address)

    def test_timeout_oversized_and_duplicate_frames_are_unavailable(self) -> None:
        for response, reason in (
            (socket.timeout(), "timeout"),
            (b"x" * (MAX_HERDR_FRAME_BYTES + 1), "malformed_response"),
            (b'{"version":"0.8.2","protocol":19,"protocol":19,"capabilities":[]}\n', "malformed_response"),
        ):
            with self.subTest(reason=reason):
                adapter, channel, ledger, _calls = self.adapter([response])
                adapter.arm()
                result = adapter.observe()
                self.assertEqual("unavailable", result["observation_outcome"])
                self.assertEqual(reason, result["observation_reason"])
                self.assertTrue(channel.closed)
                self.assertIn(
                    reason,
                    [
                        record.get("reason")
                        for record in ledger.records()
                        if record["kind"] == "herdr_client_connection"
                    ],
                )

    def test_observation_has_one_absolute_deadline_across_dripped_frames(self) -> None:
        adapter, channel, _ledger, _calls = self.adapter(
            [b'{"version":"0.8.2",', b'"protocol":19,"capabilities":[]}\n'],
            config=self.config(timeout_seconds=1.0),
        )
        adapter.arm()
        with mock.patch(
            "floati.adapters.herdr.time.monotonic",
            side_effect=[0.0, 0.0, 0.4, 0.5, 1.1],
        ):
            result = adapter.observe()
        self.assertEqual("unavailable", result["observation_outcome"])
        self.assertEqual("timeout", result["observation_reason"])
        self.assertTrue(channel.closed)

    def test_arm_makes_the_target_immutable_until_disarm(self) -> None:
        adapter, _channel, _ledger, _calls = self.adapter([PING, PANE])
        adapter.arm()
        with self.assertRaises(ProtocolRefusal) as replaced:
            adapter.config = self.config(port=3774)
        self.assertEqual("herdr_config_immutable", replaced.exception.code)
        self.assertEqual(3773, adapter.config.port)

    def test_observe_cannot_change_the_exact_bound_pane(self) -> None:
        adapter, channel, ledger, _calls = self.adapter([PING, PANE])
        adapter.arm()
        with self.assertRaises(ProtocolRefusal) as rejected:
            adapter.observe("w1:p2")
        self.assertEqual("herdr_pane_binding_mismatch", rejected.exception.code)
        self.assertEqual([], channel.sent)
        self.assertEqual(
            ["armed"],
            [record["state"] for record in ledger.records() if record["kind"] == "herdr_client_consent"],
        )


if __name__ == "__main__":
    unittest.main()
