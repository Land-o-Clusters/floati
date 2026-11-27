"""Attended pair-show and pair-join over an injected authenticated channel."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from hmac import compare_digest
import json
import re
from secrets import token_bytes
from time import monotonic
from typing import Callable, ContextManager, Optional, Protocol

from . import cross_pair_codes as codes
from .cross_panel import PanelLifecycle
from .errors import FloatiError, ProtocolRefusal
from .framing import FrameError, decode_frames


_MAX_MESSAGE = 16 * 1024
_POLICY = int.from_bytes(codes.POLICY, "big")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_DESCRIPTOR = {"v", "enable_id", "salt", "label_hint", "lan", "ssh_launcher"}
_CLASSES = frozenset({"host_key_verification_failed", "permission_denied", "connection_refused",
                      "connection_timed_out", "could_not_resolve", "other"})


class PairChannel(Protocol):
    """One selected stream. Authentication proves both leaves on that stream."""

    def send(self, payload: bytes, deadline: float) -> None: ...
    def receive(self, deadline: float) -> bytes: ...
    def authenticate(self, *, role: str, local_der: bytes, peer_der: bytes, deadline: float) -> bytes: ...
    def close(self) -> None: ...


class PairSurface(Protocol):
    """A selected local input/output route with no managed history or echo."""

    isolated: bool

    def clear(self) -> None: ...
    def draw(self, display: str) -> None: ...
    def read_code(self, deadline: float) -> str: ...


class PairOpening(Protocol):
    opening_id: str
    token: str
    enable_id: str
    enable_generation: int


class PairBackend(Protocol):
    """Each effect is scoped to a local opening, independent of its wire ID."""

    def exclusive(self) -> ContextManager: ...
    def open(self, role: str) -> PairOpening: ...
    def stage(self, opening: PairOpening) -> bytes: ...
    def check_candidate(self, opening: PairOpening, pair_id: str, label: str) -> None: ...
    def activate(self, opening: PairOpening, activation: Activation, gate_root: bytes, stream: PairChannel) -> None: ...
    def deposit_final(self, opening: PairOpening, output: FinalOutput) -> None: ...
    def observe(self, opening: PairOpening, activation: Activation) -> None: ...
    def finish(self, opening: PairOpening) -> PairOutcome: ...


@dataclass(frozen=True)
class Activation:
    opening_id: str
    wire_id: str
    pair_id: str
    role: str
    label: str
    peer_der: bytes = field(repr=False)
    peer_descriptor: bytes = field(repr=False)
    transcript_receipt_digest: str
    rendered_digest: str
    allowlist: tuple
    wake_list: tuple
    local_der_sha256: str
    policy: int = _POLICY

    @property
    def peer_sha256(self) -> str:
        return sha256(self.peer_der).hexdigest()


class FinalOutput:
    """One nonblocking deposit, bound to the original stream and activation."""

    def __init__(self, stream: PairChannel, activation: Activation, proof: bytes):
        self._stream = stream
        self._activation = activation
        if activation.role not in ("S", "T") or type(proof) is not bytes or len(proof) != 32:
            raise _protocol()
        _hex(activation.wire_id, 16)
        _hex(activation.pair_id, 16)
        role = activation.role
        self._payload = codes.canonical({
            "t": "final_" + role, "ceremony_id": activation.wire_id, "pair_id": activation.pair_id,
            "kc_" + role: proof.hex(),
        })
        self._queued = False
        self._spent = False

    @property
    def stream(self) -> PairChannel:
        return self._stream

    @property
    def activation(self) -> Activation:
        return self._activation

    @property
    def queued(self) -> bool:
        return self._queued

    def enqueue(self) -> None:
        if self._queued or self._spent:
            raise _protocol()
        self._queued = True

    def flush(self, deadline: float) -> bool:
        if not self._queued or self._spent:
            return False
        self._spent = True
        self.stream.send(self._payload, deadline)
        return True


@dataclass(frozen=True)
class Diagnostic:
    classification: str
    exit_status: Optional[int]
    signal: Optional[int]

    def __post_init__(self):
        exit_valid = type(self.exit_status) is int and 0 <= self.exit_status <= 255
        signal_valid = type(self.signal) is int and 1 <= self.signal <= 255
        if (type(self.classification) is not str or self.classification not in _CLASSES
            or not ((self.exit_status is None and self.signal is None)
                    or (exit_valid and self.signal is None) or (signal_valid and self.exit_status is None))):
            raise ProtocolRefusal("cross_transport_diagnostic_invalid", "The transport outcome is unavailable.")

    def record(self) -> dict:
        return {"class": self.classification, "exit_status": self.exit_status, "signal": self.signal}


def _transport_diagnostic(value) -> Diagnostic:
    if type(value) is Diagnostic:
        try:
            return Diagnostic(value.classification, value.exit_status, value.signal)
        except (AttributeError, ProtocolRefusal):
            pass
    return Diagnostic("other", None, None)


class TransportFailure(Exception):
    def __init__(self, diagnostic: Diagnostic):
        super().__init__("The pairing channel closed.")
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class PairOutcome:
    state: str
    pair_id: Optional[str]
    message: str
    remedy: Optional[str] = None
    diagnostic: Optional[Diagnostic] = None
    peer_reported_commit: bool = False


_SCREENS = {
    "absent": "Nothing was paired here.",
    "unknown": "Pairing here could not be verified.",
    "unconfirmed": "Paired here. No confirmation was recorded here.",
    "seen": "Paired here. The other machine proved possession during pairing.",
    "reported": "Paired here. The other machine reported its commit.",
}


def outcome(state: str, pair_id: Optional[str] = None) -> PairOutcome:
    remedy = None
    if state == "unconfirmed":
        _hex(pair_id, 16)
        remedy = f"floati cross unpair --pair-id {pair_id} --idempotency-key {pair_id}"
    return PairOutcome(state, pair_id, _SCREENS[state], remedy)


def panel_field(value: str) -> str:
    """Keep complete escape sequences within the terminal field limit."""
    pieces = []
    remaining = 157
    for character in value:
        escaped = json.dumps(character, ensure_ascii=True)[1:-1]
        if len(escaped) > remaining:
            return "".join(pieces) + "..."
        pieces.append(escaped)
        remaining -= len(escaped)
    return "".join(pieces)


def _protocol():
    return ProtocolRefusal("cross_pairing_protocol", "The pairing message did not match this step.")


def _hex(value, size: Optional[int] = None) -> bytes:
    if (not isinstance(value, str) or not value or len(value) % 2
        or (size is not None and len(value) != size * 2) or not re.fullmatch("[0-9a-f]+", value)):
        raise _protocol()
    return bytes.fromhex(value)


def _descriptor(value) -> dict:
    if not isinstance(value, dict) or value.keys() != _DESCRIPTOR or type(value["v"]) is not int or value["v"] != 5:
        raise _protocol()
    _hex(value["enable_id"], 16)
    _hex(value["salt"], 16)
    lan = value["lan"]
    if (not isinstance(lan, list) or len(lan) > 16 or any(not isinstance(item, str) or len(item) > 256 for item in lan)
        or not isinstance(value["label_hint"], str) or len(value["label_hint"]) > 256
        or not isinstance(value["ssh_launcher"], str) or len(value["ssh_launcher"]) > 512):
        raise _protocol()
    return value


def _contribution(body: dict) -> codes.Contribution:
    return codes.Contribution(_hex(body["cert_der"]), _hex(body["desc_digest"], 32), _hex(body["nonce"], 32))


def _opened(value: codes.Contribution) -> dict:
    return dict(cert_der=value.leaf.hex(), desc_digest=value.descriptor_digest.hex(), nonce=value.nonce.hex())


class _Exchange:
    def __init__(self, channel: PairChannel, expires: float, clock):
        self.channel = channel
        self.expires = expires
        self.clock = clock

    def deadline(self, seconds=30) -> float:
        deadline = min(self.expires, self.clock() + seconds)
        self.check(deadline)
        return deadline

    def check(self, deadline: float) -> None:
        if self.clock() >= deadline:
            raise TimeoutError

    def send(self, kind: str, **fields) -> None:
        payload = codes.canonical(dict(fields, t=kind))
        if len(payload) > _MAX_MESSAGE:
            raise _protocol()
        deadline = self.deadline()
        self.channel.send(payload, deadline)
        self.check(deadline)

    def receive(self, kind: str, *keys: str, seconds=30) -> dict:
        deadline = self.deadline(seconds)
        payload = self.channel.receive(deadline)
        self.check(deadline)
        if not isinstance(payload, bytes) or len(payload) > _MAX_MESSAGE:
            raise _protocol()
        try:
            rows = decode_frames(payload + b"\n")
            if len(rows) == 1 and isinstance(rows[0], dict):
                row = rows[0]
                if row.keys() == {"t", *keys} and row["t"] == kind and codes.canonical(row) == payload:
                    return row
        except (FrameError, ValueError, RecursionError):
            pass
        raise _protocol()

    def authenticate(self, role: str, own: bytes, peer: bytes) -> None:
        deadline = self.deadline()
        actual = self.channel.authenticate(role=role, local_der=own, peer_der=peer, deadline=deadline)
        self.check(deadline)
        if not isinstance(actual, bytes) or not compare_digest(peer, actual):
            raise ProtocolRefusal("cross_pairing_possession", "The channel did not prove the exchanged leaf.")


@dataclass
class _Testimony:
    peer_reported: bool = False


class Pairing:
    def __init__(self, backend: PairBackend, panels: Optional[PanelLifecycle] = None, *, clock=monotonic):
        self.backend = backend
        self.clock = clock
        self.panels = panels if panels is not None else PanelLifecycle()

    def run(self, role: str, surface: PairSurface, connect: Callable[[float], PairChannel], *,
            label: str, address: str, label_hint: str, lan=(), ssh_launcher="", allowlist=(), wake_list=()) -> PairOutcome:
        if role not in ("S", "T") or not isinstance(label, str) or not _LABEL.fullmatch(label):
            raise ProtocolRefusal("cross_pair_label_invalid", "Choose a local label of up to 64 letters, digits, or ._-.")
        with self.backend.exclusive():
            attempt = self.panels.begin(surface, role)
            try:
                expires = self.clock() + 600
                opening = self.backend.open(role)
                channel = None
                diagnostic = None
                testimony = _Testimony()
                try:
                    leaf = self.backend.stage(opening)
                    descriptor = _descriptor(dict(v=5, enable_id=opening.enable_id, salt=token_bytes(16).hex(),
                                                  label_hint=label_hint, lan=list(lan), ssh_launcher=ssh_launcher))
                    window = min(expires, self.clock() + 120)
                    channel = connect(window)
                    exchange = _Exchange(channel, expires, self.clock)
                    exchange.check(window)
                    self._compare_and_commit(exchange, role, opening, leaf, descriptor,
                                             attempt, surface, label, address, tuple(allowlist), tuple(wake_list),
                                             testimony)
                except TransportFailure as exc:
                    diagnostic = _transport_diagnostic(getattr(exc, "diagnostic", None))
                except (FloatiError, EOFError, OSError):
                    # Outcome comes from settled local evidence, never from a channel exception.
                    pass
                finally:
                    if channel is not None:
                        try:
                            channel.close()
                        except OSError:
                            pass
                try:
                    result = self.backend.finish(opening)
                except (FloatiError, OSError):
                    result = outcome("unknown")
                self.panels.end(attempt)
                lines = [result.message]
                if testimony.peer_reported and result.state in {"absent", "unknown"}:
                    lines.append("The other machine reported its commit.")
                if result.pair_id is not None:
                    _hex(result.pair_id, 16)
                    lines.append(f"Pair {result.pair_id}")
                if result.remedy:
                    lines.append(result.remedy)
                surface.draw("\n".join(lines))
                return replace(result, diagnostic=diagnostic, peer_reported_commit=testimony.peer_reported)
            finally:
                self.panels.end(attempt)

    def _compare_and_commit(self, exchange, role, opening, leaf, descriptor, attempt, surface,
                            label, address, allowlist, wake_list, testimony):
        descriptor_hash = codes.descriptor_digest(descriptor)
        if role == "S":
            wire_id = token_bytes(16)
            own = codes.Contribution(leaf, descriptor_hash, token_bytes(32))
            exchange.send("commit", ceremony_id=wire_id.hex(), policy=_POLICY, commitment=codes.commitment(wire_id, own).hex())
            row = exchange.receive("contribute", "policy", "cert_der", "desc_digest", "nonce")
            if type(row["policy"]) is not int or row["policy"] != _POLICY:
                raise _protocol()
            peer = _contribution(row)
            exchange.send("open", **_opened(own))
        else:
            row = exchange.receive("commit", "ceremony_id", "policy", "commitment")
            if type(row["policy"]) is not int or row["policy"] != _POLICY:
                raise _protocol()
            wire_id = _hex(row["ceremony_id"], 16)
            commitment = _hex(row["commitment"], 32)
            own = codes.Contribution(leaf, descriptor_hash, token_bytes(32))
            exchange.send("contribute", policy=_POLICY, **_opened(own))
            peer = _contribution(exchange.receive("open", "cert_der", "desc_digest", "nonce"))
            if not compare_digest(commitment, codes.commitment(wire_id, peer)):
                raise _protocol()
        exchange.authenticate(role, leaf, peer.leaf)
        own_share = token_bytes(32)
        exchange.send("descriptor", desc=descriptor, share=own_share.hex())
        row = exchange.receive("descriptor", "desc", "share")
        peer_descriptor = _descriptor(row["desc"])
        if not compare_digest(peer.descriptor_digest, codes.descriptor_digest(peer_descriptor)):
            raise _protocol()
        if peer_descriptor["enable_id"] == opening.enable_id:
            raise ProtocolRefusal("cross_pairing_self", "This machine is the selected peer.")
        peer_share = _hex(row["share"], 32)
        if role == "S":
            derived = codes.comparisons(wire_id, own, peer)
            root = codes.gate_root(derived.t, own_share, peer_share)
        else:
            derived = codes.comparisons(wire_id, peer, own)
            root = codes.gate_root(derived.t, peer_share, own_share)
        self.backend.check_candidate(opening, derived.pair_id, label)

        screen = f"Pairing {derived.tag}\nAddress {panel_field(address)}\nPeer {panel_field(peer_descriptor['label_hint'])}"
        if role == "S":
            screen += f"\nCompare code {derived.display_a}\nEnter the return code."
        else:
            screen += "\nEnter the comparison code."
        expected = derived.code_b if role == "S" else derived.code_a
        enter = self.panels.single_entry(attempt, lambda value: codes.compare(value, expected, role))
        surface.draw(screen)
        deadline = exchange.deadline()
        entered = surface.read_code(deadline)
        exchange.check(deadline)
        if not enter(entered):
            raise ProtocolRefusal("cross_pairing_mismatch", "The comparison code did not match.")

        if role == "T":
            self.panels.render_b(attempt, f"Return code {derived.code_b}\nNothing is paired yet.")
            self._receive_final(exchange, "S", wire_id, derived, root, seconds=30)
            testimony.peer_reported = True
        template = "pair-show-v1" if role == "S" else "pair-join-v1"
        activation = Activation(opening.opening_id, wire_id.hex(), derived.pair_id, role, label, peer.leaf,
                                codes.canonical(peer_descriptor), codes.receipt_digest(derived.t).hex(),
                                codes.panel_digest(template, derived.t), allowlist, wake_list, sha256(leaf).hexdigest())
        self.backend.activate(opening, activation, root, exchange.channel)
        output = FinalOutput(exchange.channel, activation, codes.confirmation(root, role, derived.t))
        self.backend.deposit_final(opening, output)
        if not output.flush(exchange.deadline()):
            return
        if role == "S":
            self._receive_final(exchange, "T", wire_id, derived, root, seconds=10)
            self.backend.observe(opening, activation)

    @staticmethod
    def _receive_final(exchange, role, wire_id, derived, root, *, seconds):
        field = "kc_" + role
        row = exchange.receive("final_" + role, "ceremony_id", "pair_id", field, seconds=seconds)
        if (row["ceremony_id"] != wire_id.hex() or row["pair_id"] != derived.pair_id
            or not compare_digest(_hex(row[field], 32), codes.confirmation(root, role, derived.t))):
            raise _protocol()
