"""Loopback X12 and authenticated LAN lifecycle fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest import TestCase, mock
import socket
import ssl
import subprocess

from floati.cross_authority import Capability
from floati.cross_attended import approved_completion, approved_start
from floati.cross_gate import GateImage, GateOwner, replace_gate
from floati.cross_journal import CrossJournal, Storage
from floati.cross_pair_codes import direction_key, gate_prefix
from floati.cross_pair_storage import Opening, require_gate_owner
from floati.errors import ProtocolRefusal
from tests.cross_pair_fixtures import FixtureAnchor, seed_activation
from tests.test_cross_journal import ENABLE_ONE, _OwnedEnableInventory

_PAIR = "a" * 32
_ENABLE = ENABLE_ONE
_OWNER = GateOwner(f"cross-{_ENABLE}-g1", _ENABLE, 1, "c" * 32, _PAIR)
_ROOT = b"\x19" * 32
_SEND = Capability("L", _PAIR, verb="send")


def _deadline():
    return monotonic() + 4


def _has_tls13_runtime():
    return ssl.HAS_TLSv1_3 and bool(ssl.OP_NO_TICKET)


def _leaf(directory, label):
    key, cert = directory / (label + ".key"), directory / (label + ".pem")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-subj", "/CN=floati", "-days", "2", "-config", str(directory / "openssl.cnf"),
         "-extensions", "leaf", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    return cert, key, ssl.PEM_cert_to_DER_cert(cert.read_text())


class _Authority:
    """Fixture authority. Gate replacement and all socket bytes use production code."""
    def __init__(self, directory, pair):
        from floati.cross_lan import GateState

        directory.mkdir()
        self.storage = Storage(directory)
        self.storage.create("gate-state", GateImage.empty(_OWNER.journal_id).provision(_OWNER).encoded())
        self.state = GateState(self.storage, (pair,))
        self.pair = pair
        self.enabled = True
        self.endpoint = None
        self.attempts = set()
        self.opens = []
        self.before_open = lambda: None
        start = approved_start(
            _ENABLE, 1, "e" * 32,
            {"listen": ["127.0.0.1:0"], "root": str(directory), "store": "keychain", "unit": "none"},
            schema_version=2,
        )
        self.journal = CrossJournal(
            directory, storage=self.storage,
            anchor_store=FixtureAnchor(), enable_inventory=_OwnedEnableInventory(),
            enable_approval=lambda phase, body: body == start,
            prove_pair_material=lambda record, *, phase: (
                record["peer_der"] == pair.identity.peer_der.hex() and record["role"] == pair.role
            ),
            verify_endpoint=lambda record, owner: "bound" if owner is self else "pending",
            authenticate_open=lambda proof, stream: self.transport.authenticate_open(proof, stream),
            authorize_local_send=lambda cap, sender: cap == _SEND and sender == "sender",
            dial_prerequisites=lambda cap, outbound: self.enabled and self.pair.target is not None,
        )
        self.journal.create(_ENABLE, attended=start)
        assert self.journal.append(Capability("A", verb="enable"), approved_completion(start)).status == "committed"
        opening = Opening("d" * 32, _OWNER.ceremony_token, _ENABLE, 1, pair.role)
        opened = dict(opening.scope(), kind="cross_ceremony_opened", schema_version=1,
                      id=f"cross-ceremony-opened-{opening.opening_id}")
        assert self.journal.append(opening.capability(), opened).status == "committed"
        seed_activation(self.journal, opening, _PAIR, "peer",
                        local_der=pair.identity.local_der, peer_der=pair.identity.peer_der)
        self.outbound = None
        if pair.target is not None:
            request = {"verb": "send", "envelope_fields": {
                "v": 5, "kind": "message", "message_class": "normal", "pair_id": _PAIR,
                "direction": "S-to-T", "sender_node": "sender", "recipient_node": "peer",
                "reply_to": None, "repo": None, "sha": None, "doc": None, "note": "local request",
            }}
            self.outbound = self.journal.allocate_outbound(_SEND, "first", request, "2026-11-25T00:00:00.000Z")
            if self.outbound.status != "committed":
                raise AssertionError("fixture sender item did not publish")

    def current_gate(self, *, existing_open=False):
        if not self.enabled:
            raise ProtocolRefusal("cross_disabled", "fixture disabled")
        return self.state

    def lock(self):
        return self.journal._lock(exclusive=True)

    @contextmanager
    def gate_transaction(self):
        with self.lock():
            yield self.current_gate()

    def bind_allowed(self, address):
        return self.enabled and address.host == "127.0.0.1"

    def register_listener(self, approved, bound):
        if not self.bind_allowed(approved) or bound.host != "127.0.0.1":
            raise AssertionError("fixture bound outside loopback")
        binding = dict(approved={"host": approved.host, "port": approved.port},
                       bound={"host": bound.host, "port": bound.port}, dual_stack=False,
                       pid=1, start_token="fixture", socket_token="f" * 32)
        result = self.journal.register_listener(Capability("I", verb="endpoint_start"), "9" * 32, binding, owner=self)
        if result.status != "committed":
            raise AssertionError("fixture endpoint did not publish")
        self.endpoint = approved, result.record_id
        return result.record_id

    def listener_current(self, address, endpoint_id):
        return self.endpoint == (address, endpoint_id)

    def begin_local_dial(self, capability, record_id, sender):
        attempt = self.journal.begin_local_dial(capability, record_id, sender)
        self.attempts.add(attempt)
        return attempt

    def dial_current(self, dial):
        return self.enabled and dial in self.attempts

    def discard_local_dial(self, dial):
        with self.lock():
            self.attempts.discard(dial)
            # This capability has no public journal cancellation API yet.
            self.journal._pending_dials.discard(dial)

    def dial(self):
        return self.transport.dial(_SEND, self.outbound.record_id, "sender", _deadline())

    def open_authenticated(self, proof, stream, *, dial=None):
        self.before_open()
        opened = self.journal.open_authenticated(proof, stream, dial=dial)
        self.opens.append((proof.copy(), opened))
        return opened

    def close_authenticated(self, opened):
        self.journal.close_authenticated(opened)


class CrossLanTests(TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        (cls.directory / "openssl.cnf").write_text(
            "[req]\ndistinguished_name=dn\n[dn]\n[leaf]\n"
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=serverAuth,clientAuth\nsubjectKeyIdentifier=hash\n",
            encoding="ascii",
        )
        cls.leaves = {role: _leaf(cls.directory, role) for role in ("S", "T")}

    def setUp(self):
        self.root = TemporaryDirectory(dir=self.directory)
        self.addCleanup(self.root.cleanup)
        self._fixture_index = 0

    def _side(self, role, target=None):
        from floati.cross_lan import GatePair, LanTransport, TlsIdentity, TlsRuntime

        cert, key, local = self.leaves[role]
        peer = self.leaves["T" if role == "S" else "S"][2]
        pair = GatePair(_OWNER, role, _ROOT, TlsIdentity(cert, key, local, peer), target, 0, 1)
        owner = _Authority(Path(self.root.name) / f"{role}-{self._fixture_index}", pair)
        owner.transport = LanTransport(owner, TlsRuntime.current())
        return owner

    def _listen(self):
        from floati.cross_lan import LanAddress

        server = self._side("T")
        listener = server.transport.listen(LanAddress("127.0.0.1", 0))
        self.addCleanup(listener.close)
        return server, listener

    @contextmanager
    def _assert_runtime_refusal_before_socket(self):
        with mock.patch("socket.socket", side_effect=AssertionError("socket allocated")):
            with self.assertRaises(ProtocolRefusal) as refused:
                yield
        self.assertEqual("cross_runtime_unsupported", refused.exception.code)

    def _assert_lan_runtime_refusal(self, *, dial=False):
        from floati.cross_lan import LanAddress

        server = self._side("T")
        address = LanAddress("127.0.0.1", 0)
        before = server.storage.read("gate-state")
        with self._assert_runtime_refusal_before_socket():
            server.transport.listen(address)
        self.assertEqual(before, server.storage.read("gate-state"))
        if dial:
            # A selected target and a published outbound item make this a
            # valid dial even though the runtime cannot start the listener.
            client = self._side("S", LanAddress("127.0.0.1", 1))
            before = client.storage.read("gate-state")
            with self._assert_runtime_refusal_before_socket():
                client.dial()
            self.assertEqual(before, client.storage.read("gate-state"))

    def test_default_disabled_and_runtime_mismatch_allocate_no_socket(self):
        from floati.cross_lan import LanAddress, LanTransport, TlsRuntime

        owner = self._side("T")
        address = LanAddress("127.0.0.1", 0)
        owner.enabled = False
        with mock.patch("socket.socket", side_effect=AssertionError("socket allocated")):
            with self.assertRaises(ProtocolRefusal) as disabled:
                owner.transport.listen(address)
        # Runtime qualification precedes the authority check.
        expected = "cross_disabled" if _has_tls13_runtime() else "cross_runtime_unsupported"
        self.assertEqual(expected, disabled.exception.code)
        with mock.patch.object(ssl, "HAS_TLSv1_3", False):
            with self._assert_runtime_refusal_before_socket():
                LanTransport(owner, TlsRuntime.current()).listen(address)
        owner.enabled = True
        bad = LanTransport(owner, replace(TlsRuntime.current(), library="unqualified"))
        with self._assert_runtime_refusal_before_socket():
            bad.listen(address)

    def test_unpaired_probes_receive_zero_bytes_and_leave_gate_untouched(self):
        if not _has_tls13_runtime():
            self._assert_lan_runtime_refusal()
            return
        server, listener = self._listen()
        before = server.storage.read("gate-state")
        key = direction_key(_ROOT, bytes.fromhex(_PAIR), "S-to-T")
        valid = gate_prefix(key, 1)
        probes = {
            "EOF": b"", "short": valid[:63], "unknown key": gate_prefix(b"\xff" * 32, 1),
            "wrong version": b"\x02" + valid[1:], "reserved": valid[:1] + b"\x01" + valid[2:],
            "overflow": valid[:24] + (1 << 53).to_bytes(8, "big") + valid[32:],
        }
        with ThreadPoolExecutor(max_workers=1) as pool:
            for label, probe in probes.items():
                with self.subTest(label=label):
                    future = pool.submit(listener.accept, _deadline())
                    with socket.create_connection((listener.address.host, listener.address.port), timeout=4) as peer:
                        peer.sendall(probe)
                        peer.shutdown(socket.SHUT_WR)
                        self.assertEqual(b"", peer.recv(4096))
                    self.assertIsNone(future.result(timeout=4))
        self.assertEqual([], server.opens)
        self.assertEqual(before, server.storage.read("gate-state"))

    def test_paired_dial_and_listener_publish_their_own_live_open(self):
        if not _has_tls13_runtime():
            self._assert_lan_runtime_refusal(dial=True)
            return
        server, listener = self._listen()
        client = self._side("S", listener.address)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(listener.accept, _deadline())
            with client.dial() as dialled:
                with future.result(timeout=4) as accepted:
                    self.assertIs(client.opens[0][1], dialled.opened)
                    self.assertIs(server.opens[0][1], accepted.opened)
                    self.assertEqual("TLSv1.3", dialled.tls.version)
                    for owner, side in ((server, "listener"), (client, "dialler")):
                        rows = owner.journal.read_committed(require_settled=True).records
                        kind = "remote_endpoint_registered" if side == "listener" else "remote_transport_attempt"
                        reference, = (row["id"] for row in rows if row["kind"] == kind)
                        body, opened = owner.opens[0]
                        self.assertEqual((side, reference, 1),
                                         (body["local_side"], body["owner_id"], body["gate_counter"]))
                        with owner.lock():
                            self.assertTrue(owner.transport.authenticate_open(body, opened.stream))
                            self.assertFalse(owner.transport.authenticate_open(dict(body, gate_counter=2), opened.stream))
                            self.assertFalse(owner.transport.authenticate_open(body, object()))
        body, opened = server.opens[0]
        with server.lock():
            self.assertFalse(server.transport.authenticate_open(body, opened.stream))
        self.assertNotEqual(client.opens[0][0]["stream_id"], body["stream_id"])
        image = GateImage.decode(server.storage.read("gate-state"))
        self.assertEqual(1, require_gate_owner(image, _OWNER, "S-to-T")[1])

        with ThreadPoolExecutor(max_workers=1) as pool:
            replay = pool.submit(listener.accept, _deadline())
            with socket.create_connection((listener.address.host, listener.address.port), timeout=4) as peer:
                peer.sendall(gate_prefix(direction_key(_ROOT, bytes.fromhex(_PAIR), "S-to-T"), 1))
                self.assertEqual(b"", peer.recv(4096))
            self.assertIsNone(replay.result(timeout=4))
        self.assertEqual(1, len(server.opens))

    def test_stop_after_tls_refuses_listener_open(self):
        if not _has_tls13_runtime():
            self._assert_lan_runtime_refusal(dial=True)
            return
        server, listener = self._listen()
        client = self._side("S", listener.address)

        def stop():
            result = server.journal.append(Capability("L", verb="disable"), dict(
                schema_version=1, kind="cross_optin", phase="disable_started", cause="local",
                id=f"cross-optin-{_ENABLE}-disable-started", enable_id=_ENABLE, enable_generation=1,
            ))
            self.assertEqual("committed", result.status)
            server.enabled = False

        server.before_open = stop
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(listener.accept, _deadline())
            with client.dial():
                self.assertIsNone(future.result(timeout=4))
        self.assertEqual([], server.opens)
        self.assertEqual(1, require_gate_owner(GateImage.decode(server.storage.read("gate-state")),
                                              _OWNER, "S-to-T")[1])

    def test_silent_gate_close_retries_once_and_burns_both_reservations(self):
        from floati.cross_coordinates import MAX_INTEGER

        for high, should_open in ((7, True), (MAX_INTEGER, False)):
            with self.subTest(high=high):
                self._fixture_index += 1
                if not _has_tls13_runtime():
                    self._assert_lan_runtime_refusal(dial=True)
                    continue
                server, listener = self._listen()
                client = self._side("S", listener.address)
                image = GateImage.decode(server.storage.read("gate-state"))
                replace_gate(server.storage, image, image.advance(_OWNER, "S-to-T", "in_hwm", high))

                def accept_twice():
                    self.assertIsNone(listener.accept(_deadline()))
                    return listener.accept(_deadline())

                with ThreadPoolExecutor(max_workers=1) as pool:
                    accepted = pool.submit(accept_twice)
                    if should_open:
                        with client.dial(), accepted.result(timeout=4):
                            self.assertEqual(1 + 2**20, client.opens[0][0]["gate_counter"])
                    else:
                        with self.assertRaises(ProtocolRefusal) as stopped:
                            client.dial()
                        self.assertEqual("cross_peer_silent_close", stopped.exception.code)
                        self.assertIsNone(accepted.result(timeout=4))
                client_image = GateImage.decode(client.storage.read("gate-state"))
                self.assertEqual(2 + 2**20, require_gate_owner(client_image, _OWNER, "S-to-T")[0])
                rows = client.journal.read_committed(require_settled=True).records
                self.assertEqual(2, sum(row["kind"] == "remote_transport_attempt" for row in rows))
                self.assertEqual(int(should_open), sum(row["kind"] == "remote_session_opened" for row in rows))

    def test_gate_approved_but_missing_client_leaf_emits_no_application_bytes(self):
        if not _has_tls13_runtime():
            self._assert_lan_runtime_refusal()
            return
        server, listener = self._listen()
        # The admitted-prefix fixture represents the named first-presenter case.
        # It carries the real pair prefix and no client credential.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ThreadPoolExecutor(max_workers=1) as pool:
            accepted = pool.submit(listener.accept, _deadline())
            with socket.create_connection((listener.address.host, listener.address.port), timeout=4) as raw:
                key = direction_key(_ROOT, bytes.fromhex(_PAIR), "S-to-T")
                raw.sendall(gate_prefix(key, 1))
                with ctx.wrap_socket(raw) as unpaired:
                    self.assertEqual(self.leaves["T"][2], unpaired.getpeercert(binary_form=True))
                    try:
                        self.assertEqual(b"", unpaired.recv(4096))
                    except ssl.SSLError:
                        pass
            self.assertIsNone(accepted.result(timeout=4))
        self.assertEqual([], server.opens)

    def test_pair_channel_proves_leaves_on_the_selected_stream(self):
        from floati.cross_lan import LanPairChannel, TlsRuntime

        if not _has_tls13_runtime():
            for role in ("S", "T"):
                with self.subTest(role=role):
                    stream = mock.Mock(spec=socket.socket)
                    cert, key, _ = self.leaves[role]
                    with self._assert_runtime_refusal_before_socket():
                        LanPairChannel(stream, role, cert, key, TlsRuntime.current())
                    self.assertEqual([], stream.mock_calls)
            return

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.create_connection(listener.getsockname(), timeout=4) as dialled:
                accepted, _ = listener.accept()
                with accepted, ThreadPoolExecutor(max_workers=1) as pool:
                    channels = {}
                    for role, stream in (("S", accepted), ("T", dialled)):
                        cert, key, _ = self.leaves[role]
                        channels[role] = LanPairChannel(stream, role, cert, key, TlsRuntime.current())
                        self.addCleanup(channels[role].close)
                    channels["S"].send(b'{"t":"commit"}', _deadline())
                    self.assertEqual(b'{"t":"commit"}', channels["T"].receive(_deadline()))
                    s_der, t_der = (self.leaves[role][2] for role in ("S", "T"))
                    server = pool.submit(channels["S"].authenticate, role="S", local_der=s_der,
                                         peer_der=t_der, deadline=_deadline())
                    self.assertEqual(s_der, channels["T"].authenticate(role="T", local_der=t_der,
                                                                       peer_der=s_der, deadline=_deadline()))
                    self.assertEqual(t_der, server.result(timeout=4))
                    channels["T"].send(b'{"t":"final_T"}', _deadline())
                    self.assertEqual(b'{"t":"final_T"}', channels["S"].receive(_deadline()))
