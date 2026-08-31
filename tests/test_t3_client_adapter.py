from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.adapters.headless_template import HeadlessProfileAdapter
from floati.adapters.t3 import (
    MAX_T3_RESPONSE_BYTES,
    T3ClientAdapter,
    T3ClientConfig,
    T3ReceiptLedger,
    T3TargetRegistration,
    T3TokenSource,
)
from floati.errors import ProtocolRefusal


OK = b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n\r\nsecret-body\n"
NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"


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


class T3ClientAdapterTests(unittest.TestCase):
    def registration(self, **changes: object) -> T3TargetRegistration:
        values: dict[str, object] = {
            "host": "127.0.0.1",
            "port": 3773,
            "path": "/",
            "registration_id": "fixture-t3-target",
        }
        values.update(changes)
        return T3TargetRegistration(**values)

    def config(self, **changes: object) -> T3ClientConfig:
        values: dict[str, object] = {
            "host": "127.0.0.1",
            "port": 3773,
            "path": "/",
            "target_registration": None,
        }
        values.update(changes)
        if "target_registration" not in changes:
            values["target_registration"] = self.registration(
                host=values["host"],
                port=values["port"],
                path=values["path"],
            )
        return T3ClientConfig(**values)

    def adapter(
        self,
        responses: list[bytes],
        *,
        config: T3ClientConfig | None = None,
        connect_error: BaseException | None = None,
    ) -> tuple[T3ClientAdapter, FakeSocket, T3ReceiptLedger, list[tuple[int, int]]]:
        channel = FakeSocket(responses, connect_error=connect_error)
        calls: list[tuple[int, int]] = []

        def factory(family: int, socket_type: int) -> FakeSocket:
            calls.append((family, socket_type))
            return channel

        ledger = T3ReceiptLedger()
        adapter = T3ClientAdapter(
            config or self.config(),
            receipt_sink=ledger.append,
            socket_factory=factory,
        )
        return adapter, channel, ledger, calls

    def test_config_requires_literal_registered_loopback_and_bound_path(self) -> None:
        for host in ("localhost", "127.0.0.2", "192.0.2.1", "::ffff:127.0.0.1"):
            with self.subTest(host=host):
                with self.assertRaises(ProtocolRefusal) as rejected:
                    self.config(host=host)
                self.assertEqual("t3_loopback_required", rejected.exception.code)

        with self.assertRaises(ProtocolRefusal) as unregistered:
            self.config(target_registration=None)
        self.assertEqual("t3_target_registration_required", unregistered.exception.code)

        with self.assertRaises(ProtocolRefusal) as mismatched:
            self.config(target_registration=self.registration(port=3774))
        self.assertEqual("t3_target_registration_mismatch", mismatched.exception.code)

        for path in ("", "health", "/../etc/passwd", "//", "/a/../b"):
            with self.subTest(path=path):
                with self.assertRaises(ProtocolRefusal) as rejected:
                    self.config(path=path)
                self.assertEqual("t3_path_invalid", rejected.exception.code)

        for port in (0, 65536):
            with self.subTest(port=port):
                with self.assertRaises(ProtocolRefusal) as rejected:
                    self.config(port=port)
                self.assertEqual("t3_port_invalid", rejected.exception.code)

        with self.assertRaises(ProtocolRefusal) as bool_port:
            self.config(port=True, target_registration=self.registration(port=0))
        self.assertEqual("t3_port_invalid", bool_port.exception.code)

    def test_consent_is_required_and_arm_disarm_are_receipted(self) -> None:
        adapter, channel, ledger, _calls = self.adapter([OK])
        with self.assertRaises(ProtocolRefusal) as unarmed:
            adapter.observe()
        self.assertEqual("t3_consent_required", unarmed.exception.code)
        self.assertEqual([], ledger.records())

        armed = adapter.arm()
        self.assertEqual("armed", armed["state"])
        with self.assertRaises(ProtocolRefusal) as duplicate:
            adapter.arm()
        self.assertEqual("t3_already_armed", duplicate.exception.code)
        disarmed = adapter.disarm()
        self.assertEqual("disarmed", disarmed["state"])
        with self.assertRaises(ProtocolRefusal) as closed:
            adapter.observe()
        self.assertEqual("t3_consent_required", closed.exception.code)
        self.assertTrue(channel.closed is False)
        self.assertEqual(
            ["armed", "disarmed"],
            [record["state"] for record in ledger.records() if record["kind"] == "t3_client_consent"],
        )

    def test_success_uses_only_status_line_and_never_receipts_token_or_body(self) -> None:
        adapter, channel, ledger, calls = self.adapter([OK])
        with mock.patch.dict(os.environ, {"T3_FIXTURE_TOKEN": "fixture-token-value"}, clear=False):
            adapter = T3ClientAdapter(
                self.config(token_source=T3TokenSource.from_env("T3_FIXTURE_TOKEN")),
                receipt_sink=ledger.append,
                socket_factory=lambda family, socket_type: (calls.append((family, socket_type)) or channel),
            )
            adapter.arm()
            result = adapter.observe()

        self.assertEqual("observed", result["observation_outcome"])
        self.assertEqual(200, result["http_status"])
        self.assertNotIn("secret-body", json.dumps(result))
        dumped = json.dumps(ledger.records())
        self.assertNotIn("fixture-token-value", dumped)
        self.assertNotIn("secret-body", dumped)
        request = channel.sent[0].decode("ascii")
        self.assertIn("GET / HTTP/1.1", request)
        self.assertIn("Host: 127.0.0.1", request)
        self.assertIn("Authorization: Bearer fixture-token-value", request)
        self.assertEqual([(socket.AF_INET, socket.SOCK_STREAM)], calls)
        self.assertEqual(("127.0.0.1", 3773), channel.address)
        self.assertTrue(channel.closed)
        kinds = [record["kind"] for record in ledger.records()]
        self.assertIn("t3_client_consent", kinds)
        self.assertIn("t3_client_connection", kinds)
        self.assertNotIn("t3_client_payload", kinds)
        self.assertTrue(all(record.get("token_source_configured") is True for record in ledger.records()))

    def test_file_token_source_is_passed_through_but_not_recorded(self) -> None:
        with tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp") as temporary:
            token_path = Path(temporary) / "t3.token"
            token_path.write_text("file-token-value\n", encoding="utf-8")
            adapter, channel, ledger, _calls = self.adapter(
                [OK],
                config=self.config(token_source=T3TokenSource.from_file(token_path)),
            )
            adapter.arm()
            adapter.observe()

        self.assertIn(b"Authorization: Bearer file-token-value", channel.sent[0])
        self.assertNotIn("file-token-value", json.dumps(ledger.records()))

    def test_http_status_is_observed_without_body(self) -> None:
        adapter, channel, _ledger, _calls = self.adapter([NOT_FOUND])
        adapter.arm()
        result = adapter.observe()
        self.assertEqual("observed", result["observation_outcome"])
        self.assertEqual(404, result["http_status"])
        self.assertEqual(1, len(channel.sent))

    def test_malformed_responses_are_typed_without_discovery(self) -> None:
        adapter, channel, _ledger, _calls = self.adapter([b"{not-http}\r\n"])
        adapter.arm()
        unavailable = adapter.observe()
        self.assertEqual("unavailable", unavailable["observation_outcome"])
        self.assertEqual("malformed_response", unavailable["observation_reason"])
        self.assertEqual(1, len(channel.sent))

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
                if record["kind"] == "t3_client_connection"
            ],
        )

    def test_ipv6_loopback_uses_literal_ipv6_endpoint(self) -> None:
        adapter, channel, _ledger, calls = self.adapter(
            [OK], config=self.config(host="::1", port=3774)
        )
        adapter.arm()
        adapter.observe()
        self.assertEqual([(socket.AF_INET6, socket.SOCK_STREAM)], calls)
        self.assertEqual(("::1", 3774, 0, 0), channel.address)
        self.assertIn(b"Host: [::1]", channel.sent[0])

    def test_timeout_and_oversized_responses_are_unavailable(self) -> None:
        for response, reason in (
            (socket.timeout(), "timeout"),
            (b"x" * (MAX_T3_RESPONSE_BYTES + 1), "malformed_response"),
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
                        if record["kind"] == "t3_client_connection"
                    ],
                )

    def test_observation_has_one_absolute_deadline_across_dripped_bytes(self) -> None:
        adapter, channel, _ledger, _calls = self.adapter(
            [b"HTTP/1.1 200", b" OK\r\n\r\n"],
            config=self.config(timeout_seconds=1.0),
        )
        adapter.arm()
        with mock.patch(
            "floati.adapters.t3.time.monotonic",
            side_effect=[0.0, 0.0, 0.4, 0.5, 1.1],
        ):
            result = adapter.observe()
        self.assertEqual("unavailable", result["observation_outcome"])
        self.assertEqual("timeout", result["observation_reason"])
        self.assertTrue(channel.closed)

    def test_arm_makes_the_target_immutable_until_disarm(self) -> None:
        adapter, _channel, _ledger, _calls = self.adapter([OK])
        adapter.arm()
        with self.assertRaises(ProtocolRefusal) as replaced:
            adapter.config = self.config(port=3774)
        self.assertEqual("t3_config_immutable", replaced.exception.code)
        self.assertEqual(3773, adapter.config.port)

    def test_client_is_not_a_worker_or_headless_profile(self) -> None:
        self.assertFalse(issubclass(T3ClientAdapter, HeadlessProfileAdapter))
        self.assertFalse(hasattr(T3ClientAdapter, "spawn"))
        self.assertFalse(hasattr(T3ClientAdapter, "drive"))
        self.assertEqual(T3ClientAdapter.name, "t3-client")


if __name__ == "__main__":
    unittest.main()
