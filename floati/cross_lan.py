"""Opt-in direct LAN transport. Discovery is limited to explicit destinations."""

from __future__ import annotations

import ipaddress
import select
import socket
from dataclasses import dataclass, field
from hashlib import sha256
from hmac import compare_digest
from secrets import token_bytes, token_hex
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import ContextManager, Mapping, Protocol

from .cross_authority import Capability
from .cross_coordinates import MAX_INTEGER
from .cross_gate import GateImage, GateOwner, replace_gate
from .cross_journal import AuthenticatedOpen, LocalDial, Storage
from .cross_lan_tls import (
    LanPairChannel as LanPairChannel, SilentClose, TlsIdentity as TlsIdentity,
    TlsRuntime as TlsRuntime, TlsStream, raw_receive, raw_send, read_exact, remaining,
)
from .cross_pair_codes import direction_key, gate_prefix
from .cross_pair_storage import require_gate_owner
from .errors import FloatiError, ProtocolRefusal
from .framing import decode_frames


_JUMP = 1 << 20
_MAX_FRAME = 256 * 1024


@dataclass(frozen=True)
class LanAddress:
    host: str
    port: int

    def __post_init__(self):
        if (not isinstance(self.host, str) or not self.host or self.host.startswith("-")
            or any(character.isspace() or ord(character) < 33 for character in self.host)
            or type(self.port) is not int or not 0 <= self.port <= 65535):
            raise ProtocolRefusal("cross_lan_address_invalid", "Choose an explicit LAN address and port.")


@dataclass(frozen=True)
class GatePair:
    owner: GateOwner
    role: str
    root: bytes = field(repr=False)
    identity: TlsIdentity
    target: LanAddress | None
    inbound_max: int
    outbound_min_next: int
    inbound_key: bytes = field(init=False, repr=False)
    outbound_key: bytes = field(init=False, repr=False)

    def __post_init__(self):
        self.owner.fields()
        if self.role not in {"S", "T"} or not isinstance(self.root, bytes) or len(self.root) != 32:
            raise _gate_unproved()
        if (type(self.inbound_max) is not int or not 0 <= self.inbound_max <= MAX_INTEGER
            or type(self.outbound_min_next) is not int or not 1 <= self.outbound_min_next <= MAX_INTEGER):
            raise _gate_unproved()
        pair_id = bytes.fromhex(self.owner.pair_id)
        object.__setattr__(self, "inbound_key", direction_key(self.root, pair_id, self.inbound))
        object.__setattr__(self, "outbound_key", direction_key(self.root, pair_id, self.outbound))

    @property
    def outbound(self) -> str:
        return "S-to-T" if self.role == "S" else "T-to-S"

    @property
    def inbound(self) -> str:
        return "T-to-S" if self.role == "S" else "S-to-T"

@dataclass(frozen=True)
class GateState:
    """Settled root and active pair materials, projected under the cross lock.

    The authority checks the independent anchor, config, active pair, item and
    shutdown fence. Maxima come from live and carried journal evidence.
    """

    storage: Storage
    pairs: tuple[GatePair, ...]

    def image(self) -> GateImage:
        raw = self.storage.read("gate-state")
        if raw is None or self.storage.read(".gate-state.tmp") is not None:
            raise _gate_unproved()
        image = GateImage.decode(raw)
        if len(self.pairs) > 64 or len({pair.owner.pair_id for pair in self.pairs}) != len(self.pairs):
            raise _gate_unproved()
        for pair in self.pairs:
            _, inbound, _ = require_gate_owner(image, pair.owner, pair.inbound)
            outbound, _, _ = require_gate_owner(image, pair.owner, pair.outbound)
            if inbound < pair.inbound_max or outbound < pair.outbound_min_next:
                raise _gate_unproved()
        return image


def _gate_unproved():
    return ProtocolRefusal("cross_gate_ownership_unproved", "The current LAN gate could not be verified.")


class LanAuthority(Protocol):
    """Local composition seam. Gate transactions share the journal's cross lock."""

    def gate_transaction(self) -> ContextManager[GateState]: ...
    def current_gate(self, *, existing_open: bool) -> GateState: ...
    def bind_allowed(self, address: LanAddress) -> bool: ...
    def register_listener(self, approved: LanAddress, bound: LanAddress) -> str: ...
    def listener_current(self, address: LanAddress, endpoint_id: str) -> bool: ...
    def begin_local_dial(self, capability: Capability, record_id: str, sender: str) -> LocalDial: ...
    def dial_current(self, dial: LocalDial) -> bool: ...
    def discard_local_dial(self, dial: LocalDial) -> None: ...
    def open_authenticated(self, proof: Mapping[str, object], stream: object,
                           *, dial: LocalDial | None = None) -> AuthenticatedOpen: ...
    def close_authenticated(self, opened: AuthenticatedOpen) -> None: ...


def dial_target(pair: GatePair) -> LanAddress:
    target = pair.target
    if target is None or target.host == "any" or not target.port:
        raise ProtocolRefusal("cross_lan_address_invalid", "Choose an explicit LAN address and port.")
    return target


def _tcp_address(address: LanAddress):
    family, _, _, _, target = socket.getaddrinfo(address.host, address.port, type=socket.SOCK_STREAM,
                                                 proto=socket.IPPROTO_TCP)[0]
    ip = ipaddress.ip_address(target[0])
    mapped = getattr(ip, "ipv4_mapped", None)
    if ip.is_unspecified or (mapped is not None and mapped.is_unspecified):
        raise ProtocolRefusal("cross_lan_address_invalid", "Choose an explicit LAN address and port.")
    return family, target


@dataclass(frozen=True, eq=False)
class _GateEvent:
    pair: GatePair
    side: str
    counter: int
    owner_id: str
    listener: LanListener | None = None

    @property
    def direction(self) -> str:
        return self.pair.inbound if self.side == "listener" else self.pair.outbound


@dataclass
class _Live:
    raw: socket.socket
    event: _GateEvent
    body: dict
    published: bool = False


class LanTransport:
    """One local lifecycle owner for a cross root. Construction opens no sockets."""

    def __init__(self, authority: LanAuthority, runtime: TlsRuntime):
        self._authority, self._runtime = authority, runtime
        self._dummy = tuple(token_bytes(32) for _ in range(64))
        self._unauthenticated = BoundedSemaphore(16)
        self._authenticated = BoundedSemaphore(8)
        self._live: dict[TlsStream, _Live] = {}
        self._guard = Lock()

    def listen(self, address: LanAddress) -> LanListener:
        self._runtime.require()
        with self._authority.gate_transaction() as gate:
            gate.image()
            self._require_bind(address)
        if address.host == "any":
            family, target = socket.AF_INET6, ("::", address.port)
        else:
            family, target = _tcp_address(address)
        with self._authority.gate_transaction() as gate:
            gate.image()
            self._require_bind(address)
            stream = socket.socket(family, socket.SOCK_STREAM)
            try:
                if address.host == "any":
                    stream.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                stream.bind(target)
            except BaseException:
                stream.close()
                raise
        try:
            bound = _bound_address(stream)
            endpoint = self._authority.register_listener(address, bound)
            listener = LanListener(self, stream, address, endpoint, bound)
            with self._authority.gate_transaction() as gate:
                gate.image()
                if not listener.current():
                    raise _gate_unproved()
                stream.listen(16)
                stream.setblocking(False)
            return listener
        except BaseException:
            stream.close()
            raise

    def _require_bind(self, address):
        if self._authority.bind_allowed(address) is not True:
            raise ProtocolRefusal("cross_lan_bind_unapproved", "Approve the exact LAN listener address first.")

    def _accept(self, listener: LanListener, raw: socket.socket, stream_id: str,
                accepted_at: float, deadline: float) -> LanSession | None:
        try:
            prefix = read_exact(lambda size, expiry: raw_receive(raw, size, expiry), 64,
                                min(deadline, accepted_at + 5))
            with self._authority.gate_transaction() as gate:
                if not listener.current():
                    return None
                event = self._match_prefix(gate, prefix, listener)
            if event is None:
                return None
            tls = TlsStream(raw, event.pair.identity, server=True, deadline=min(deadline, monotonic() + 10))
            return self._publish(raw, tls, event, stream_id)
        except (FloatiError, OSError, EOFError):
            # A pre-gate error has sent nothing on this TCP stream.
            return None

    def _match_prefix(self, gate: GateState, prefix: bytes, listener: LanListener) -> _GateEvent | None:
        counter = int.from_bytes(prefix[24:32], "big")
        if prefix[:24] != b"\x03" + bytes(23) or not 1 <= counter <= MAX_INTEGER:
            return None
        matches = []
        for slot, dummy in enumerate(self._dummy):
            pair = gate.pairs[slot] if slot < len(gate.pairs) else None
            key = dummy if pair is None else pair.inbound_key
            matched = compare_digest(gate_prefix(key, counter)[32:], prefix[32:])
            if matched:
                matches.append(pair)
        if len(matches) != 1 or matches[0] is None:
            return None
        pair = matches[0]
        image = gate.image()
        _, high_water, _ = require_gate_owner(image, pair.owner, pair.inbound)
        if high_water == MAX_INTEGER or counter <= high_water:
            return None
        successor = image.advance(pair.owner, pair.inbound, "in_hwm", counter)
        replace_gate(gate.storage, image, successor)
        return _GateEvent(pair, "listener", counter, listener.endpoint, listener)

    def dial(self, capability: Capability, record_id: str, sender: str, deadline: float) -> LanSession:
        self._runtime.require()
        jump_from = None
        for _ in range(2):
            remaining(deadline)
            attempt = self._authority.begin_local_dial(capability, record_id, sender)
            try:
                event, target = self._reserve(attempt, jump_from)
                return self._connect(event, target, attempt, deadline)
            except SilentClose:
                jump_from = event.counter
            finally:
                self._authority.discard_local_dial(attempt)
        raise ProtocolRefusal("cross_peer_silent_close", "The peer closed before sending TLS bytes.")

    def _reserve(self, attempt: LocalDial, jump_from: int | None) -> tuple[_GateEvent, LanAddress]:
        source, = decode_frames(attempt.outbound_frame)
        record, = decode_frames(attempt.attempt_frame)
        with self._authority.gate_transaction() as gate:
            if self._authority.dial_current(attempt) is not True:
                raise _gate_unproved()
            image = gate.image()
            pair = next((pair for pair in gate.pairs if pair.owner.pair_id == source["pair_id"]), None)
            if (pair is None or pair.outbound != source["direction"]
                or pair.owner.enable_id != source["enable_id"]
                or pair.owner.enable_generation != source["enable_generation"]):
                raise _gate_unproved()
            target = dial_target(pair)
            counter, _, _ = require_gate_owner(image, pair.owner, pair.outbound)
            if jump_from is not None:
                counter = max(counter, jump_from + _JUMP)
            if counter >= MAX_INTEGER:
                raise ProtocolRefusal("cross_gate_exhausted", "Unpair and pair again to renew the LAN counter.")
            successor = image.advance(pair.owner, pair.outbound, "out_next", counter + 1)
            replace_gate(gate.storage, image, successor)
        return _GateEvent(pair, "dialler", counter, record["id"]), target

    def _connect(self, event, target, attempt, deadline):
        try:
            family, address = _tcp_address(target)
            raw = socket.socket(family, socket.SOCK_STREAM)
            stream_id = token_hex(16)
        except OSError as exc:
            raise _unreachable() from exc
        handed_off = False
        try:
            raw.settimeout(remaining(deadline))
            raw.connect(address)
            try:
                raw_send(raw, gate_prefix(event.pair.outbound_key, event.counter), deadline)
            except (ConnectionResetError, BrokenPipeError) as exc:
                raise SilentClose from exc
            tls = TlsStream(raw, event.pair.identity, server=False, deadline=min(deadline, monotonic() + 10))
            session = self._publish(raw, tls, event, stream_id, dial=attempt)
            handed_off = True
            return session
        except OSError as exc:
            raise _unreachable() from exc
        finally:
            if not handed_off:
                raw.close()

    def _publish(self, raw, tls, event, stream_id, *, dial=None):
        if not self._authenticated.acquire(blocking=False):
            raise ProtocolRefusal("cross_lan_session_limit", "The local LAN session limit is reached.")
        owner = event.pair.owner
        body = {
            "id": "cross-open-" + stream_id, "kind": "remote_session_opened", "schema_version": 1,
            "enable_id": owner.enable_id, "enable_generation": owner.enable_generation, "pair_id": owner.pair_id,
            "local_side": event.side, "direction": event.direction, "gate_counter": event.counter,
            "owner_id": event.owner_id, "stream_id": stream_id, "peer_der_sha256": sha256(tls.peer_der()).hexdigest(),
        }
        try:
            live = _Live(raw, event, body)
            with self._guard:
                self._live[tls] = live
            opened = self._authority.open_authenticated(body.copy(), tls, dial=dial)
            with self._guard:
                live.published = True
            return LanSession(self, raw, tls, opened)
        except BaseException:
            self._forget(tls)
            raise

    def _forget(self, stream):
        with self._guard:
            live = self._live.pop(stream, None)
        if live is not None:
            self._authenticated.release()
        return live is not None

    def authenticate_open(self, proof: Mapping[str, object], stream: object) -> bool:
        """Journal callback. The caller holds the cross lock and checks publication/scope."""
        if not isinstance(stream, TlsStream):
            return False
        with self._guard:
            live = self._live.get(stream)
        if live is None or live.body != proof or live.raw.fileno() < 0:
            return False
        event = live.event
        if event.listener is not None and not event.listener.current():
            return False
        try:
            gate = self._authority.current_gate(existing_open=live.published)
            image = gate.image()
            pair = next((pair for pair in gate.pairs if pair.owner == event.pair.owner), None)
            if (pair is None or pair.role != event.pair.role or pair.target != event.pair.target
                or not compare_digest(pair.root, event.pair.root)
                or not compare_digest(pair.identity.local_der, event.pair.identity.local_der)
                or not compare_digest(pair.identity.peer_der, stream.peer_der() or b"")):
                return False
            outbound, inbound, _ = require_gate_owner(image, pair.owner, event.direction)
            return event.counter <= inbound if event.side == "listener" else event.counter < outbound
        except (FloatiError, OSError):
            return False


def _unreachable():
    return ProtocolRefusal("cross_transport_unreachable", "The selected LAN destination could not be reached.")


def _bound_address(stream: socket.socket) -> LanAddress:
    bound = stream.getsockname()
    host = bound[0]
    if len(bound) == 4 and bound[3] and "%" not in host:
        host = f"{host}%{bound[3]}"
    return LanAddress(host, bound[1])


class LanListener:
    def __init__(self, transport, stream, approved, endpoint, bound):
        self._transport, self._stream, self._approved, self.endpoint = transport, stream, approved, endpoint
        self.address = bound

    def current(self) -> bool:
        return (self._stream.fileno() >= 0
                and self._transport._authority.listener_current(self._approved, self.endpoint) is True)

    def accept(self, deadline: float) -> LanSession | None:
        """Serve one accepted connection. No application handler runs here."""
        while select.select([self._stream], [], [], remaining(deadline))[0]:
            try:
                raw, _ = self._stream.accept()
                break
            except BlockingIOError:
                continue
        else:
            return None
        accepted_at = monotonic()
        if not self._transport._unauthenticated.acquire(blocking=False):
            raw.close()
            return None
        session = None
        try:
            session = self._transport._accept(self, raw, token_hex(16), accepted_at, deadline)
            return session
        finally:
            if session is None:
                raw.close()
            self._transport._unauthenticated.release()

    def close(self) -> None:
        self._stream.close()


class LanSession:
    """Private handoff to the local stream owner after a published open.

    The owner must route frames through the journal's scoped ingress and
    local output APIs and serialize socket I/O through this owner. This
    transport does not interpret or dispatch frames.
    """

    def __init__(self, transport, raw, stream, opened):
        self._transport, self._raw, self._stream = transport, raw, stream
        self.opened, self.tls = opened, stream.evidence
        now = monotonic()
        self._ends = now + 1800
        self._idle = now + 60

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _deadline(self, deadline: float) -> float:
        return min(deadline, self._ends, self._idle, monotonic() + 10)

    def _receive_frame(self, deadline: float) -> bytes:
        try:
            size = int.from_bytes(read_exact(self._stream.receive, 4, self._deadline(deadline)), "big")
            if not 0 < size <= _MAX_FRAME:
                raise ProtocolRefusal("cross_lan_frame_invalid", "The LAN frame exceeds the frame bounds.")
            payload = read_exact(self._stream.receive, size, self._deadline(deadline))
            self._idle = monotonic() + 60
            return payload
        except (FloatiError, OSError, EOFError):
            self.close()
            raise

    def _send_frame(self, payload: bytes, deadline: float) -> None:
        if not 0 < len(payload) <= _MAX_FRAME:
            raise ProtocolRefusal("cross_lan_frame_invalid", "The LAN frame exceeds the frame bounds.")
        try:
            self._stream.send(len(payload).to_bytes(4, "big") + payload, self._deadline(deadline))
            self._idle = monotonic() + 60
        except (OSError, EOFError):
            self.close()
            raise

    def close(self) -> None:
        self._raw.close()
        if self._transport._forget(self._stream):
            self._transport._authority.close_authenticated(self.opened)
