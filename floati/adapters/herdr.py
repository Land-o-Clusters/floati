"""Dark, observation-only client for an explicitly registered Herdr target.

The client has no worker-host or public-command surface. It performs one
bounded pull for one bound pane after an explicit, receipted arm. Only literal
loopback addresses are accepted; the socket path is never discovered and
responses are reduced to the closed observation vocabulary.
"""

# Authority: docs/design/loopback-client-ruling-2026-08-28.md — all five
# loopback-client conditions are enforced by this module's closed boundary.

from __future__ import annotations

import json
import math
import os
import re
import socket
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from ..errors import ProtocolRefusal
from ..ids import uuid7_hex


MAX_HERDR_FRAME_BYTES = 65536
HERDR_PROTOCOL_VERSION = 19
HERDR_SOURCE_PROFILE_VERSION = 1
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_PANE_ID = re.compile(r"^w[0-9]+:p[0-9]+$")
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AGENT_STATUS = frozenset({"idle", "working", "blocked", "done", "unknown"})
_TOKEN_BYTES = 4096


class _FrameError(Exception):
    pass


class _VersionMismatch(Exception):
    def __init__(self, protocol: object) -> None:
        self.protocol = protocol


class _TokenSourceError(Exception):
    pass


class _ConnectError(Exception):
    pass


@dataclass(frozen=True)
class HerdrTokenSource:
    """A configured source for a local Herdr session token.

    The source descriptor may be retained. Its value is read only while one
    request is being prepared and is never placed in a receipt.
    """

    kind: str
    location: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"env", "file"} or not isinstance(self.location, str):
            raise ProtocolRefusal(
                "herdr_token_source_invalid",
                "token source must be an environment key or absolute file path",
            )
        if self.kind == "env":
            if not _ENVIRONMENT_KEY.fullmatch(self.location):
                raise ProtocolRefusal(
                    "herdr_token_source_invalid",
                    "environment token source has an invalid key",
                )
            return
        path = Path(self.location)
        if (
            not path.is_absolute()
            or str(path) != self.location
            or any(part in {"", ".", ".."} for part in path.parts[1:])
        ):
            raise ProtocolRefusal(
                "herdr_token_source_invalid",
                "file token source must be one absolute normalized path",
            )

    @classmethod
    def from_env(cls, name: str) -> "HerdrTokenSource":
        return cls("env", name)

    @classmethod
    def from_file(cls, path: Path | str) -> "HerdrTokenSource":
        return cls("file", str(path))

    def read(self, environ: Optional[Mapping[str, str]] = None) -> str:
        if self.kind == "env":
            source = os.environ if environ is None else environ
            value = source.get(self.location)
            if not isinstance(value, str) or not value:
                raise _TokenSourceError
            return value

        descriptor: Optional[int] = None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.location, flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _TOKEN_BYTES:
                raise _TokenSourceError
            chunks = bytearray()
            while len(chunks) <= _TOKEN_BYTES:
                chunk = os.read(descriptor, _TOKEN_BYTES + 1 - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            if len(chunks) > _TOKEN_BYTES:
                raise _TokenSourceError
            value = bytes(chunks).decode("utf-8").rstrip("\r\n")
            if not value:
                raise _TokenSourceError
            return value
        except _TokenSourceError:
            raise
        except (OSError, UnicodeError) as exc:
            raise _TokenSourceError from exc
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@dataclass(frozen=True)
class HerdrTargetRegistration:
    """Explicit operator-registration capability bound to one target."""

    host: str
    port: int
    pane_id: str
    registration_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.host, str)
            or not isinstance(self.pane_id, str)
            or type(self.port) is not int
            or not isinstance(self.registration_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.registration_id)
        ):
            raise ProtocolRefusal(
                "herdr_target_registration_invalid",
                "target registration must be an explicit bounded capability",
            )


@dataclass(frozen=True)
class HerdrClientConfig:
    """Closed target and pane binding supplied by the operator."""

    host: str
    port: int
    pane_id: str
    target_registration: Optional[HerdrTargetRegistration] = None
    token_source: Optional[HerdrTokenSource] = None
    expected_protocol: int = HERDR_PROTOCOL_VERSION
    timeout_seconds: float = 2.0
    inner_adapter: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or self.host not in _LOOPBACK_HOSTS:
            raise ProtocolRefusal(
                "herdr_loopback_required",
                "Herdr client targets must be the literal loopback address",
            )
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ProtocolRefusal(
                "herdr_port_invalid", "Herdr client target port is outside bounds"
            )
        registration = self.target_registration
        if type(registration) is not HerdrTargetRegistration:
            raise ProtocolRefusal(
                "herdr_target_registration_required",
                "Herdr client targets require an explicit registration capability",
            )
        if not isinstance(self.pane_id, str) or not _PANE_ID.fullmatch(self.pane_id):
            raise ProtocolRefusal(
                "herdr_pane_binding_invalid",
                "Herdr client requires one exact public pane coordinate",
            )
        if type(self.expected_protocol) is not int or self.expected_protocol != HERDR_PROTOCOL_VERSION:
            raise ProtocolRefusal(
                "herdr_protocol_invalid",
                "Herdr client is pinned to the ruled protocol version",
            )
        if (
            registration.host,
            registration.port,
            registration.pane_id,
        ) != (self.host, self.port, self.pane_id):
            raise ProtocolRefusal(
                "herdr_target_registration_mismatch",
                "target registration is not bound to the configured loopback target",
            )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 30
        ):
            raise ProtocolRefusal(
                "herdr_timeout_invalid", "Herdr client timeout is outside bounds"
            )
        if self.token_source is not None and type(self.token_source) is not HerdrTokenSource:
            raise ProtocolRefusal(
                "herdr_token_source_invalid", "Herdr token source has an invalid type"
            )
        if self.inner_adapter is not None and self.inner_adapter not in {"codex", "claude", "pi"}:
            raise ProtocolRefusal(
                "herdr_inner_adapter_invalid",
                "Herdr host identity must name a closed inner adapter",
            )


class HerdrReceiptLedger:
    """Small append-only sink useful for dark callers and fixture gates."""

    def __init__(self) -> None:
        self._records: list[Dict[str, object]] = []

    def append(self, record: Dict[str, object]) -> Dict[str, object]:
        self._records.append(dict(record))
        return dict(record)

    def records(self) -> list[Dict[str, object]]:
        return [dict(record) for record in self._records]


class HerdrClientAdapter:
    """Pull one bound pane observation over a consented literal loopback link."""

    name = "herdr-client"

    def __init__(
        self,
        config: Optional[HerdrClientConfig] = None,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        pane_id: Optional[str] = None,
        target_registration: Optional[HerdrTargetRegistration] = None,
        token_source: Optional[HerdrTokenSource] = None,
        timeout_seconds: float = 2.0,
        receipt_sink: Optional[object] = None,
        socket_factory: Callable[[int, int], object] = socket.socket,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        if config is None:
            if host is None or port is None or pane_id is None:
                raise ProtocolRefusal(
                    "herdr_config_required",
                    "Herdr client requires an explicit target and pane binding",
                )
            config = HerdrClientConfig(
                host=host,
                port=port,
                pane_id=pane_id,
                target_registration=target_registration,
                token_source=token_source,
                timeout_seconds=timeout_seconds,
            )
        elif any(value is not None for value in (host, port, pane_id)):
            raise ProtocolRefusal(
                "herdr_config_conflict",
                "Herdr client accepts either a config or explicit constructor fields",
            )
        if type(config) is not HerdrClientConfig:
            raise ProtocolRefusal("herdr_config_invalid", "Herdr client config has an invalid type")
        self._config = config
        self._socket_factory = socket_factory
        self._environ = os.environ if environ is None else environ
        self._armed = False
        self._armed_config: Optional[HerdrClientConfig] = None
        self._local_receipts = HerdrReceiptLedger()
        if receipt_sink is None:
            self._external_receipt_sink = None
        elif hasattr(receipt_sink, "append") and callable(receipt_sink.append):
            self._external_receipt_sink = receipt_sink.append
        elif callable(receipt_sink):
            self._external_receipt_sink = receipt_sink
        else:
            raise ProtocolRefusal(
                "herdr_receipt_sink_invalid", "Herdr client receipt sink is not callable"
            )

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def config(self) -> HerdrClientConfig:
        return self._config

    @config.setter
    def config(self, _config: HerdrClientConfig) -> None:
        raise ProtocolRefusal(
            "herdr_config_immutable",
            "Herdr target configuration cannot change after construction",
        )

    @property
    def receipt_ledger(self) -> HerdrReceiptLedger:
        return self._local_receipts

    def arm(self) -> Dict[str, object]:
        if self._armed:
            raise ProtocolRefusal("herdr_already_armed", "Herdr client is already armed")
        receipt = self._receipt(
            "herdr_client_consent",
            state="armed",
            target_registered=True,
            target_registration_id=self.config.target_registration.registration_id,
        )
        self._armed_config = self._config
        self._armed = True
        return receipt

    def disarm(self) -> Dict[str, object]:
        if not self._armed:
            raise ProtocolRefusal("herdr_not_armed", "Herdr client is not armed")
        receipt = self._receipt("herdr_client_consent", state="disarmed")
        self._armed_config = None
        self._armed = False
        return receipt

    def close(self) -> Optional[Dict[str, object]]:
        if not self._armed:
            return None
        return self.disarm()

    def __enter__(self) -> "HerdrClientAdapter":
        self.arm()
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.disarm()

    def observe(self, pane_id: Optional[str] = None) -> Dict[str, object]:
        if not self._armed:
            raise ProtocolRefusal(
                "herdr_consent_required",
                "Herdr client observation requires a current consent receipt",
            )
        if self._armed_config is None or self._config != self._armed_config:
            raise ProtocolRefusal(
                "herdr_consent_target_changed",
                "Herdr observation target changed after the consent receipt",
            )
        if pane_id is not None and pane_id != self.config.pane_id:
            raise ProtocolRefusal(
                "herdr_pane_binding_mismatch",
                "Herdr observation may read only the configured pane coordinate",
            )

        connection_id = "herdr-connection-" + uuid7_hex()
        self._connection_receipt(
            connection_id,
            phase="attempt",
            outcome="started",
            reason="registered_loopback_target",
        )
        deadline = time.monotonic() + float(self.config.timeout_seconds)
        channel: Optional[object] = None
        protocol: Optional[int] = None
        try:
            try:
                token = (
                    None
                    if self.config.token_source is None
                    else self.config.token_source.read(self._environ)
                )
            except _TokenSourceError:
                result = self._observation("unavailable", "token_source_unavailable", protocol)
                self._connection_receipt(
                    connection_id,
                    phase="outcome",
                    outcome="unavailable",
                    reason="token_source_unavailable",
                )
                return result

            channel = self._connect(deadline)
            self._connection_receipt(
                connection_id,
                phase="connected",
                outcome="connected",
                reason="literal_loopback_connected",
            )
            ping = self._request(channel, "ping", token=token, deadline=deadline)
            try:
                protocol = self._validate_ping(ping)
            except _VersionMismatch as mismatch:
                protocol = mismatch.protocol if type(mismatch.protocol) is int else None
                result = self._observation("version_mismatch", "protocol_mismatch", protocol)
                self._connection_receipt(
                    connection_id,
                    phase="outcome",
                    outcome="version_mismatch",
                    reason="protocol_mismatch",
                )
                return result

            pane = self._request(
                channel,
                "pane.get",
                pane_id=self.config.pane_id,
                token=token,
                deadline=deadline,
            )
            if self._pane_is_absent(pane):
                result = self._observation("absent", "pane_not_found", protocol)
                self._connection_receipt(
                    connection_id,
                    phase="outcome",
                    outcome="absent",
                    reason="pane_not_found",
                )
                return result
            self._validate_pane(pane)
            result = self._observation("observed", "bounded_pane_read", protocol, pane["agent_status"])
            self._connection_receipt(
                connection_id,
                phase="outcome",
                outcome="observed",
                reason="bounded_pane_read",
            )
            return result
        except socket.timeout:
            result = self._observation("unavailable", "timeout", protocol)
            self._connection_receipt(
                connection_id, phase="outcome", outcome="unavailable", reason="timeout"
            )
            return result
        except TimeoutError:
            result = self._observation("unavailable", "timeout", protocol)
            self._connection_receipt(
                connection_id, phase="outcome", outcome="unavailable", reason="timeout"
            )
            return result
        except _ConnectError:
            result = self._observation("unavailable", "connect_failed", protocol)
            self._connection_receipt(
                connection_id,
                phase="outcome",
                outcome="unavailable",
                reason="connect_failed",
            )
            return result
        except _FrameError:
            result = self._observation("unavailable", "malformed_response", protocol)
            self._connection_receipt(
                connection_id,
                phase="outcome",
                outcome="unavailable",
                reason="malformed_response",
            )
            return result
        except OSError:
            result = self._observation("unavailable", "connection_failed", protocol)
            self._connection_receipt(
                connection_id,
                phase="outcome",
                outcome="unavailable",
                reason="connection_failed",
            )
            return result
        finally:
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    pass

    def _connect(self, deadline: float) -> object:
        family = socket.AF_INET if self.config.host == "127.0.0.1" else socket.AF_INET6
        channel = self._socket_factory(family, socket.SOCK_STREAM)
        address: object
        if family == socket.AF_INET:
            address = (self.config.host, self.config.port)
        else:
            address = (self.config.host, self.config.port, 0, 0)
        try:
            self._set_channel_timeout(channel, self._remaining(deadline))
            channel.connect(address)
        except (socket.timeout, TimeoutError):
            try:
                channel.close()
            except OSError:
                pass
            raise
        except OSError as exc:
            try:
                channel.close()
            except OSError:
                pass
            raise _ConnectError from exc
        except Exception:
            try:
                channel.close()
            except OSError:
                pass
            raise
        return channel

    def _request(
        self,
        channel: object,
        method: str,
        *,
        pane_id: Optional[str] = None,
        token: Optional[str] = None,
        deadline: float,
    ) -> Dict[str, object]:
        request: Dict[str, object] = {"method": method}
        if pane_id is not None:
            request["pane_id"] = pane_id
        if token is not None:
            request["token"] = token
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self._set_channel_timeout(channel, self._remaining(deadline))
        channel.sendall(payload)
        return self._read_frame(channel, deadline)

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout()
        return remaining

    @staticmethod
    def _set_channel_timeout(channel: object, timeout_seconds: float) -> None:
        if hasattr(channel, "settimeout"):
            channel.settimeout(timeout_seconds)

    @staticmethod
    def _read_frame(channel: object, deadline: float) -> Dict[str, object]:
        buffer = bytearray()
        while b"\n" not in buffer:
            if len(buffer) >= MAX_HERDR_FRAME_BYTES:
                raise _FrameError
            HerdrClientAdapter._set_channel_timeout(
                channel, HerdrClientAdapter._remaining(deadline)
            )
            chunk = channel.recv(min(4096, MAX_HERDR_FRAME_BYTES + 1 - len(buffer)))
            if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                raise _FrameError
            buffer.extend(chunk)
            if len(buffer) > MAX_HERDR_FRAME_BYTES:
                raise _FrameError
        line, _, _remainder = buffer.partition(b"\n")
        if not line or len(line) > MAX_HERDR_FRAME_BYTES:
            raise _FrameError
        try:
            value = json.loads(
                bytes(line).decode("utf-8"),
                object_pairs_hook=HerdrClientAdapter._unique_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, _FrameError):
            raise _FrameError from None
        if not isinstance(value, dict):
            raise _FrameError
        return value

    @staticmethod
    def _unique_object(pairs: list[Tuple[str, object]]) -> Dict[str, object]:
        value: Dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise _FrameError
            value[key] = item
        return value

    @staticmethod
    def _validate_ping(ping: Dict[str, object]) -> int:
        protocol = ping.get("protocol")
        if type(protocol) is not int:
            raise _FrameError
        if protocol != HERDR_PROTOCOL_VERSION:
            raise _VersionMismatch(protocol)
        if (
            not isinstance(ping.get("version"), str)
            or not ping["version"]
            or not isinstance(ping.get("capabilities"), list)
        ):
            raise _FrameError
        return protocol

    def _validate_pane(self, pane: Dict[str, object]) -> None:
        required = {
            "pane_id",
            "workspace_id",
            "tab_id",
            "agent_status",
            "revision",
            "interactive_ready",
        }
        if not required.issubset(pane):
            raise _FrameError
        if pane["pane_id"] != self.config.pane_id:
            raise _FrameError
        if (
            not isinstance(pane["workspace_id"], str)
            or not pane["workspace_id"]
            or not isinstance(pane["tab_id"], str)
            or not pane["tab_id"]
            or not isinstance(pane["agent_status"], str)
            or pane["agent_status"] not in _AGENT_STATUS
            or type(pane["revision"]) is not int
            or pane["revision"] < 0
            or type(pane["interactive_ready"]) is not bool
        ):
            raise _FrameError

    @staticmethod
    def _pane_is_absent(pane: Dict[str, object]) -> bool:
        error = pane.get("error", pane.get("code"))
        if isinstance(error, dict):
            error = error.get("code")
        return error == "pane_not_found"

    def _observation(
        self,
        outcome: str,
        reason: str,
        protocol: Optional[int],
        status: Optional[object] = None,
    ) -> Dict[str, object]:
        return {
            "source_kind": "herdr_socket",
            "source_profile_version": HERDR_SOURCE_PROFILE_VERSION,
            "herdr_api_version": protocol,
            "pane_id": self.config.pane_id,
            "host_status": status if isinstance(status, str) else None,
            "observation_outcome": outcome,
            "observation_reason": reason,
            "observed_at_testimony": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }

    def _connection_receipt(
        self,
        connection_id: str,
        *,
        phase: str,
        outcome: str,
        reason: str,
    ) -> Dict[str, object]:
        return self._receipt(
            "herdr_client_connection",
            connection_id=connection_id,
            phase=phase,
            outcome=outcome,
            reason=reason,
        )

    def _receipt(self, kind: str, **fields: object) -> Dict[str, object]:
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "herdr-client-receipt-" + uuid7_hex(),
            "kind": kind,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "host": self.config.host,
            "port": self.config.port,
            "pane_id": self.config.pane_id,
            "token_source_configured": self.config.token_source is not None,
        }
        record.update(fields)
        if self._external_receipt_sink is not None:
            self._external_receipt_sink(dict(record))
        return self._local_receipts.append(record)


HerdrObservationSource = HerdrClientAdapter
HerdrClient = HerdrClientAdapter
TokenSource = HerdrTokenSource
