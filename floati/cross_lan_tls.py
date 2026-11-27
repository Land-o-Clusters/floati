"""The TLS 1.3 byte stream shared by LAN sessions and attended pairing."""

from __future__ import annotations

import re
import ssl
import sys
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from time import monotonic
from typing import Protocol

from .errors import FloatiError, ProtocolRefusal


class _Stream(Protocol):
    def settimeout(self, timeout: float) -> None: ...
    def recv(self, size: int) -> bytes: ...
    def sendall(self, payload: bytes) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class TlsRuntime:
    interpreter: str
    library: str
    tls13: bool

    @classmethod
    def current(cls) -> TlsRuntime:
        return cls(str(Path(sys.executable).resolve()), ssl.OPENSSL_VERSION, ssl.HAS_TLSv1_3)

    def require(self) -> None:
        if self != self.current() or not self.tls13 or not ssl.OP_NO_TICKET:
            raise ProtocolRefusal(
                "cross_runtime_unsupported",
                "Set FLOATI_PYTHON to the qualified TLS 1.3 interpreter.",
            )


@dataclass(frozen=True)
class TlsIdentity:
    """Exact, privately owned leaf and key inputs held stable by the material owner."""

    cert: Path = field(repr=False)
    key: Path = field(repr=False)
    local_der: bytes = field(repr=False)
    peer_der: bytes = field(repr=False)
    password: bytes | None = field(default=None, repr=False)

    def context(self, *, server: bool) -> ssl.SSLContext:
        # A chain appended to the owned leaf must never be sent to a peer.
        pem = self.cert.read_text(encoding="ascii").strip()
        if pem.count("-----BEGIN CERTIFICATE-----") != 1 or not compare_digest(
            ssl.PEM_cert_to_DER_cert(pem), self.local_der
        ):
            raise _failure("LOCAL_LEAF_UNPROVED")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        ctx.options |= ssl.OP_NO_TICKET
        if server:
            ctx.num_tickets = 0
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(self.peer_der))
        # An unavailable password must refuse without prompting on the service's stdin.
        ctx.load_cert_chain(self.cert, self.key, password=self.password or b"")
        return ctx


@dataclass(frozen=True)
class TlsEvidence:
    version: str
    cipher: str
    group: str = "unknown"


class SilentClose(EOFError):
    """EOF or reset before a client has received any TLS byte."""


def _failure(reason: str) -> ProtocolRefusal:
    return ProtocolRefusal("cross_tls_handshake_failed", f"TLS handshake failed ({reason}).")


def remaining(deadline: float) -> float:
    seconds = deadline - monotonic()
    if seconds <= 0:
        raise TimeoutError
    return seconds


def read_exact(receive, size: int, deadline: float) -> bytes:
    result = bytearray()
    while len(result) < size:
        part = receive(size - len(result), deadline)
        if not part:
            raise EOFError
        result.extend(part)
    return bytes(result)


def raw_receive(stream: _Stream, size: int, deadline: float) -> bytes:
    stream.settimeout(remaining(deadline))
    return stream.recv(size)


def raw_send(stream: _Stream, payload: bytes, deadline: float) -> None:
    stream.settimeout(remaining(deadline))
    stream.sendall(payload)


class TlsStream:
    def __init__(self, stream: _Stream, identity: TlsIdentity, *, server: bool, deadline: float):
        self._stream = stream
        self._incoming, self._outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._received = False
        self._eof = False
        try:
            ctx = identity.context(server=server)
            self._tls = ctx.wrap_bio(self._incoming, self._outgoing, server_side=server)
            self._run(self._tls.do_handshake, deadline)
        except (EOFError, ConnectionResetError, BrokenPipeError) as exc:
            if not server and not self._received:
                raise SilentClose from exc
            raise _failure("UNEXPECTED_EOF") from exc
        except ssl.SSLError as exc:
            reason = getattr(exc, "reason", None)
            raise _failure(reason if reason and re.fullmatch(r"[A-Z0-9_]+", reason) else "UNKNOWN") from exc
        except (OSError, ValueError) as exc:
            raise _failure("LOCAL_IO_OR_CONFIGURATION") from exc

        cert = self.peer_der()
        if cert is None:
            raise _failure("PEER_LEAF_ABSENT")
        if not compare_digest(cert, identity.peer_der):
            raise ProtocolRefusal("cross_pin_mismatch", "The verified TLS leaf differs from the paired leaf.")
        version, cipher = self._tls.version(), self._tls.cipher()
        if version != "TLSv1.3" or self._tls.session_reused or not cipher:
            raise _failure("TLS_PROFILE_REFUSED")
        self.evidence = TlsEvidence(version, cipher[0])

    def _flush(self, deadline):
        while self._outgoing.pending:
            raw_send(self._stream, self._outgoing.read(), deadline)

    def _run(self, operation, deadline):
        while True:
            remaining(deadline)
            try:
                result = operation()
            except ssl.SSLWantReadError:
                self._flush(deadline)
                if self._eof:
                    raise EOFError
                data = raw_receive(self._stream, 16384, deadline)
                if not data:
                    self._eof = True
                    self._incoming.write_eof()
                    if not self._received:
                        raise EOFError
                else:
                    self._received = True
                    self._incoming.write(data)
            except ssl.SSLError:
                # An alert already generated by the TLS library is safe to send.
                try:
                    self._flush(deadline)
                except OSError:
                    pass
                raise
            else:
                self._flush(deadline)
                return result

    def peer_der(self) -> bytes | None:
        return self._tls.getpeercert(binary_form=True)

    def receive(self, size: int, deadline: float) -> bytes:
        return self._run(lambda: self._tls.read(size), deadline)

    def send(self, payload: bytes, deadline: float) -> None:
        view = memoryview(payload)
        while view:
            used = self._run(lambda: self._tls.write(view), deadline)
            view = view[used:]


class LanPairChannel:
    """PairChannel over one selected TCP stream, with an in-place TLS upgrade."""

    def __init__(self, stream: _Stream, role: str, cert: Path, key: Path, runtime: TlsRuntime,
                 *, password: bytes | None = None):
        runtime.require()
        self._stream, self._role, self._runtime = stream, role, runtime
        self._cert, self._key, self._password = cert, key, password
        self._secure: TlsStream | None = None
        self._authentication_started = False

    def _use(self):
        if self._authentication_started and self._secure is None:
            raise EOFError
        return self._secure

    def send(self, payload: bytes, deadline: float) -> None:
        if not 0 < len(payload) <= 16384:
            raise ProtocolRefusal("cross_pairing_protocol", "The pairing message exceeds the frame bounds.")
        frame = len(payload).to_bytes(4, "big") + payload
        secure = self._use()
        if secure is None:
            raw_send(self._stream, frame, deadline)
        else:
            secure.send(frame, deadline)

    def receive(self, deadline: float) -> bytes:
        secure = self._use()

        def receive(size, expiry):
            if secure is None:
                return raw_receive(self._stream, size, expiry)
            return secure.receive(size, expiry)

        size = int.from_bytes(read_exact(receive, 4, deadline), "big")
        if not 0 < size <= 16384:
            raise ProtocolRefusal("cross_pairing_protocol", "The pairing message exceeds the frame bounds.")
        return read_exact(receive, size, deadline)

    def authenticate(self, *, role: str, local_der: bytes, peer_der: bytes, deadline: float) -> bytes:
        if role not in {"S", "T"} or role != self._role or self._authentication_started:
            self.close()
            raise ProtocolRefusal("cross_pairing_possession", "Pairing could not verify the selected stream.")
        self._runtime.require()
        self._authentication_started = True
        identity = TlsIdentity(self._cert, self._key, local_der, peer_der, self._password)
        try:
            self._secure = TlsStream(self._stream, identity, server=role == "S",
                                     deadline=min(deadline, monotonic() + 10))
        except (FloatiError, OSError, EOFError):
            self.close()
            raise
        return self._secure.peer_der()

    def close(self) -> None:
        self._stream.close()
