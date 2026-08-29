"""Closed configuration and private transport tests for Worker bootstrap."""

from __future__ import annotations

import math
import socket
import struct
import tempfile
import unittest
from pathlib import Path

from floati.worker_bootstrap_protocol import (
    MAX_FRAME_BYTES,
    BootstrapChannel,
    BuiltInAdapterSpec,
    builtin_adapter_spec_from_payload,
    builtin_adapter_spec_to_payload,
    isolation_policy_from_payload,
    isolation_policy_to_payload,
    validate_builtin_adapter_spec,
    validate_isolation_backend,
)
from floati.worker_errors import WorkerAdapterFailure
from floati.worker_isolation import WorkerIsolationPolicy


class _StringSubclass(str):
    pass


class _TupleSubclass(tuple):
    pass


class _ListSubclass(list):
    pass


class _MappingSubclass(dict):
    def __iter__(self) -> object:
        raise AssertionError("hostile mapping hook ran")


class WorkerBootstrapProtocolTests(unittest.TestCase):
    def assert_isolation_refusal(self, operation: object) -> None:
        self.assertTrue(callable(operation))
        with self.assertRaises(WorkerAdapterFailure) as caught:
            operation()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, "effect_worker_isolation_unavailable")

    def _channels(self) -> tuple[BootstrapChannel, BootstrapChannel]:
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        sender = BootstrapChannel(left.detach())
        receiver = BootstrapChannel(right.detach())
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        return sender, receiver

    def _send_raw(self, raw: socket.socket, body: bytes) -> None:
        raw.sendall(struct.pack(">I", len(body)) + body)

    def test_builtin_spec_accepts_only_exact_kind_and_detached_command_tuple(self) -> None:
        original = BuiltInAdapterSpec("codex", ("/usr/local/bin/codex", "--json"))

        result = validate_builtin_adapter_spec(original)

        self.assertEqual(result, original)
        self.assertIsNot(result, original)
        self.assertIsNot(result.command, original.command)
        self.assertEqual(
            builtin_adapter_spec_to_payload(result),
            {"kind": "codex", "command": ["/usr/local/bin/codex", "--json"]},
        )
        self.assertEqual(
            builtin_adapter_spec_from_payload(
                {"kind": "pi", "command": ["/usr/local/bin/pi"]}
            ),
            BuiltInAdapterSpec("pi", ("/usr/local/bin/pi",)),
        )

    def test_builtin_spec_rejects_subclasses_stateful_sequences_controls_and_relative_executable(self) -> None:
        self.assert_isolation_refusal(
            lambda: validate_builtin_adapter_spec(
                BuiltInAdapterSpec(_StringSubclass("codex"), ("/bin/echo",))
            )
        )
        self.assert_isolation_refusal(
            lambda: validate_builtin_adapter_spec(
                BuiltInAdapterSpec("codex", _TupleSubclass(("/bin/echo",)))
            )
        )
        for command in (
            ("relative",),
            ("/bin/echo", "bad\x00value"),
            ("/bin/echo", "bad\u202evalue"),
            ("/bin/echo", "bad\u2028value"),
            ("/bin/echo", "bad\ud800value"),
        ):
            self.assert_isolation_refusal(
                lambda command=command: validate_builtin_adapter_spec(
                    BuiltInAdapterSpec("codex", command)
                )
            )
        self.assert_isolation_refusal(
            lambda: builtin_adapter_spec_from_payload(
                _MappingSubclass({"kind": "codex", "command": ["/bin/echo"]})
            )
        )
        self.assert_isolation_refusal(
            lambda: builtin_adapter_spec_from_payload(
                {"kind": "codex", "command": _ListSubclass(["/bin/echo"])}
            )
        )
        self.assert_isolation_refusal(
            lambda: builtin_adapter_spec_from_payload(
                {"kind": "codex", "command": ["/bin/echo", _StringSubclass("x")]}
            )
        )

    def test_channel_round_trips_every_existing_worker_frame_over_real_socketpair(self) -> None:
        sender, receiver = self._channels()
        frames = (
            ("isolation_ready", {"backend": "macos-sandbox"}),
            ("process_group", 123),
            ("spawned", None),
            ("descendant", {"provider_descendant_id": "native-1", "state": "observed"}),
            ("effect", {"verb": "intent", "idempotency_key": "key-1"}),
            ("result", [{"repo": "slipway", "sha": "a" * 40, "doc": "README.md"}]),
            ("effect_reporting_closed", None),
            ("effect_reporting_closed_ack", None),
            ("observation_closed", None),
            ("observation_closed_ack", None),
            ("failure", "adapter_error"),
        )

        for frame in frames:
            sender.send(frame)
            self.assertTrue(receiver.poll(0.5))
            self.assertEqual(receiver.recv(), frame)

    def test_channel_rejects_duplicate_keys_noncanonical_json_nan_trailing_and_oversize(self) -> None:
        sender, receiver = self._channels()
        sender.send(("spawned", None))
        self.assertEqual(receiver.recv(), ("spawned", None))
        malformed_bodies = (
            b'{"payload":null,"verb":"spawned","verb":"failure"}',
            b'{"payload":null, "verb":"spawned"}',
            b'{"payload":NaN,"verb":"spawned"}',
            b'{"payload":null,"verb":"spawned"}\n',
        )
        for body in malformed_bodies:
            raw_left, raw_right = socket.socketpair()
            self.addCleanup(raw_left.close)
            self.addCleanup(raw_right.close)
            channel = BootstrapChannel(raw_left.detach())
            self._send_raw(raw_right, body)
            self.assert_isolation_refusal(channel.recv)
            channel.close()
        raw_left, raw_right = socket.socketpair()
        self.addCleanup(raw_left.close)
        self.addCleanup(raw_right.close)
        channel = BootstrapChannel(raw_left.detach())
        raw_right.sendall(struct.pack(">I", MAX_FRAME_BYTES + 1))
        self.assert_isolation_refusal(channel.recv)
        channel.close()

    def test_channel_poll_eof_and_partial_frame_are_bounded_and_typed(self) -> None:
        sender, receiver = self._channels()
        self.assertFalse(receiver.poll(0.0))
        self.assert_isolation_refusal(lambda: receiver.poll(True))
        self.assert_isolation_refusal(lambda: receiver.poll(-0.1))
        self.assert_isolation_refusal(lambda: receiver.poll(math.inf))
        sender.close()
        self.assertTrue(receiver.poll(0.5))
        self.assert_isolation_refusal(receiver.recv)

        raw_left, raw_right = socket.socketpair()
        self.addCleanup(raw_left.close)
        self.addCleanup(raw_right.close)
        channel = BootstrapChannel(raw_left.detach())
        raw_right.sendall(struct.pack(">I", 24) + b'{"payload":')
        raw_right.shutdown(socket.SHUT_WR)
        self.assertTrue(channel.poll(0.5))
        self.assert_isolation_refusal(channel.recv)
        channel.close()

    def test_channel_encode_excessive_nesting_is_typed(self) -> None:
        sender, _ = self._channels()
        payload: object = None
        for _ in range(1_200):
            payload = [payload]

        self.assert_isolation_refusal(lambda: sender.send(("effect", payload)))

    def test_channel_decode_excessive_nesting_is_typed(self) -> None:
        raw_left, raw_right = socket.socketpair()
        self.addCleanup(raw_left.close)
        self.addCleanup(raw_right.close)
        channel = BootstrapChannel(raw_left.detach())
        self.addCleanup(channel.close)
        body = b'{"payload":' + (b"[" * 1_200) + b"null" + (b"]" * 1_200) + b',"verb":"spawned"}'
        self._send_raw(raw_right, body)

        self.assert_isolation_refusal(channel.recv)

    def test_channel_poll_huge_finite_timeout_is_typed(self) -> None:
        _, receiver = self._channels()

        self.assert_isolation_refusal(lambda: receiver.poll(1e308))

    def test_isolation_policy_payload_round_trip_preserves_exact_paths_and_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tenant = root / "tenant"
            workspace = root / "workspace"
            scratch = root / "scratch"
            effects = tenant / "effects"
            for path in (tenant, workspace, scratch, effects):
                path.mkdir()
            probe = effects / "probe"
            probe.touch()
            policy = WorkerIsolationPolicy(
                tenant_home=tenant,
                workspace=workspace,
                scratch=scratch,
                write_probe=probe,
                workspace_identity=(10, 11),
                scratch_identity=(12, 13),
                probe_identity=(14, 15),
            )

            payload = isolation_policy_to_payload(policy)

            self.assertEqual(
                payload,
                {
                    "tenant_home": str(tenant),
                    "workspace": str(workspace),
                    "scratch": str(scratch),
                    "write_probe": str(probe),
                    "workspace_identity": [10, 11],
                    "scratch_identity": [12, 13],
                    "probe_identity": [14, 15],
                },
            )
            self.assertEqual(isolation_policy_from_payload(payload), policy)
            malformed = dict(payload)
            malformed["scratch_identity"] = [True, 13]
            self.assert_isolation_refusal(lambda: isolation_policy_from_payload(malformed))
            self.assert_isolation_refusal(
                lambda: isolation_policy_from_payload(
                    _MappingSubclass(payload)
                )
            )

    def test_backend_validator_accepts_only_macos_or_canonical_landlock_v3_through_v999(self) -> None:
        for backend in ("macos-sandbox", "linux-landlock-v3", "linux-landlock-v999"):
            self.assertEqual(validate_isolation_backend(backend), backend)
        for backend in (
            "linux-landlock-v1",
            "linux-landlock-v2",
            "linux-landlock-v03",
            "linux-landlock-v0003",
            "linux-landlock-v1000",
            "linux-landlock-v3.0",
            "linux-landlock-v٣",
            _StringSubclass("macos-sandbox"),
            True,
        ):
            self.assert_isolation_refusal(
                lambda backend=backend: validate_isolation_backend(backend)
            )
