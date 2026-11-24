"""Behavioral regressions for epoch admission and cooperating writer exclusion.

These tests describe current-runtime requirements, not the cause of any past
roll failure. Child processes own only fixture paths. Their flock observations
are synchronization evidence; no elapsed-time performance threshold is used.
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
# Direct child entry uses -I, so import this exact repository explicitly.
if __name__ == "__main__" and "--owned-writer" in sys.argv:
    sys.path.insert(0, str(REPOSITORY))

from floati import bus_epoch, jsonl
from floati.cursor import SparseCursor
from floati.errors import DurabilityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS, EventLog
from floati.framing import decode_frames, encode_frame
from floati.ledger_repair import LedgerRepair
from floati.root import FloatiRoot
from tests import test_bus_epoch_roll as native_fixture
from tests.readiness import Readiness


# The cooperating-writer fallback must outlive its 60-second phase handshake.
# No lock-latency value is asserted by these exclusion controls.
CHILD_SECONDS = 120.0
IDENTITY_FIELDS = (
    "st_dev", "st_ino", "st_mode", "st_nlink", "st_size",
    "st_mtime_ns", "st_ctime_ns",
)


def _emit(**value: object) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


def _owned_writer(home: str, configuration: str) -> int:
    """Run a native alias append or an explicitly ledger-only legacy writer."""
    root = FloatiRoot.open_direct_home(Path(home), create=False)
    config = json.loads(Path(configuration).read_text(encoding="utf-8"))
    relative = config["relative"]
    path = root.resolve_relative(relative)
    _emit(phase="started")
    if config["mode"] == "alias":
        root_stat = root.tenant_home.stat()
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        native_flock = fcntl.flock
        announced = False

        def observed_flock(descriptor: int, operation: int) -> object:
            nonlocal announced
            status = os.fstat(descriptor)
            if (
                not announced and operation & fcntl.LOCK_SH
                and (status.st_dev, status.st_ino) == root_identity
            ):
                announced = True
                _emit(phase="epoch_shared_attempt")
            return native_flock(descriptor, operation)

        try:
            # Call through the real kernel flock; no return value is replaced.
            with mock.patch.object(fcntl, "flock", observed_flock):
                jsonl.append_record(
                    root, relative, config["record"],
                    allowed_kinds=set(EVENT_KINDS),
                )
        except ProtocolRefusal as exc:
            _emit(phase="refused", code=exc.code)
            return 0
    elif config["mode"] == "ledger-only":
        lock = path.with_name(path.name + ".lock")
        with lock.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _emit(phase="ledger_probe", blocked=True)
            else:
                _emit(phase="ledger_probe", blocked=False)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        # Deliberately emulate a cooperating writer that knows the existing
        # ledger lock but has no epoch admission. The exact frame was produced
        # by the native fixture; this is not a public append-guard bypass.
        with jsonl._locked_path(
            lock, exclusive=True, relative=relative + ".lock",
            timeout_seconds=CHILD_SECONDS - 2,
        ):
            jsonl._append_frame(path, bytes.fromhex(config["frame_hex"]))
    else:
        raise AssertionError("unknown fixture writer mode")
    _emit(phase="appended")
    return 0


class _OwnedWriter:
    def __init__(self, home: Path, configuration: Path):
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
             "--owned-writer", str(home), str(configuration)],
            cwd=REPOSITORY, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.buffer = b""
        self.collected = False

    def next(self) -> dict[str, object]:
        assert self.process.stdout is not None
        readiness = Readiness("owned writer next synchronization phase")
        while b"\n" not in self.buffer:
            readiness.readable(self.process.stdout)
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                raise AssertionError("owned writer exited before its next phase")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def collect(self) -> None:
        stdout, stderr = self.process.communicate(timeout=CHILD_SECONDS)
        self.collected = True
        if self.process.returncode != 0 or stderr or stdout or self.buffer:
            raise AssertionError(
                "owned writer did not collect cleanly: "
                + repr((self.process.returncode, stdout, stderr, self.buffer))
            )

    def cleanup(self) -> None:
        if self.collected:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        self.process.communicate(timeout=CHILD_SECONDS)
        self.collected = True


class EpochWriterExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        # Compose the established native fixture without exporting another
        # TestCase class for unittest's module discovery.
        self.fixture = native_fixture.GovernedBusEpochRollTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.sequence = 0

    def _root(self, label: str) -> FloatiRoot:
        return self.fixture._new_root(
            self.fixture.base / label / self.fixture.root.tenant_id
        )

    def _seed(self, root: FloatiRoot) -> None:
        message = EventLog(root).send(
            "actor-a", "recipient", "floati", "a" * 40,
            "docs/evidence/exclusion.md", "native exclusion seed",
            idempotency_key="exclusion-seed",
        )
        EventLog(root).present("recipient")
        SparseCursor(root).ack(
            "recipient", [message["id"]], acting_session_id="exclusion-ack"
        )

    def _envelope(self, label: str) -> dict[str, object]:
        template = self._root("template-" + label)
        return dict(EventLog(template).send(
            "actor-a", "recipient", "floati", "b" * 40,
            "docs/evidence/exclusion.md", "native pending envelope",
            idempotency_key="pending-" + label,
        )["message"])

    def _writer(self, root: FloatiRoot, **config: object) -> _OwnedWriter:
        self.sequence += 1
        configuration = self.fixture.base / ("writer-%d.json" % self.sequence)
        configuration.write_text(json.dumps(config), encoding="utf-8")
        child = _OwnedWriter(root.tenant_home, configuration)
        self.addCleanup(child.cleanup)
        self.assertEqual({"phase": "started"}, child.next())
        return child

    @staticmethod
    def _selected_bytes(root: FloatiRoot) -> dict[str, bytes]:
        return {
            relative: path.read_bytes()
            for relative, path, _plane, identity
            in bus_epoch._preflight_selected_members(root)
            if identity is not None
        }

    def test_contained_alias_cannot_append_around_exclusive_epoch(self) -> None:
        root = self._root("alias")
        self._seed(root)
        envelope = self._envelope("alias")
        root.tenant_home.joinpath("alias.jsonl").symlink_to("events.jsonl")
        before = self._selected_bytes(root)
        with bus_epoch.epoch_guard(root, exclusive=True):
            child = self._writer(
                root, mode="alias", relative="alias.jsonl", record=envelope
            )
            phase = child.next()
            # Either refuse the alias before writing, or attempt the actual
            # root SH lock. An immediate append is the old lexical bypass.
            self.assertIn(phase["phase"], {"epoch_shared_attempt", "refused"})
            self.assertEqual(before, self._selected_bytes(root))
        if phase["phase"] == "epoch_shared_attempt":
            self.assertEqual({"phase": "appended"}, child.next())
            self.assertEqual(
                [envelope], [row for row in EventLog(root).records()
                             if row["id"] == envelope["id"]],
            )
        else:
            self.assertEqual(before, self._selected_bytes(root))
        child.collect()

    def test_surviving_marker_refuses_fresh_append_but_owned_recovery_works(self) -> None:
        class SimulatedStop(Exception):
            pass

        for state, boundary, direction in (
            ("PREPARED", "marker_prepared_durable", "rollback"),
            ("COMMITTED", "marker_committed_durable", "roll_forward"),
        ):
            with self.subTest(state=state):
                root = self._root("pending-" + state.lower())
                self._seed(root)
                self.fixture._grant_roll_authority(root, "actor-a")

                def stop(name: str) -> None:
                    if name == boundary:
                        raise SimulatedStop()

                with self.assertRaises(SimulatedStop):
                    bus_epoch.roll_bus_epoch(
                        root, actor="actor-a", idempotency_key="pending-marker",
                        fault=stop,
                    )
                self.assertEqual(state, bus_epoch.observe_epoch_roll_state(root)["classification"])
                before = self._selected_bytes(root)
                envelope = self._envelope(state.lower())
                # Explicit SH ownership proves marker admission, independently
                # of contention. No setup writer bypass is used in this test.
                with bus_epoch.epoch_guard(root, exclusive=False):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        jsonl.append_record(
                            root, "events.jsonl", envelope,
                            allowed_kinds=set(EVENT_KINDS),
                        )
                self.assertEqual("epoch_roll_recovery_required", caught.exception.code)
                self.assertEqual(before, self._selected_bytes(root))
                with bus_epoch.epoch_guard(root, exclusive=True):
                    result = bus_epoch.reconcile_epoch_roll(root)
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual((state, direction), (result["classification"], result["direction"]))
                self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(root)["classification"])
                if state == "PREPARED":
                    self.assertEqual(before, self._selected_bytes(root))
                else:
                    self.assertEqual([result["receipt"]], decode_frames(
                        root.resolve_relative("events.jsonl").read_bytes()
                    ))

    def test_selected_ledger_locks_span_archive_and_commit_before_swap(self) -> None:
        for boundary in ("before_archive_staging", "marker_committed_durable"):
            for relative in (
                "events.jsonl", "receipts/deliveries/recipient.jsonl",
                "receipts/acks/recipient.jsonl",
            ):
                with self.subTest(boundary=boundary, relative=relative):
                    root = self._root("ledger-%d" % self.sequence)
                    self._seed(root)
                    self.fixture._grant_roll_authority(root, "actor-a")
                    original = self._selected_bytes(root)
                    # Reuse an authentic native frame in each selected plane.
                    frame = encode_frame(decode_frames(original[relative])[-1])
                    children: list[_OwnedWriter] = []

                    def contend(name: str) -> None:
                        if name != boundary:
                            return
                        child = self._writer(
                            root, mode="ledger-only", relative=relative,
                            frame_hex=frame.hex(),
                        )
                        children.append(child)
                        self.assertEqual(
                            {"phase": "ledger_probe", "blocked": True},
                            child.next(),
                            "selected ledger lock must already exclude a cooperating process",
                        )
                        self.assertEqual(original, self._selected_bytes(root))

                    result = bus_epoch.roll_bus_epoch(
                        root, actor="actor-a", idempotency_key="ledger-exclusion",
                        fault=contend,
                    )
                    self.assertEqual(1, len(children))
                    child = children[0]
                    self.assertEqual({"phase": "appended"}, child.next())
                    child.collect()
                    archive = Path(result["receipt"]["archive_path"])
                    self.assertEqual(original[relative], (archive / relative).read_bytes())
                    expected = (
                        encode_frame(result["receipt"]) + frame
                        if relative == "events.jsonl" else frame
                    )
                    self.assertEqual(expected, root.resolve_relative(relative).read_bytes())

    def test_replacement_still_refuses_each_changed_identity_field(self) -> None:
        root = self._root("identity")
        self._seed(root)
        self.fixture._grant_roll_authority(root, "actor-a")
        bus_epoch.roll_bus_epoch(root, actor="actor-a", idempotency_key="identity-seed")
        path = root.resolve_relative("events.jsonl")
        receipt_frame = path.read_bytes()
        original_fstat = os.fstat
        target = (path.stat().st_dev, path.stat().st_ino)
        for field in IDENTITY_FIELDS:
            with self.subTest(field=field), bus_epoch.epoch_guard(root, exclusive=True):
                selected = bus_epoch._preflight_selected_members(root)

                def changed(descriptor: int) -> object:
                    status = original_fstat(descriptor)
                    if (status.st_dev, status.st_ino) != target:
                        return status
                    values = {name: getattr(status, name) for name in IDENTITY_FIELDS}
                    values[field] += 1
                    return SimpleNamespace(**values)

                with mock.patch.object(os, "fstat", changed):
                    with self.assertRaises(DurabilityFailure) as caught:
                        jsonl._replace_epoch_selected(root, selected, receipt_frame)
                self.assertEqual("epoch_selected_member_changed", caught.exception.code)
                self.assertIn(field, caught.exception.detail)
                self.assertEqual(receipt_frame, path.read_bytes())

    def test_selected_writer_marker_refusal_precedes_ancillary_work_and_keeps_reads_available(self) -> None:
        class SimulatedStop(Exception):
            pass

        for state, boundary in (
            ("PREPARED", "marker_prepared_durable"),
            ("COMMITTED", "marker_committed_durable"),
        ):
            root = self._root("native-pending-" + state.lower())
            self._seed(root)
            self.fixture._grant_roll_authority(root, "actor-a")
            message_id = EventLog(root).records()[0]["id"]

            def stop(name: str) -> None:
                if name == boundary:
                    raise SimulatedStop()

            with self.assertRaises(SimulatedStop):
                bus_epoch.roll_bus_epoch(
                    root, actor="actor-a", idempotency_key="native-pending",
                    fault=stop,
                )
            before = self.fixture._snapshot(root)
            # Read-only SH and the native event projection remain usable. A
            # surviving marker is writer admission, not a blanket read ban.
            with bus_epoch.epoch_guard(root, exclusive=False):
                self.assertEqual([message_id], [row["id"] for row in EventLog(root).records()])
            # Exercise native selected-writer admission below the authorized
            # public automatic recovery boundary. Each immediate __wrapped__
            # retains SH and the marker check; none bypasses admission.
            operations = {
                "send": lambda: EventLog.send.__wrapped__(EventLog(root),
                    "actor-a", "recipient", "floati", "c" * 40,
                    "docs/evidence/exclusion.md", "must refuse before readiness",
                    idempotency_key="must-not-send",
                ),
                "present": lambda: EventLog.present.__wrapped__(EventLog(root), "recipient"),
                "ack": lambda: SparseCursor.ack.__wrapped__(SparseCursor(root),
                    "recipient", [message_id], acting_session_id="must-not-ack",
                ),
                "quarantine": lambda: LedgerRepair(root).quarantine(
                    "events.jsonl", message_id, key="must-not-repair",
                ),
            }
            for operation, invoke in operations.items():
                with self.subTest(state=state, operation=operation):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        invoke()
                    self.assertEqual("epoch_roll_recovery_required", caught.exception.code)
                    self.assertEqual(
                        before, self.fixture._snapshot(root),
                        "early admission must preserve selected and ancillary control leaves",
                    )

    def test_public_activity_recovers_marker_then_retries_native_operation(self) -> None:
        class SimulatedStop(Exception):
            pass

        for state, boundary in (
            ("PREPARED", "marker_prepared_durable"),
            ("COMMITTED", "marker_committed_durable"),
        ):
            for operation in ("send", "present", "ack"):
                with self.subTest(state=state, operation=operation):
                    root = self._root("public-recovery-" + state.lower() + "-" + operation)
                    self._seed(root)
                    self.fixture._grant_roll_authority(root, "actor-a")
                    message_id = EventLog(root).records()[0]["id"]

                    def stop(name: str) -> None:
                        if name == boundary:
                            raise SimulatedStop()

                    with self.assertRaises(SimulatedStop):
                        bus_epoch.roll_bus_epoch(root, actor="actor-a",
                            idempotency_key="public-recovery", fault=stop)
                    if operation == "send":
                        sent = EventLog(root).send("actor-a", "recipient", "floati", "c" * 40,
                            "docs/evidence/exclusion.md", "native retry after recovery",
                            idempotency_key="recovered-send")
                        self.assertIn(sent["id"], EventLog(root).unacked_ids("recipient"))
                    elif operation == "present":
                        rows, delivery = EventLog(root).present("recipient")
                        self.assertEqual([], rows)
                        self.assertIsNone(delivery)
                    elif state == "COMMITTED":
                        with self.assertRaises(ProtocolRefusal) as caught:
                            SparseCursor(root).ack("recipient", [message_id],
                                acting_session_id="after-recovery")
                        self.assertEqual("ack_item_archived", caught.exception.code)
                    else:
                        receipt = SparseCursor(root).ack("recipient", [message_id],
                            acting_session_id="after-recovery")
                        self.assertEqual([message_id], receipt["item_ids"])
                    self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(root)["classification"])

    def test_sealed_replacement_refuses_changed_coordinates_and_escaped_scope(self) -> None:
        root = self._root("sealed-scope")
        self._seed(root)
        selected = bus_epoch._preflight_selected_members(root)
        frame = encode_frame(self._envelope("sealed"))
        with bus_epoch.epoch_guard(root, exclusive=True):
            with jsonl._epoch_selected_writer(root, selected) as replace:
                before = self.fixture._snapshot(root)
                additional = (
                    "receipts/acks/unlocked.jsonl",
                    root.resolve_relative("receipts/acks/unlocked.jsonl"),
                    "acks", None,
                )
                with self.assertRaises(ProtocolRefusal) as caught:
                    replace(selected + (additional,), frame)
                self.assertEqual("epoch_selected_member_changed", caught.exception.code)
                self.assertEqual(before, self.fixture._snapshot(root))
            # Keep EX owned: refusal must come from the capability having
            # expired, not merely from losing the enclosing epoch barrier.
            with self.assertRaises(ProtocolRefusal) as caught:
                replace(selected, frame)
            self.assertEqual("epoch_selected_writer_scope_invalid", caught.exception.code)
            self.assertEqual(before, self.fixture._snapshot(root))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--owned-writer":
        raise SystemExit(_owned_writer(sys.argv[2], sys.argv[3]))
    unittest.main()
