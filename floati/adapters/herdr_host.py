"""Herdr worker host — places a closed inner adapter in one bound pane.

This is the AD-1 work-column seam (Q5). It is not a standalone worker named
``herdr`` and it is not the observation client. Durable identity is
``host_kind=herdr`` plus ``inner_adapter`` in {codex, claude, pi}.

The host pin is protocol 20, measured from the live 0.8.2 status capture.
The observation client keeps protocol 19. A peer protocol above this pin is
a VS-1 skew refusal that names both versions.

Live 0.8.2 AF_UNIX dialect (AD-1h-2) is ``{"id","method","params"}`` with
``params`` always present; one fresh connection per request. TCP loopback
remains available for fixture hosts. The observation client is unchanged.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from ..errors import ProtocolRefusal
from ..ids import uuid7_hex
from ..worker_errors import WorkerAdapterFailure
from .herdr import (
    HERDR_PROTOCOL_VERSION,
    MAX_HERDR_FRAME_BYTES,
    HerdrReceiptLedger,
    HerdrTargetRegistration,
    HerdrTokenSource,
    _LOOPBACK_HOSTS,
    _PANE_ID,
    _TokenSourceError,
)

HERDR_HOST_PROTOCOL_VERSION = 20
HERDR_HOST_PIN_RECEIPT = (
    "docs/evidence/gauntlet/captures/H-ad1-herdr-scope-status.out"
)
_INNER_ADAPTERS = frozenset({"codex", "claude", "pi"})


def protocol_pins() -> tuple[Dict[str, object], Dict[str, object]]:
    """VS-4 projection: both herdr protocol pins, named separately."""

    return (
        {
            "component": "herdr-client",
            "protocol": HERDR_PROTOCOL_VERSION,
        },
        {
            "component": "herdr-host",
            "protocol": HERDR_HOST_PROTOCOL_VERSION,
            "measured_receipt": HERDR_HOST_PIN_RECEIPT,
        },
    )


@dataclass(frozen=True)
class HerdrHostConfig:
    """Closed loopback target, pane, and inner-adapter identity."""

    host: str
    port: int
    pane_id: str
    inner_adapter: str
    target_registration: Optional[HerdrTargetRegistration] = None
    token_source: Optional[HerdrTokenSource] = None
    socket_path: Optional[str] = None
    expected_protocol: int = HERDR_HOST_PROTOCOL_VERSION
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or self.host not in _LOOPBACK_HOSTS:
            raise ProtocolRefusal(
                "herdr_loopback_required",
                "Herdr host targets must be the literal loopback address",
            )
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ProtocolRefusal(
                "herdr_port_invalid", "Herdr host target port is outside bounds"
            )
        registration = self.target_registration
        if type(registration) is not HerdrTargetRegistration:
            raise ProtocolRefusal(
                "herdr_target_registration_required",
                "Herdr host targets require an explicit registration capability",
            )
        if not isinstance(self.pane_id, str) or not _PANE_ID.fullmatch(self.pane_id):
            raise ProtocolRefusal(
                "herdr_pane_binding_invalid",
                "Herdr host requires one exact public pane coordinate",
            )
        if (
            type(self.expected_protocol) is not int
            or self.expected_protocol != HERDR_HOST_PROTOCOL_VERSION
        ):
            raise ProtocolRefusal(
                "herdr_protocol_invalid",
                "Herdr host is pinned to the measured protocol version",
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
        if self.inner_adapter not in _INNER_ADAPTERS:
            raise ProtocolRefusal(
                "herdr_inner_adapter_invalid",
                "Herdr host identity must name a closed inner adapter",
            )
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 30
        ):
            raise ProtocolRefusal(
                "herdr_timeout_invalid", "Herdr host timeout is outside bounds"
            )
        if self.token_source is not None and type(self.token_source) is not HerdrTokenSource:
            raise ProtocolRefusal(
                "herdr_token_source_invalid", "Herdr token source has an invalid type"
            )
        if self.socket_path is not None:
            if not isinstance(self.socket_path, str):
                raise ProtocolRefusal(
                    "herdr_socket_path_invalid",
                    "Herdr host unix socket path must be one absolute path",
                )
            path = Path(self.socket_path)
            if (
                not path.is_absolute()
                or str(path) != self.socket_path
                or any(part in {"", ".", ".."} for part in path.parts[1:])
            ):
                raise ProtocolRefusal(
                    "herdr_socket_path_invalid",
                    "Herdr host unix socket path must be one absolute normalized path",
                )


@dataclass(frozen=True)
class HerdrHostHandle:
    pane_id: str
    inner_adapter: str
    protocol: int
    connection_id: str


class HerdrWorkerHost:
    """Place one closed inner adapter in one operator-bound Herdr pane."""

    name = "herdr-host"
    host_kind = "herdr"
    cancel_mode = "unavailable"
    INNER_ADAPTERS = _INNER_ADAPTERS

    def __init__(
        self,
        config: HerdrHostConfig,
        *,
        receipt_sink: Optional[object] = None,
        socket_factory: Callable[[int, int], object] = socket.socket,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        if type(config) is not HerdrHostConfig:
            raise ProtocolRefusal("herdr_config_invalid", "Herdr host config has an invalid type")
        self._config = config
        self._socket_factory = socket_factory
        self._environ = os.environ if environ is None else environ
        self._armed = False
        self._armed_config: Optional[HerdrHostConfig] = None
        self._local_receipts = HerdrReceiptLedger()
        if receipt_sink is None:
            self._external_receipt_sink = None
        elif hasattr(receipt_sink, "append") and callable(receipt_sink.append):
            self._external_receipt_sink = receipt_sink.append
        elif callable(receipt_sink):
            self._external_receipt_sink = receipt_sink
        else:
            raise ProtocolRefusal(
                "herdr_receipt_sink_invalid", "Herdr host receipt sink is not callable"
            )

    @property
    def config(self) -> HerdrHostConfig:
        return self._config

    def arm(self) -> Dict[str, object]:
        if self._armed:
            raise ProtocolRefusal("herdr_already_armed", "Herdr host is already armed")
        receipt = self._receipt(
            "herdr_host_consent",
            state="armed",
            inner_adapter=self.config.inner_adapter,
            target_registration_id=self.config.target_registration.registration_id,
        )
        self._armed_config = self._config
        self._armed = True
        return receipt

    def disarm(self) -> Dict[str, object]:
        if not self._armed:
            raise ProtocolRefusal("herdr_not_armed", "Herdr host is not armed")
        receipt = self._receipt("herdr_host_consent", state="disarmed")
        self._armed_config = None
        self._armed = False
        return receipt

    def spawn(self, item: Dict[str, object], *, deadline_seconds: float) -> HerdrHostHandle:
        self._require_armed()
        if item.get("effect_context") is not None:
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        inner = item.get("inner_adapter", self.config.inner_adapter)
        if inner not in self.INNER_ADAPTERS:
            raise ProtocolRefusal(
                "herdr_inner_adapter_invalid",
                "Herdr host spawn requires a closed inner adapter",
            )
        if inner != self.config.inner_adapter:
            raise ProtocolRefusal(
                "herdr_inner_adapter_mismatch",
                "spawn inner adapter must match the bound host identity",
            )
        pane_id = item.get("pane_id", self.config.pane_id)
        if pane_id != self.config.pane_id:
            raise ProtocolRefusal(
                "herdr_pane_binding_mismatch",
                "Herdr host may bind only the configured pane coordinate",
            )
        connection_id = "herdr-host-connection-" + uuid7_hex()
        deadline = time.monotonic() + float(deadline_seconds)
        token = self._token()
        ping = self._rpc("ping", {}, token=token, deadline=deadline)
        protocol = self._validate_host_ping(ping)
        pane = self._rpc(
            "pane.get",
            {"pane_id": self.config.pane_id},
            token=token,
            deadline=deadline,
        )
        if self._pane_is_absent(pane):
            raise ProtocolRefusal(
                "herdr_pane_absent",
                "Herdr host spawn requires an operator-bound pane",
            )
        placed = self._rpc(
            "agent.start",
            {
                "pane_id": self.config.pane_id,
                "inner_adapter": inner,
                "host_kind": self.host_kind,
            },
            token=token,
            deadline=deadline,
        )
        if not isinstance(placed, dict) or placed.get("error") is not None:
            raise ProtocolRefusal(
                "herdr_host_place_failed",
                "Herdr host did not receive a pane placement receipt",
            )
        if not isinstance(placed.get("result"), dict):
            raise ProtocolRefusal(
                "herdr_host_place_failed",
                "Herdr host did not receive a pane placement receipt",
            )
        handle = HerdrHostHandle(
            pane_id=self.config.pane_id,
            inner_adapter=inner,
            protocol=protocol,
            connection_id=connection_id,
        )
        self._receipt(
            "herdr_host_spawn",
            pane_id=handle.pane_id,
            inner_adapter=handle.inner_adapter,
            host_kind=self.host_kind,
            protocol=protocol,
        )
        return handle

    def drive(
        self, handle: object, item: Dict[str, object], *, deadline_seconds: float
    ) -> list[Dict[str, str]]:
        self._require_armed()
        if not isinstance(handle, HerdrHostHandle):
            raise ProtocolRefusal("herdr_host_handle_invalid", "Herdr host drive requires its own handle")
        if handle.inner_adapter != self.config.inner_adapter:
            raise ProtocolRefusal(
                "herdr_inner_adapter_mismatch",
                "drive must preserve the bound inner adapter identity",
            )
        if handle.pane_id != self.config.pane_id:
            raise ProtocolRefusal(
                "herdr_pane_binding_mismatch",
                "drive must preserve the bound pane coordinate",
            )
        deadline = time.monotonic() + float(deadline_seconds)
        token = self._token()
        ping = self._rpc("ping", {}, token=token, deadline=deadline)
        protocol = self._validate_host_ping(ping)
        pane = self._rpc(
            "pane.get",
            {"pane_id": handle.pane_id},
            token=token,
            deadline=deadline,
        )
        if self._pane_is_absent(pane):
            raise ProtocolRefusal(
                "herdr_pane_absent",
                "Herdr host drive requires the spawned pane to remain bound",
            )
        self._receipt(
            "herdr_host_drive",
            pane_id=handle.pane_id,
            inner_adapter=handle.inner_adapter,
            host_kind=self.host_kind,
            protocol=protocol,
        )
        return [
            {
                "event": "drive",
                "host_kind": self.host_kind,
                "inner_adapter": handle.inner_adapter,
                "pane_id": handle.pane_id,
            }
        ]

    def _require_armed(self) -> None:
        if not self._armed or self._armed_config is None or self._config != self._armed_config:
            raise ProtocolRefusal(
                "herdr_consent_required",
                "Herdr host spawn/drive requires a current consent receipt",
            )

    def _token(self) -> Optional[str]:
        if self.config.token_source is None:
            return None
        try:
            return self.config.token_source.read(self._environ)
        except _TokenSourceError as exc:
            raise ProtocolRefusal(
                "herdr_token_source_unavailable",
                "Herdr host token source could not be read",
            ) from exc

    def _validate_host_ping(self, ping: Dict[str, object]) -> int:
        if ping.get("error") is not None:
            raise ProtocolRefusal(
                "herdr_protocol_invalid",
                "Herdr ping returned an error envelope",
            )
        result = ping.get("result")
        if not isinstance(result, dict) or result.get("type") != "pong":
            raise ProtocolRefusal(
                "herdr_protocol_invalid",
                "Herdr ping is missing the captured result envelope",
            )
        protocol = result.get("protocol")
        if type(protocol) is not int:
            raise ProtocolRefusal("herdr_protocol_invalid", "Herdr ping protocol is not an integer")
        pin = HERDR_HOST_PROTOCOL_VERSION
        if protocol > pin:
            raise ProtocolRefusal(
                "version_skew",
                f"herdr host pin is protocol {pin}; peer spoke {protocol}",
                remedy=(
                    "update the herdr host pin as a measured change citing a "
                    "live handshake receipt"
                ),
            )
        if protocol != pin:
            raise ProtocolRefusal(
                "herdr_protocol_mismatch",
                f"herdr host pin is protocol {pin}; peer spoke {protocol}",
            )
        capabilities = result.get("capabilities")
        if (
            not isinstance(result.get("version"), str)
            or not result["version"]
            or not isinstance(capabilities, (list, dict))
        ):
            raise ProtocolRefusal("herdr_protocol_invalid", "Herdr ping is missing required fields")
        return protocol

    def _connect(self, deadline: float) -> object:
        if self.config.socket_path is not None:
            family = socket.AF_UNIX
            address: object = self.config.socket_path
        elif self.config.host == "127.0.0.1":
            family = socket.AF_INET
            address = (self.config.host, self.config.port)
        else:
            family = socket.AF_INET6
            address = (self.config.host, self.config.port, 0, 0)
        channel = self._socket_factory(family, socket.SOCK_STREAM)
        try:
            self._set_channel_timeout(channel, self._remaining(deadline))
            channel.connect(address)
        except Exception:
            try:
                channel.close()
            except OSError:
                pass
            raise
        return channel

    def _rpc(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        token: Optional[str],
        deadline: float,
    ) -> Dict[str, object]:
        """One request on one fresh connection — live 0.8.2 unix dialect."""

        payload_params: Dict[str, object] = dict(params)
        if token is not None:
            payload_params["token"] = token
        request_id = method.replace(".", "-") + "-" + uuid7_hex()
        envelope = {
            "id": request_id,
            "method": method,
            "params": payload_params,
        }
        payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        channel: Optional[object] = None
        try:
            channel = self._connect(deadline)
            self._set_channel_timeout(channel, self._remaining(deadline))
            channel.sendall(payload)
            return self._read_frame(channel, deadline)
        finally:
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    pass

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
                raise ProtocolRefusal("herdr_frame_invalid", "Herdr host frame exceeded bounds")
            HerdrWorkerHost._set_channel_timeout(
                channel, HerdrWorkerHost._remaining(deadline)
            )
            chunk = channel.recv(min(4096, MAX_HERDR_FRAME_BYTES + 1 - len(buffer)))
            if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                raise ProtocolRefusal("herdr_frame_invalid", "Herdr host frame was empty")
            buffer.extend(chunk)
            if len(buffer) > MAX_HERDR_FRAME_BYTES:
                raise ProtocolRefusal("herdr_frame_invalid", "Herdr host frame exceeded bounds")
        line, _, _remainder = buffer.partition(b"\n")
        try:
            value = json.loads(bytes(line).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal("herdr_frame_invalid", "Herdr host frame was not JSON") from exc
        if not isinstance(value, dict):
            raise ProtocolRefusal("herdr_frame_invalid", "Herdr host frame was not an object")
        return value

    @staticmethod
    def _pane_is_absent(pane: Dict[str, object]) -> bool:
        error = pane.get("error")
        if isinstance(error, dict):
            if error.get("code") == "pane_not_found":
                return True
        elif error == "pane_not_found":
            return True
        result = pane.get("result")
        if not isinstance(result, dict):
            return True
        body = result.get("pane") if result.get("type") == "pane_info" else result
        if not isinstance(body, dict):
            return True
        return body.get("pane_id") is None

    def _receipt(self, kind: str, **fields: object) -> Dict[str, object]:
        record = {
            "id": "herdr-host-receipt-" + uuid7_hex(),
            "kind": kind,
            "schema_version": 0,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            **fields,
        }
        self._local_receipts.append(record)
        if self._external_receipt_sink is not None:
            self._external_receipt_sink(dict(record))
        return dict(record)
