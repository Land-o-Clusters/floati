from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from unittest import mock

from floati.errors import DurabilityFailure, ProtocolRefusal
from floati.contracts import TaskContract, contract_digest
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.scheduler import RetryPolicy, RunScheduler

try:
    from floati.sequencer import (
        SequencerClient,
        SequencerConfig,
        SequencerService,
        _decode_request,
        _encode_frame,
        _semantic_uuid,
    )
except ModuleNotFoundError:
    SequencerClient = SequencerConfig = SequencerService = _decode_request = None


NOW = "2026-08-09T12:00:00.000Z"


def peer_closure(channel: socket.socket) -> str:
    """Report the ONE fact 'the peer closed', in whichever spelling the host uses.

    When a peer closes a connection that still has UNREAD data queued toward
    it, the two kernels answer differently: macOS lets the next `recv` return
    `b""`, Linux sends an RST and the next `recv` raises `ConnectionResetError`
    (ECONNRESET, errno 104). Measured, not assumed — a socketpair whose peer
    closes over an unread payload returns `b""` here on darwin, and public run
    33565717088 shows the reset on ubuntu at tests/test_sequencer.py:403.

    ⇒ ASSERTING ONE PLATFORM'S SPELLING OF A FACT IS NOT ASSERTING THE FACT.

    The fact under test was never "recv returns b''" — it is that the service
    hung up on the refused client. So this returns `"closed"` for either
    spelling and NAMES whatever else arrives, which keeps a delivered byte a
    failure rather than folding it into a widened predicate.
    """

    try:
        received = channel.recv(1)
    except ConnectionResetError:
        return "closed"
    if received == b"":
        return "closed"
    return "delivered " + repr(received)


def run_created(tenant: str, *, record_id: Optional[str] = None) -> dict[str, object]:
    return {
        "schema_version": 0,
        "id": record_id or "run-created-" + uuid7_hex(),
        "tenant_id": tenant,
        "timestamp": NOW,
        "kind": "run_created",
        "run_id": "run-" + uuid7_hex(),
        "plan_digest": "a" * 64,
        "item_ids": ["work-" + uuid7_hex()],
        "dependency_edges": [],
    }


class SequencerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(SequencerService, "floati.sequencer must provide SequencerService")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "alpha", create=True)
        self.services: list[tuple[object, threading.Event, threading.Thread]] = []

    def start(self, **config_values: object):
        config = SequencerConfig(select_timeout=0.01, **config_values)
        service = SequencerService(self.root, "sequencer-a", config=config)
        stop = threading.Event()
        thread = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
        thread.start()
        self.services.append((service, stop, thread))
        self.addCleanup(self.stop, service, stop, thread)
        return service

    @staticmethod
    def stop(service, stop: threading.Event, thread: threading.Thread) -> None:
        stop.set()
        thread.join(3)
        service.close()

    def client(self, service, client_id: str = "client-a"):
        return SequencerClient(service.socket_path, service.epoch, client_id)

    def queued(self, service, record: dict[str, object], client_id: str):
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        channel.settimeout(3)
        channel.connect(str(service.socket_path))
        channel.sendall(self.client(service, client_id).frame(record))
        self.addCleanup(channel.close)
        return channel

    def managed_effect_acceptance(self):
        from tests.test_effect_controller import _EffectCase
        from tests.test_runtruth import EffectAcceptanceTests

        case = _EffectCase(self)
        candidate = case.result_acceptance_candidate()
        intent, confirmed = EffectAcceptanceTests._confirmed_effect(case)
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        acceptance = EffectAcceptanceTests._bound(candidate, evidence)
        service = SequencerService(
            case.root, "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, max_batch=64),
        )
        self.addCleanup(service.close)
        response = service._process_request(
            self.client(service, "acceptance-seed").request(acceptance)
        )
        self.assertEqual("ok", response["status"])
        return case, intent, confirmed, acceptance, service, response

    def managed_acceptance_batch(self, service, acceptance):
        retry_channel = self.queued(service, acceptance, "acceptance-retry")
        peer = run_created("alpha")
        peer_channel = self.queued(service, peer, "acceptance-peer")
        self.assertEqual(2, service.serve_once())
        return (
            json.loads(retry_channel.recv(65536)),
            json.loads(peer_channel.recv(65536)),
        )

    @staticmethod
    def raw(path: Path, payload: bytes) -> dict[str, object]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(3)
            channel.connect(str(path))
            try:
                channel.sendall(payload)
            except BrokenPipeError:
                # A peer-UID refusal may close immediately after sending its frame.
                pass
            data = b""
            while not data.endswith(b"\n"):
                chunk = channel.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data)

    def test_round_trip_is_durable_idempotent_and_returns_physical_coordinate(self) -> None:
        """Catches success before canonical append, duplicate effects, or invented coordinates."""
        service = self.start(response_cache_size=1)
        client = self.client(service)
        first = run_created("alpha")
        second = run_created("alpha")

        one = client.append(first)
        two = client.append(second)
        retry = client.append(first)

        self.assertEqual(first, one["record"])
        self.assertEqual({"segment_number": -1, "frame_ordinal": 1, "global_ordinal": 1}, one["coordinate"])
        self.assertEqual(2, two["coordinate"]["global_ordinal"])
        self.assertEqual(one, retry)
        self.assertEqual([first, second], RunLedger(self.root).records())

        divergent = dict(first, run_id="run-" + uuid7_hex())
        with self.assertRaises(ProtocolRefusal) as caught:
            client.append(divergent)
        self.assertEqual("duplicate_record_id", caught.exception.code)
        self.assertEqual([first, second], RunLedger(self.root).records())

    def test_managed_same_id_acceptance_retry_returns_current_canonical_row_from_cache_and_durable_lookup(self) -> None:
        """Lawful control: managed retries return the one current physical acceptance."""
        for lookup in ("cache", "durable"):
            with self.subTest(lookup=lookup):
                case, _intent, _confirmed, acceptance, service, original = (
                    self.managed_effect_acceptance()
                )
                if lookup == "durable":
                    service.close()
                    service = SequencerService(
                        case.root, "sequencer-b",
                        config=SequencerConfig(select_timeout=0.01, max_batch=64),
                    )
                    self.addCleanup(service.close)

                retry, peer = self.managed_acceptance_batch(service, acceptance)

                self.assertEqual(original, retry)
                self.assertEqual("ok", peer["status"])
                self.assertEqual(1, sum(
                    row["kind"] == "result_accepted"
                    for row in RunLedger(case.root).records()
                ))

    def test_managed_same_id_acceptance_retry_allows_lawful_tail_from_cache_and_durable_lookup(self) -> None:
        """Catches managed retries comparing accepted evidence to a later proposal."""
        from tests.test_runtruth import EffectAcceptanceTests

        for lookup in ("cache", "durable"):
            with self.subTest(lookup=lookup):
                case, intent, confirmed, acceptance, service, _original = (
                    self.managed_effect_acceptance()
                )
                EffectAcceptanceTests._advance_effect_prefix(
                    case, intent, confirmed,
                )
                if lookup == "durable":
                    service.close()
                    service = SequencerService(
                        case.root, "sequencer-b",
                        config=SequencerConfig(select_timeout=0.01, max_batch=64),
                    )
                    self.addCleanup(service.close)

                retry, peer = self.managed_acceptance_batch(service, acceptance)

                self.assertEqual("ok", retry["status"])
                self.assertEqual(acceptance, retry["record"])
                self.assertEqual("ok", peer["status"])
                self.assertEqual(1, sum(
                    row["kind"] == "result_accepted"
                    for row in RunLedger(case.root).records()
                ))

    def test_managed_same_id_acceptance_retry_refuses_later_same_attempt_intent(self) -> None:
        """Catches cache or durable lookup bypassing the post-acceptance intent fence."""
        from tests.test_runtruth import EffectAcceptanceTests

        for lookup in ("cache", "durable"):
            with self.subTest(lookup=lookup):
                case, _intent, _confirmed, acceptance, service, _original = (
                    self.managed_effect_acceptance()
                )
                EffectAcceptanceTests._append_post_acceptance_intent(case)
                if lookup == "durable":
                    service.close()
                    service = SequencerService(
                        case.root, "sequencer-b",
                        config=SequencerConfig(select_timeout=0.01, max_batch=64),
                    )
                    self.addCleanup(service.close)

                retry, peer = self.managed_acceptance_batch(service, acceptance)

                self.assertEqual("refused", retry["status"])
                self.assertEqual("effect_evidence_overtaken", retry["code"])
                self.assertEqual("refused", peer["status"])
                self.assertEqual("effect_evidence_overtaken", peer["code"])

    def test_managed_single_request_cached_acceptance_retry_validates_current_prefix(self) -> None:
        """Catches the non-batched response cache bypassing the guarded retry path."""
        from tests.test_runtruth import EffectAcceptanceTests

        for later_tail in ("none", "proposal", "intent"):
            with self.subTest(later_tail=later_tail):
                case, intent, confirmed, acceptance, service, original = (
                    self.managed_effect_acceptance()
                )
                if later_tail == "proposal":
                    EffectAcceptanceTests._advance_effect_prefix(
                        case, intent, confirmed,
                    )
                elif later_tail == "intent":
                    EffectAcceptanceTests._append_post_acceptance_intent(case)

                if later_tail == "intent":
                    with self.assertRaises(ProtocolRefusal) as caught:
                        service._process_request(
                            self.client(service, "single-retry").request(acceptance)
                        )
                    self.assertEqual(
                        "effect_evidence_overtaken", caught.exception.code,
                    )
                else:
                    self.assertEqual(
                        original,
                        service._process_request(
                            self.client(service, "single-retry").request(acceptance)
                        ),
                    )

    def test_runtime_socket_modes_and_same_uid_positive_control_precede_refusal(self) -> None:
        """Catches permissive filesystem endpoints or treating an arbitrary local peer as same-UID."""
        service = self.start()
        self.assertEqual(0o700, stat.S_IMODE(service.socket_path.parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(service.socket_path.stat().st_mode))
        self.assertEqual("ok", self.client(service).append(run_created("alpha"))["status"])

        original = service._peer_uid
        service._peer_uid = lambda _channel: os.geteuid() + 1
        try:
            frame = self.client(service, "client-b").frame(run_created("alpha"))
            refused = self.raw(service.socket_path, frame)
        finally:
            service._peer_uid = original
        self.assertEqual("refused", refused["status"])
        self.assertEqual("peer_uid_mismatch", refused["code"])

    def test_exact_frame_rejects_duplicate_non_utf8_oversize_unknown_and_private_record(self) -> None:
        """Catches permissive JSON decoding or a raw frame escalating to domain-owned append authority."""
        service = self.start()
        record = run_created("alpha")
        valid = self.client(service).request(record)
        text = json.dumps(valid, separators=(",", ":"))
        duplicate = text.replace('"protocol_version":1', '"protocol_version":1,"protocol_version":1', 1).encode() + b"\n"
        private = dict(record, id="attempt-opened-" + uuid7_hex(), kind="attempt_opened")
        cases = (
            (duplicate, "duplicate_json_key"),
            (b"{\"protocol_version\":\"\xff\"}\n", "frame_not_utf8"),
            (json.dumps({**valid, "unknown": 1}, separators=(",", ":")).encode() + b"\n", "frame_fields_invalid"),
            (self.client(service).frame(private), "private_record_requires_intent"),
        )
        for payload, code in cases:
            with self.subTest(code=code):
                response = self.raw(service.socket_path, payload)
                self.assertEqual("refused", response["status"])
                self.assertEqual(code, response["code"])
                self.assertEqual({"status", "code", "detail"}, set(response))

        with mock.patch("floati.sequencer.MAX_FRAME_BYTES", 1024):
            response = self.raw(service.socket_path, b"{" + b" " * 1024)
        self.assertEqual("refused", response["status"])
        self.assertEqual("frame_too_large", response["code"])
        self.assertEqual({"status", "code", "detail"}, set(response))

    def test_aggregate_buffer_default_carries_one_maximum_lawful_frame(self) -> None:
        """Catches an aggregate bound that makes the lawful per-frame ceiling unreachable."""
        from floati import sequencer

        self.assertEqual(
            sequencer.MAX_FRAME_BYTES
            + (sequencer.MAX_CLIENTS - 1) * sequencer.SOCKET_READ_BYTES,
            sequencer.MAX_REQUEST_BUFFER_BYTES,
        )
        maximum_frame = 1024
        read_quantum = 64
        max_clients = 4
        aggregate_cap = maximum_frame + (max_clients - 1) * read_quantum
        with (
            mock.patch.object(sequencer, "MAX_FRAME_BYTES", maximum_frame),
            mock.patch.object(sequencer, "SOCKET_READ_BYTES", read_quantum),
        ):
            service = self.start(
                max_clients=max_clients,
                max_request_buffer_bytes=aggregate_cap,
            )
            client = self.client(service, "maximum-frame")
            record = run_created("alpha")
            frame = client.frame(record)
            self.assertLess(len(frame), maximum_frame)
            frame = (
                frame[:-1]
                + b" " * (maximum_frame - len(frame))
                + b"\n"
            )
            self.assertEqual(maximum_frame, len(frame))
            self.assertEqual(client.request(record), _decode_request(frame))

            response = self.raw(service.socket_path, frame)

            self.assertEqual("ok", response["status"])
            self.assertEqual(record, response["record"])
            self.assertEqual([record], RunLedger(self.root).records())
            self.assertEqual(0, service._request_buffer_bytes)

    def test_aggregate_buffer_overload_refuses_reuses_credit_and_keeps_peer_live(self) -> None:
        """Catches partial clients exceeding aggregate credit or leaking it on transitions."""
        from floati import sequencer

        maximum_frame = 1024
        read_quantum = 64
        max_clients = 4
        aggregate_cap = maximum_frame + (max_clients - 1) * read_quantum
        with (
            mock.patch.object(sequencer, "MAX_FRAME_BYTES", maximum_frame),
            mock.patch.object(sequencer, "SOCKET_READ_BYTES", read_quantum),
        ):
            service = SequencerService(
                self.root,
                "sequencer-a",
                config=SequencerConfig(
                    select_timeout=0.01,
                    max_clients=max_clients,
                    max_batch=1,
                    max_request_buffer_bytes=aggregate_cap,
                ),
            )
            self.addCleanup(service.close)

            def partial(payload: bytes) -> socket.socket:
                channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                channel.settimeout(3)
                channel.connect(str(service.socket_path))
                channel.sendall(payload)
                self.addCleanup(channel.close)
                return channel

            maximum_holder = partial(b"{" + b" " * (maximum_frame - 1))
            first_holder = partial(b"a" * read_quantum)
            second_holder = partial(b"b" * read_quantum)
            excess = partial(b"c" * read_quantum)

            self.assertEqual(0, service.serve_once())
            self.assertEqual(aggregate_cap, service._request_buffer_bytes)
            excess.sendall(b"c")
            self.assertEqual(0, service.serve_once())
            refused = json.loads(excess.recv(65536))
            self.assertEqual(
                ("refused", "request_buffer_overloaded"),
                (refused["status"], refused["code"]),
            )
            self.assertEqual({"status", "code", "detail"}, set(refused))
            self.assertEqual("closed", peer_closure(excess))
            self.assertLessEqual(service._request_buffer_bytes, aggregate_cap)
            self.assertEqual(
                maximum_frame + 2 * read_quantum,
                service._request_buffer_bytes,
            )

            maximum_holder.close()
            self.assertEqual(0, service.serve_once())
            self.assertEqual(2 * read_quantum, service._request_buffer_bytes)

            record = run_created("alpha")
            client = self.client(service, "live-peer")
            frame = client.frame(record)
            frame = (
                frame[:-1]
                + b" " * (maximum_frame - len(frame))
                + b"\n"
            )
            valid = partial(frame)
            self.assertEqual(1, service.serve_once())
            response = json.loads(valid.recv(65536))
            self.assertEqual("ok", response["status"])
            self.assertEqual(record, response["record"])
            self.assertEqual([record], RunLedger(self.root).records())
            self.assertEqual(2 * read_quantum, service._request_buffer_bytes)

            queued_records = [run_created("alpha"), run_created("alpha")]
            queued_client = self.client(service, "ready-before-partial")
            queued = [
                partial(queued_client.frame(item)) for item in queued_records
            ]
            self.assertEqual(1, service.serve_once())
            self.assertEqual("ok", json.loads(queued[0].recv(65536))["status"])
            self.assertTrue(service._ready)
            buffered_before_ready_turn = service._request_buffer_bytes
            first_holder.sendall(b"d" * read_quantum)
            self.assertEqual(1, service.serve_once())
            self.assertEqual("ok", json.loads(queued[1].recv(65536))["status"])
            self.assertEqual(
                buffered_before_ready_turn,
                service._request_buffer_bytes,
            )
            self.assertEqual(
                [record] + queued_records,
                RunLedger(self.root).records(),
            )

            service.close()
            self.assertEqual(0, service._request_buffer_bytes)

    def test_ijson_framing_rejects_recursive_numeric_overflow_with_finite_control(self) -> None:
        """Catches JSON numeric overflow escaping the recursive finite-number boundary."""
        finite = _decode_request(b'{"outer":[{"number":1e308}]}\n')
        self.assertEqual(1e308, finite["outer"][0]["number"])

        for payload in (
            b'{"outer":[{"number":1e309}]}\n',
            b'{"outer":[{"number":-1e309}]}\n',
            b'{"outer":[{"number":NaN}]}\n',
            b'{"outer":[{"number":Infinity}]}\n',
            b'{"outer":[{"number":-Infinity}]}\n',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ProtocolRefusal) as caught:
                    _decode_request(payload)
                self.assertEqual("frame_not_ijson", caught.exception.code)

    def test_stale_epoch_and_two_frames_on_one_connection_refuse_without_append(self) -> None:
        """Catches stale writers or more than one queued request on a connection."""
        service = self.start()
        record = run_created("alpha")
        stale = SequencerClient(service.socket_path, service.epoch + 1, "stale")
        with self.assertRaises(ProtocolRefusal) as caught:
            stale.append(record)
        self.assertEqual("sequencer_epoch_mismatch", caught.exception.code)

        client = self.client(service)
        frame = client.frame(record)
        response = self.raw(service.socket_path, frame + frame)
        self.assertEqual("connection_queue_limit", response["code"])
        self.assertEqual([], RunLedger(self.root).records())

    def test_no_response_precedes_covering_fsync(self) -> None:
        """Catches acknowledging an append while its covering file fsync is still blocked."""
        from floati import run_segments

        service = self.start()
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        result: list[object] = []
        original = run_segments._append_frame

        def blocked(path, encoded):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fsync barrier timed out")
            return original(path, encoded)

        def append() -> None:
            try:
                result.append(self.client(service).append(run_created("alpha")))
            finally:
                finished.set()

        with mock.patch.object(run_segments, "_append_frame", side_effect=blocked):
            thread = threading.Thread(target=append)
            thread.start()
            self.assertTrue(entered.wait(3))
            time.sleep(0.05)
            self.assertFalse(finished.is_set())
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual("ok", result[0]["status"])

    def test_lost_post_fsync_response_is_recovered_by_exact_retry(self) -> None:
        """Catches response loss causing a duplicate effect or losing the committed coordinate."""
        service = self.start()
        record = run_created("alpha")
        original = service._send_response
        dropped = threading.Event()

        def drop_once(channel, response):
            if response.get("status") == "ok" and not dropped.is_set():
                dropped.set()
                channel.close()
                return
            return original(channel, response)

        service._send_response = drop_once
        with self.assertRaises(ProtocolRefusal) as caught:
            self.client(service).append(record)
        self.assertEqual("sequencer_response_lost", caught.exception.code)
        service._send_response = original
        retry = self.client(service).append(record)
        self.assertEqual(1, retry["coordinate"]["global_ordinal"])
        self.assertEqual([record], RunLedger(self.root).records())

    def test_round_robin_quiet_clients_are_not_starved_by_reconnecting_client(self) -> None:
        """Catches a reconnecting client monopolizing physical writer turns."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, max_batch=1),
        )
        stop = threading.Event()
        thread = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
        quiet_channels = []
        for index in range(100):
            record = run_created("alpha")
            client = SequencerClient(
                service.socket_path, service.epoch, "quiet-" + str(index)
            )
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.settimeout(10)
            channel.connect(str(service.socket_path))
            channel.sendall(client.frame(record))
            quiet_channels.append(channel)
        thread.start()
        self.addCleanup(self.stop, service, stop, thread)
        noisy_results = []
        for _index in range(12):
            noisy_results.append(
                SequencerClient(service.socket_path, service.epoch, "noisy").append(
                    run_created("alpha")
                )
            )
        quiet_positions = []
        for channel in quiet_channels:
            response = json.loads(channel.recv(65536))
            quiet_positions.append(response["coordinate"]["global_ordinal"])
            channel.close()
        self.assertEqual(100, len(quiet_positions))
        self.assertLessEqual(max(quiet_positions), 101)
        self.assertNotIn("ledger_lock_timeout", [item.get("code") for item in noisy_results])

    def test_close_releases_epoch_unlinks_socket_and_allows_direct_mode(self) -> None:
        """Catches shutdown leaving a live-looking endpoint, open epoch, or stranded daemonless writer."""
        service = self.start()
        managed = run_created("alpha")
        self.assertEqual("ok", self.client(service).append(managed)["status"])
        self.assertEqual(0, service._request_buffer_bytes)
        path = service.socket_path
        listener = service._listener
        self.assertIsNotNone(listener)
        service.close()
        self.assertFalse(path.exists())
        self.assertEqual(-1, listener.fileno())
        from floati.sequencer_epoch import DirectWriterLease, SequencerEpochLedger

        self.assertEqual("released", SequencerEpochLedger(self.root).current()["operation"])
        with DirectWriterLease(self.root):
            appended = RunLedger(self.root).append(run_created("alpha"))
        self.assertEqual("run_created", appended["kind"])
        self.assertEqual([managed, appended], RunLedger(self.root).records())

    def test_concurrent_read_and_close_release_every_service_resource(self) -> None:
        """Catches close splitting bytearray growth from shared buffer accounting."""
        from floati.sequencer_epoch import DirectWriterLease, SequencerEpochLedger

        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01),
        )
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        channel.settimeout(3)
        channel.connect(str(service.socket_path))
        channel.sendall(b"{")
        self.addCleanup(channel.close)
        service._accept_ready()
        self.assertEqual(1, len(service._connections))
        connection = next(iter(service._connections.values()))
        listener = service._listener
        self.assertIsNotNone(listener)
        path = service.socket_path

        extended = threading.Event()
        allow_extend = threading.Event()
        allow_release = threading.Event()
        coordination = threading.Condition()
        observed = {"close_lock": False, "release": False}
        close_thread_name = "sequencer-concurrent-close"

        def signal(name: str) -> None:
            with coordination:
                observed[name] = True
                coordination.notify_all()

        class BarrierBuffer(bytearray):
            def extend(self, payload: bytes) -> None:
                super().extend(payload)
                extended.set()
                if not allow_extend.wait(3):
                    raise RuntimeError("read accounting barrier timed out")

        class ObservedRLock:
            def __init__(self) -> None:
                self.lock = threading.RLock()

            def acquire(self, *args: object, **kwargs: object) -> bool:
                if threading.current_thread().name == close_thread_name:
                    signal("close_lock")
                return self.lock.acquire(*args, **kwargs)

            def release(self) -> None:
                self.lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, _kind, _value, _traceback) -> None:
                self.release()

        connection.buffer = BarrierBuffer()
        service._state_lock = ObservedRLock()
        original_release = service._release_request_buffer

        def blocked_release(candidate) -> None:
            signal("release")
            if not allow_release.wait(3):
                raise RuntimeError("close accounting barrier timed out")
            original_release(candidate)

        service._release_request_buffer = blocked_release

        def force_cleanup() -> None:
            allow_extend.set()
            allow_release.set()
            service._release_request_buffer = original_release
            if connection.channel.fileno() >= 0:
                connection.channel.close()
            if service._lease._active:
                service._closed = False
                service.close()

        self.addCleanup(force_cleanup)
        read_errors: list[BaseException] = []
        close_errors: list[BaseException] = []
        close_finished = threading.Event()

        def read() -> None:
            try:
                service._read_ready(connection)
            except BaseException as exc:
                read_errors.append(exc)

        def close() -> None:
            try:
                service.close()
            except BaseException as exc:
                close_errors.append(exc)
            finally:
                close_finished.set()

        reader = threading.Thread(target=read, name="sequencer-concurrent-read")
        closer = threading.Thread(target=close, name=close_thread_name)
        reader.start()
        self.assertTrue(extended.wait(3))
        closer.start()
        with coordination:
            self.assertTrue(coordination.wait_for(
                lambda: observed["close_lock"] or observed["release"],
                timeout=3,
            ))
        if observed["release"]:
            allow_release.set()
            self.assertTrue(close_finished.wait(3))
            self.assertFalse(allow_extend.is_set())
            allow_extend.set()
        else:
            self.assertTrue(observed["close_lock"])
            allow_extend.set()
            with coordination:
                self.assertTrue(coordination.wait_for(
                    lambda: observed["release"],
                    timeout=3,
                ))
            allow_release.set()
        reader.join(3)
        closer.join(3)

        self.assertFalse(reader.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual([], read_errors)
        self.assertEqual([], close_errors)
        self.assertEqual(0, service._request_buffer_bytes)
        self.assertEqual(0, connection.buffered_bytes)
        self.assertEqual(0, len(connection.buffer))
        self.assertEqual({}, service._connections)
        self.assertEqual(-1, connection.channel.fileno())
        self.assertEqual(-1, listener.fileno())
        self.assertEqual(b"", channel.recv(1))
        self.assertFalse(path.exists())
        self.assertEqual("released", SequencerEpochLedger(self.root).current()["operation"])
        with DirectWriterLease(self.root):
            direct = RunLedger(self.root).append(run_created("alpha"))
        self.assertEqual("run_created", direct["kind"])

    def test_accounting_invariant_failure_still_completes_terminal_cleanup(self) -> None:
        """Catches an internal accounting assertion skipping terminal resource release."""
        from floati.sequencer_epoch import DirectWriterLease, SequencerEpochLedger

        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01),
        )
        self.addCleanup(service.close)
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        channel.settimeout(3)
        channel.connect(str(service.socket_path))
        channel.sendall(b"{")
        self.addCleanup(channel.close)
        service._accept_ready()
        connection = next(iter(service._connections.values()))
        listener = service._listener
        self.assertIsNotNone(listener)
        path = service.socket_path
        service._read_ready(connection)
        self.assertEqual(1, service._request_buffer_bytes)
        self.assertEqual(1, connection.buffered_bytes)
        self.assertEqual(1, len(connection.buffer))

        with mock.patch.object(
            service,
            "_release_request_buffer",
            side_effect=AssertionError("injected accounting invariant"),
        ):
            with self.assertRaisesRegex(
                AssertionError, "injected accounting invariant"
            ):
                service.close()

        self.assertEqual(0, service._request_buffer_bytes)
        self.assertEqual(0, connection.buffered_bytes)
        self.assertEqual(0, len(connection.buffer))
        self.assertEqual({}, service._connections)
        self.assertEqual(-1, connection.channel.fileno())
        self.assertEqual(-1, listener.fileno())
        self.assertEqual(b"", channel.recv(1))
        self.assertFalse(path.exists())
        self.assertEqual("released", SequencerEpochLedger(self.root).current()["operation"])
        service.close()
        with DirectWriterLease(self.root):
            direct = RunLedger(self.root).append(run_created("alpha"))
        self.assertEqual("run_created", direct["kind"])

    def test_typed_scheduler_intent_is_reconstructed_under_service_ownership(self) -> None:
        """Catches forwarding a private durable record or treating its raw frame as scheduler authority."""
        service = self.start()
        client = self.client(service)
        ledger = RunLedger(self.root, sequencer_client=client)
        run_id = "run-" + uuid7_hex()
        item_id = "work-" + uuid7_hex()
        created = run_created("alpha")
        created["run_id"] = run_id
        created["item_ids"] = [item_id]
        ledger.append(created)
        contract = TaskContract.create(
            objective="typed scheduler intent",
            non_goals=["no raw authority"],
            areas_to_avoid=[{"path": "slip/graph.py", "region": "all"}],
            input_hashes={"brief": "a" * 64},
            acceptance_checks={"tests": "python3 -m unittest"},
            constraints={"network": "dark"},
            risk_class="high",
            retry_policy={
                "max_attempts": 2,
                "backoff": {
                    "base_delay_ms": 10,
                    "cap_delay_ms": 10,
                    "strategy": "exponential",
                },
            },
            dependencies=[],
        )
        ledger.append(
            {
                "schema_version": 0,
                "id": "task-contract-" + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": NOW,
                "kind": "task_contract",
                "run_id": run_id,
                "item_id": item_id,
                **contract.canonical(),
                "contract_digest": contract_digest(contract),
            }
        )

        opened = RunScheduler(ledger).open_attempt(
            run_id,
            item_id,
            RetryPolicy(max_attempts=2, base_delay_ms=10, cap_delay_ms=10),
            1,
            now=NOW,
        )

        self.assertEqual("attempt_opened", opened["kind"])
        self.assertEqual("alpha", opened["tenant_id"])
        self.assertEqual(opened, RunLedger(self.root).records()[-1])

        intent = client.intent_request("scheduler", opened)
        intent["operation_id"] = "attempt-opened-" + uuid7_hex()
        intent["intent"]["fields"]["tenant_id"] = "forged"
        hostile = json.dumps(intent, separators=(",", ":")).encode() + b"\n"
        refused = self.raw(service.socket_path, hostile)
        self.assertEqual("intent_fields_invalid", refused["code"])
        self.assertEqual(3, len(RunLedger(self.root).records()))

    def test_one_ready_batch_uses_one_covering_run_fsync_before_responses(self) -> None:
        """Catches nominal batching that still fsyncs each accepted frame separately."""
        from floati import run_segments

        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, max_batch=64),
        )
        self.addCleanup(service.close)
        channels = []
        records = [run_created("alpha"), run_created("alpha")]
        for index, record in enumerate(records):
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.settimeout(3)
            channel.connect(str(service.socket_path))
            channel.sendall(
                SequencerClient(
                    service.socket_path, service.epoch, "batch-" + str(index)
                ).frame(record)
            )
            channels.append(channel)
        original = run_segments._append_frame
        with mock.patch.object(run_segments, "_append_frame", wraps=original) as append_frame:
            self.assertEqual(2, service.serve_once())
        responses = []
        for channel in channels:
            responses.append(json.loads(channel.recv(65536)))
            channel.close()
        self.assertEqual(["ok", "ok"], [item["status"] for item in responses])
        self.assertEqual(1, append_frame.call_count)
        self.assertEqual(records, RunLedger(self.root).records())

    def test_empty_object_is_typed_refusal_and_service_remains_live(self) -> None:
        """Catches a missing envelope member escaping as KeyError and stopping service."""
        service = self.start()
        refused = self.raw(service.socket_path, b"{}\n")
        self.assertEqual("refused", refused["status"])
        self.assertEqual("frame_fields_invalid", refused["code"])
        self.assertEqual(
            "ok", self.client(service, "positive-after-empty").append(run_created("alpha"))["status"]
        )

    def test_invalid_batch_member_does_not_poison_valid_peer(self) -> None:
        """Catches one semantic refusal being broadcast to every ready client."""
        service = SequencerService(
            self.root, "sequencer-a", config=SequencerConfig(select_timeout=0.01)
        )
        self.addCleanup(service.close)
        invalid = run_created("alpha")
        invalid["plan_digest"] = "not-a-digest"
        valid = run_created("alpha")
        bad_channel = self.queued(service, invalid, "invalid-peer")
        good_channel = self.queued(service, valid, "valid-peer")

        self.assertEqual(2, service.serve_once())
        bad = json.loads(bad_channel.recv(65536))
        good = json.loads(good_channel.recv(65536))

        self.assertEqual("refused", bad["status"])
        self.assertEqual("plan_digest_invalid", bad["code"])
        self.assertEqual("ok", good["status"])
        self.assertEqual([valid], RunLedger(self.root).records())

    def test_invalid_suspension_evaluation_does_not_poison_valid_public_peer(self) -> None:
        """Catches a refused semantic evaluation being broadcast to a valid append."""
        service = SequencerService(
            self.root, "sequencer-a", config=SequencerConfig(select_timeout=0.01)
        )
        self.addCleanup(service.close)
        client = self.client(service)
        intent = {
            "run_id": "run-" + uuid7_hex(),
            "item_id": "work-" + uuid7_hex(),
            "attempt_id": "attempt-" + uuid7_hex(),
            "approval_request_id": "approval-request-" + uuid7_hex(),
            "adapter": 123,
            "resume_mode": "unsupported",
            "provider_session_or_thread_id": None,
            "workspace_checkpoint": {
                "repo": "owner/slipway",
                "sha": "d" * 40,
                "doc": "docs/checkpoints/approval-park.md",
            },
            "execution_authority_subject": "execute-run",
            "execution_authority_holder": "worker-a",
            "execution_authority_epoch": 1,
        }
        operation = "suspension_evaluation"
        request = client._evaluation_request(
            operation,
            "suspension-evaluation-" + _semantic_uuid(operation, intent),
            intent,
        )
        invalid = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        invalid.settimeout(3)
        invalid.connect(str(service.socket_path))
        invalid.sendall(_encode_frame(request))
        self.addCleanup(invalid.close)
        valid = run_created("alpha")
        public = self.queued(service, valid, "public-peer")

        self.assertEqual(1, service.serve_once())
        refused = json.loads(invalid.recv(65536))
        accepted = json.loads(public.recv(65536))
        self.assertEqual("refused", refused["status"])
        self.assertEqual("intent_fields_invalid", refused["code"])
        self.assertEqual("ok", accepted["status"])
        self.assertEqual([valid], RunLedger(self.root).records())

    def test_raw_spawn_records_remain_private(self) -> None:
        """Catches raw or generic typed frames bypassing semantic spawn evaluation."""
        service = self.start()
        client = self.client(service)
        raw = {
            "schema_version": 1,
            "id": "spawn-group-created-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "spawn_group_created",
        }
        response = self.raw(service.socket_path, client.frame(raw))
        self.assertEqual(
            ("refused", "spawn_group_controller_only"),
            (response["status"], response["code"]),
        )

        request = client.intent_request("scheduler", raw)
        typed = self.raw(service.socket_path, _encode_frame(request))
        self.assertEqual(
            ("refused", "spawn_group_controller_only"),
            (typed["status"], typed["code"]),
        )

        amendment = dict(
            raw,
            id="plan-amendment-" + uuid7_hex(),
            kind="plan_amendment",
        )
        amendment_response = self.raw(
            service.socket_path, client.frame(amendment),
        )
        self.assertEqual(
            ("refused", "spawn_group_controller_only"),
            (amendment_response["status"], amendment_response["code"]),
        )

    def test_same_id_conflict_in_one_ready_batch_does_not_refuse_valid_peer(self) -> None:
        """Catches a duplicate inside one batch poisoning an unrelated accepted append."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, max_batch=64),
        )
        self.addCleanup(service.close)
        duplicated = run_created("alpha")
        conflicting = dict(duplicated, run_id="run-" + uuid7_hex())
        peer = run_created("alpha")
        first_channel = self.queued(service, duplicated, "duplicate-first")
        conflict_channel = self.queued(service, conflicting, "duplicate-conflict")
        peer_channel = self.queued(service, peer, "unrelated-peer")

        self.assertEqual(3, service.serve_once())
        first = json.loads(first_channel.recv(65536))
        conflict = json.loads(conflict_channel.recv(65536))
        peer_response = json.loads(peer_channel.recv(65536))

        self.assertEqual("ok", first["status"])
        self.assertEqual("refused", conflict["status"])
        self.assertEqual("duplicate_record_id", conflict["code"])
        self.assertEqual("ok", peer_response["status"])
        self.assertEqual([duplicated, peer], RunLedger(self.root).records())

    def test_projection_invalid_middle_member_does_not_refuse_valid_batch_peers(self) -> None:
        """Catches governed projection refusal being broadcast to unrelated valid peers."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, max_batch=64),
        )
        self.addCleanup(service.close)
        existing = run_created("alpha")
        self.assertEqual(
            "ok",
            service._process_request(
                self.client(service, "projection-seed").request(existing)
            )["status"],
        )
        first = run_created("alpha")
        invalid = run_created("alpha")
        invalid["run_id"] = existing["run_id"]
        third = run_created("alpha")
        channels = [
            self.queued(service, first, "projection-first"),
            self.queued(service, invalid, "projection-invalid"),
            self.queued(service, third, "projection-third"),
        ]

        self.assertEqual(3, service.serve_once())
        responses = [json.loads(channel.recv(65536)) for channel in channels]

        self.assertEqual(
            [("ok", None), ("refused", "run_duplicate"), ("ok", None)],
            [(response["status"], response.get("code")) for response in responses],
        )
        self.assertEqual([existing, first, third], RunLedger(self.root).records())

    def test_identical_retry_alias_receives_batch_failure_and_connection_closes(self) -> None:
        """Catches a failed covering append leaving an identical retry unanswered."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, max_batch=64),
        )
        self.addCleanup(service.close)
        seed = run_created("alpha")
        self.assertEqual(
            "ok",
            service._process_request(
                self.client(service, "alias-seed").request(seed)
            )["status"],
        )
        candidate = run_created("alpha")
        first = self.queued(service, candidate, "alias-first")
        retry = self.queued(service, candidate, "alias-retry")
        first.settimeout(0.5)
        retry.settimeout(0.5)

        with mock.patch.object(
            service._ledger,
            "_append_managed_batch",
            side_effect=DurabilityFailure(
                "forced_batch_failure", "covering append failed before durability"
            ),
        ):
            self.assertEqual(2, service.serve_once())
        responses = [json.loads(channel.recv(65536)) for channel in (first, retry)]

        self.assertEqual(
            [("refused", "forced_batch_failure")] * 2,
            [(response["status"], response.get("code")) for response in responses],
        )
        self.assertEqual(b"", first.recv(1))
        self.assertEqual(b"", retry.recv(1))
        self.assertEqual([seed], RunLedger(self.root).records())

    def test_listener_saturation_does_not_starve_accepted_client(self) -> None:
        """Catches a readable full listener spinning ahead of an accepted request."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, max_clients=1),
        )
        self.addCleanup(service.close)
        first_record = run_created("alpha")
        first = self.queued(service, first_record, "accepted-client")
        service._accept_ready()
        second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second.settimeout(3)
        second.connect(str(service.socket_path))
        self.addCleanup(second.close)
        finished = threading.Event()

        def serve() -> None:
            try:
                service.serve_once()
            finally:
                finished.set()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        self.assertTrue(finished.wait(1), "full listener must not livelock serve_once")
        response = json.loads(first.recv(65536))
        self.assertEqual("ok", response["status"])
        self.assertEqual([first_record], RunLedger(self.root).records())

    def test_normalized_retry_after_cache_eviction_does_not_poison_peer(self) -> None:
        """Catches raw-versus-canonical retry comparison after the response LRU evicts."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, response_cache_size=1),
        )
        self.addCleanup(service.close)
        first_item, second_item = sorted(
            ("work-" + uuid7_hex(), "work-" + uuid7_hex())
        )
        retried = run_created("alpha")
        retried["item_ids"] = [first_item, second_item]
        retried["dependency_edges"] = [{"source": first_item, "target": second_item}]
        evictor = run_created("alpha")
        first = service._process_request(self.client(service).request(retried))
        service._process_request(self.client(service, "evictor").request(evictor))
        peer = run_created("alpha")
        retry_channel = self.queued(service, retried, "retry-peer")
        peer_channel = self.queued(service, peer, "new-peer")

        self.assertEqual(2, service.serve_once())
        retry_response = json.loads(retry_channel.recv(65536))
        peer_response = json.loads(peer_channel.recv(65536))

        self.assertEqual("ok", retry_response["status"])
        self.assertEqual(first["coordinate"], retry_response["coordinate"])
        self.assertEqual("ok", peer_response["status"])
        self.assertEqual(3, len(RunLedger(self.root).records()))

    def test_close_clears_response_cache_and_closed_service_cannot_repopulate(self) -> None:
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, response_cache_size=4),
        )
        record = run_created("alpha")
        response = service._process_request(self.client(service).request(record))
        self.assertEqual("ok", response["status"])
        self.assertEqual(1, len(service._cache))

        service.close()
        self.assertEqual(0, len(service._cache))
        service.close()
        service._remember("after-close", record, response)
        self.assertEqual(0, len(service._cache))

    def test_inflight_remember_cannot_repopulate_cache_after_close(self) -> None:
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01, response_cache_size=4),
        )
        lawful_record = run_created("alpha")
        lawful_response = {
            "status": "ok", "record": lawful_record, "coordinate": {},
        }
        service._remember("lawful-open-service", lawful_record, lawful_response)
        self.assertEqual(1, len(service._cache))

        entered = threading.Event()
        release = threading.Event()

        class BlockingCache(OrderedDict):
            def __setitem__(self, key: object, value: object) -> None:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("remember/close barrier timed out")
                super().__setitem__(key, value)

        service._cache = BlockingCache()
        remember_errors: list[BaseException] = []
        close_errors: list[BaseException] = []

        def remember() -> None:
            try:
                service._remember(
                    "inflight", lawful_record, lawful_response,
                )
            except BaseException as exc:
                remember_errors.append(exc)

        def close() -> None:
            try:
                service.close()
            except BaseException as exc:
                close_errors.append(exc)

        remember_thread = threading.Thread(target=remember)
        close_thread = threading.Thread(target=close)
        remember_thread.start()
        self.assertTrue(entered.wait(3))
        close_thread.start()
        release.set()
        remember_thread.join(5)
        close_thread.join(5)
        self.assertFalse(remember_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertFalse(remember_errors, remember_errors)
        self.assertFalse(close_errors, close_errors)
        self.assertEqual(0, len(service._cache))

        service._remember("after-close", lawful_record, lawful_response)
        self.assertEqual(0, len(service._cache))


if __name__ == "__main__":
    unittest.main()
