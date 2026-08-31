"""Dark, observation-only client for an explicitly registered T3 Code target.

Server-class t3 (`t3 serve` on a loopback port) is a loopback CLIENT, not a
HeadlessProfileAdapter and not a WorkerAdapter. One bounded HTTP GET over a
literal loopback socket after an explicit, receipted arm. The path is
operator-registered; floati never scans ports or discovers routes. Response
bodies are not mirrored into receipts.
"""

# Authority: docs/design/loopback-client-ruling-2026-08-28.md

from __future__ import annotations

import math
import os
import re
import socket
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from ..errors import ProtocolRefusal
from ..ids import uuid7_hex


MAX_T3_RESPONSE_BYTES = 65536
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,256}$")
_TOKEN_BYTES = 4096
_STATUS_LINE = re.compile(r"^HTTP/1\.[01] (\d{3}) ")


class _TokenSourceError(Exception):
    pass


class _ConnectError(Exception):
    pass


class _FrameError(Exception):
    pass


@dataclass(frozen=True)
class T3TokenSource:
    kind: str
    location: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {"env", "file"} or not isinstance(self.location, str):
            raise ProtocolRefusal(
                "t3_token_source_invalid",
                "token source must be an environment key or absolute file path",
            )
        if self.kind == "env":
            if not _ENVIRONMENT_KEY.fullmatch(self.location):
                raise ProtocolRefusal(
                    "t3_token_source_invalid",
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
                "t3_token_source_invalid",
                "file token source must be one absolute normalized path",
            )

    @classmethod
    def from_env(cls, name: str) -> "T3TokenSource":
        return cls("env", name)

    @classmethod
    def from_file(cls, path: Path | str) -> "T3TokenSource":
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
class T3TargetRegistration:
    host: str
    port: int
    path: str
    registration_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.host, str)
            or not isinstance(self.path, str)
            or type(self.port) is not int
            or not isinstance(self.registration_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.registration_id)
        ):
            raise ProtocolRefusal(
                "t3_target_registration_invalid",
                "target registration must be an explicit bounded capability",
            )


@dataclass(frozen=True)
class T3ClientConfig:
    host: str
    port: int
    path: str
    target_registration: Optional[T3TargetRegistration] = None
    token_source: Optional[T3TokenSource] = None
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or self.host not in _LOOPBACK_HOSTS:
            raise ProtocolRefusal(
                "t3_loopback_required",
                "T3 client targets must be the literal loopback address",
            )
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ProtocolRefusal("t3_port_invalid", "T3 client target port is outside bounds")
        registration = self.target_registration
        if type(registration) is not T3TargetRegistration:
            raise ProtocolRefusal(
                "t3_target_registration_required",
                "T3 client targets require an explicit registration capability",
            )
        if (
            not isinstance(self.path, str)
            or not _PATH.fullmatch(self.path)
            or ".." in self.path.split("/")
            or "//" in self.path
        ):
            raise ProtocolRefusal(
                "t3_path_invalid",
                "T3 client requires one exact registered request path",
            )
        if (
            registration.host,
            registration.port,
            registration.path,
        ) != (self.host, self.port, self.path):
            raise ProtocolRefusal(
                "t3_target_registration_mismatch",
                "target registration is not bound to the configured loopback target",
            )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 30
        ):
            raise ProtocolRefusal("t3_timeout_invalid", "T3 client timeout is outside bounds")
        if self.token_source is not None and type(self.token_source) is not T3TokenSource:
            raise ProtocolRefusal("t3_token_source_invalid", "T3 token source has an invalid type")


class T3ReceiptLedger:
    def __init__(self) -> None:
        self._records: list[Dict[str, object]] = []

    def append(self, record: Dict[str, object]) -> Dict[str, object]:
        self._records.append(dict(record))
        return dict(record)

    def records(self) -> list[Dict[str, object]]:
        return [dict(record) for record in self._records]


class T3ClientAdapter:
    """One consented loopback GET; observation is the HTTP status line only."""

    name = "t3-client"

    def __init__(
        self,
        config: T3ClientConfig,
        *,
        receipt_sink: Optional[object] = None,
        socket_factory: Callable[[int, int], object] = socket.socket,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        if type(config) is not T3ClientConfig:
            raise ProtocolRefusal("t3_config_invalid", "T3 client config has an invalid type")
        self._config = config
        self._socket_factory = socket_factory
        self._environ = os.environ if environ is None else environ
        self._armed = False
        self._armed_config: Optional[T3ClientConfig] = None
        self._local_receipts = T3ReceiptLedger()
        if receipt_sink is None:
            self._external_receipt_sink = None
        elif hasattr(receipt_sink, "append") and callable(receipt_sink.append):
            self._external_receipt_sink = receipt_sink.append
        elif callable(receipt_sink):
            self._external_receipt_sink = receipt_sink
        else:
            raise ProtocolRefusal("t3_receipt_sink_invalid", "T3 client receipt sink is not callable")

    @property
    def is_armed(self) -> bool:
        return self._armed

    @property
    def config(self) -> T3ClientConfig:
        return self._config

    @config.setter
    def config(self, _config: T3ClientConfig) -> None:
        raise ProtocolRefusal(
            "t3_config_immutable",
            "T3 target configuration cannot change after construction",
        )

    def arm(self) -> Dict[str, object]:
        if self._armed:
            raise ProtocolRefusal("t3_already_armed", "T3 client is already armed")
        receipt = self._receipt(
            "t3_client_consent",
            state="armed",
            target_registered=True,
            target_registration_id=self.config.target_registration.registration_id,
        )
        self._armed_config = self._config
        self._armed = True
        return receipt

    def disarm(self) -> Dict[str, object]:
        if not self._armed:
            raise ProtocolRefusal("t3_not_armed", "T3 client is not armed")
        receipt = self._receipt("t3_client_consent", state="disarmed")
        self._armed_config = None
        self._armed = False
        return receipt

    def observe(self) -> Dict[str, object]:
        if not self._armed or self._armed_config is None or self._config != self._armed_config:
            raise ProtocolRefusal(
                "t3_consent_required",
                "T3 client observation requires a current consent receipt",
            )
        connection_id = "t3-connection-" + uuid7_hex()
        self._connection_receipt(connection_id, phase="attempt", outcome="started", reason="registered_loopback_target")
        deadline = time.monotonic() + float(self.config.timeout_seconds)
        channel: Optional[object] = None
        status: Optional[int] = None
        try:
            try:
                token = (
                    None
                    if self.config.token_source is None
                    else self.config.token_source.read(self._environ)
                )
            except _TokenSourceError:
                result = self._observation("unavailable", "token_source_unavailable", status)
                self._connection_receipt(
                    connection_id, phase="outcome", outcome="unavailable", reason="token_source_unavailable"
                )
                return result
            channel = self._connect(deadline)
            self._connection_receipt(
                connection_id, phase="connected", outcome="connected", reason="literal_loopback_connected"
            )
            status = self._get(channel, token=token, deadline=deadline)
            result = self._observation("observed", "bounded_status_read", status)
            self._connection_receipt(
                connection_id, phase="outcome", outcome="observed", reason="bounded_status_read"
            )
            return result
        except socket.timeout:
            result = self._observation("unavailable", "timeout", status)
            self._connection_receipt(connection_id, phase="outcome", outcome="unavailable", reason="timeout")
            return result
        except TimeoutError:
            result = self._observation("unavailable", "timeout", status)
            self._connection_receipt(connection_id, phase="outcome", outcome="unavailable", reason="timeout")
            return result
        except _ConnectError:
            result = self._observation("unavailable", "connect_failed", status)
            self._connection_receipt(connection_id, phase="outcome", outcome="unavailable", reason="connect_failed")
            return result
        except _FrameError:
            result = self._observation("unavailable", "malformed_response", status)
            self._connection_receipt(
                connection_id, phase="outcome", outcome="unavailable", reason="malformed_response"
            )
            return result
        except OSError:
            result = self._observation("unavailable", "connection_failed", status)
            self._connection_receipt(connection_id, phase="outcome", outcome="unavailable", reason="connection_failed")
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
        address: object = (
            (self.config.host, self.config.port)
            if family == socket.AF_INET
            else (self.config.host, self.config.port, 0, 0)
        )
        try:
            self._set_timeout(channel, self._remaining(deadline))
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

    def _get(self, channel: object, *, token: Optional[str], deadline: float) -> int:
        host_header = (
            f"[{self.config.host}]" if ":" in self.config.host else self.config.host
        )
        headers = [
            f"GET {self.config.path} HTTP/1.1",
            f"Host: {host_header}",
            "Connection: close",
        ]
        if token is not None:
            headers.append(f"Authorization: Bearer {token}")
        payload = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
        self._set_timeout(channel, self._remaining(deadline))
        channel.sendall(payload)
        buffer = bytearray()
        while b"\r\n" not in buffer:
            if len(buffer) >= MAX_T3_RESPONSE_BYTES:
                raise _FrameError
            self._set_timeout(channel, self._remaining(deadline))
            chunk = channel.recv(min(4096, MAX_T3_RESPONSE_BYTES + 1 - len(buffer)))
            if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                raise _FrameError
            buffer.extend(chunk)
            if len(buffer) > MAX_T3_RESPONSE_BYTES:
                raise _FrameError
        line, _, _rest = buffer.partition(b"\r\n")
        try:
            decoded = bytes(line).decode("ascii")
        except UnicodeDecodeError as exc:
            raise _FrameError from exc
        match = _STATUS_LINE.match(decoded)
        if match is None:
            raise _FrameError
        return int(match.group(1))

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout()
        return remaining

    @staticmethod
    def _set_timeout(channel: object, timeout_seconds: float) -> None:
        if hasattr(channel, "settimeout"):
            channel.settimeout(timeout_seconds)

    def _observation(self, outcome: str, reason: str, status: Optional[int]) -> Dict[str, object]:
        return {
            "source_kind": "t3_loopback",
            "observation_outcome": outcome,
            "observation_reason": reason,
            "http_status": status,
        }

    def _connection_receipt(self, connection_id: str, *, phase: str, outcome: str, reason: str) -> None:
        self._receipt(
            "t3_client_connection",
            connection_id=connection_id,
            phase=phase,
            outcome=outcome,
            reason=reason,
        )

    def _receipt(self, kind: str, **fields: object) -> Dict[str, object]:
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "t3-client-receipt-" + uuid7_hex(),
            "kind": kind,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "host": self.config.host,
            "port": self.config.port,
            "path": self.config.path,
            "token_source_configured": self.config.token_source is not None,
        }
        record.update(fields)
        if self._external_receipt_sink is not None:
            self._external_receipt_sink(dict(record))
        return self._local_receipts.append(record)


T3ObservationSource = T3ClientAdapter
