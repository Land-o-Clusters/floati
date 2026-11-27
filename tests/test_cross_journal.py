"""The cross journal storage layer under crashes, power loss and races.

A new module: no existing one covers the cross journal. `test_journal_chain`
pins the house `JournalChain` and its verbs, whose contract differs.
"""

from __future__ import annotations

import errno
import hashlib
import inspect
import os
import random
import shutil
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from queue import Full, Queue
from typing import Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

from floati.cross_authority import Capability
from floati.cross_journal import Storage, genesis_head
from floati.cross_journal import CrossJournal as ProductionJournal
from floati.cross_reserve import Budget
from floati.errors import FloatiError, ProtocolRefusal
from floati.framing import encode_frame
from tests import cross_journal_harness as harness
from tests.cross_journal_harness import CrossJournal


ENABLE_ONE = "1" * 32
ENABLE_TWO = "2" * 32
ENABLE_OTHER = "3" * 32
_DELIVERY_TOKEN = bytes(range(32))


class _OwnedEnableInventory:
    def expected(self, item, start):
        return hashlib.sha256(encode_frame({"item": item, "start": start})).hexdigest()

    def read(self, item, start):
        return "owned", self.expected(item, start)


class _OutputStream:
    def __init__(self, direction):
        self.direction = direction
        self.ready = True
        self.queue = Queue(maxsize=1)
        self.denied = []

    def permit_local_output(self, direction):
        return self.ready and direction == self.direction

    def enqueue_local_output(self, frame):
        try:
            self.queue.put_nowait(frame)
        except Full:
            self.denied.append(frame)
            return False
        return True


def _write(journal, capability, record, **options):
    """Mint the named entry point's fixture capability around a synthetic effect."""
    capability = Capability(capability) if isinstance(capability, str) else capability
    writer = capability.writer
    kind = record["kind"]
    if writer == "I":
        verb = {
            "remote_inbound": "envelope", "remote_peer_receipt": "receipt",
            "remote_receipt_confirmed": "receipt_ack", "remote_session_opened": "admission",
            "remote_session_refused": "admission", "remote_endpoint_registered": "endpoint_start",
            "remote_attempt": "reconcile", "remote_transport_attempt": "reconcile",
            "cross_pair_seen": "admission", "remote_unpair_notice_received": "notice",
        }.get(kind)
        pair = capability.pair_id or record.get("pair_id")
        if verb == "endpoint_start":
            record = {key: value for key, value in record.items() if key != "pair_id"}
            pair = None
        capability = Capability(writer, pair, verb)
    elif writer == "A":
        if kind in {"cross_optin", "cross_inventory_item"}:
            capability = Capability(writer, verb="enable")
        else:
            ceremony = record.get("ceremony_id", "")
            token = hashlib.sha256(ceremony.encode("ascii")).hexdigest()[:32]
            record = dict(record, ceremony_token=token)
            if kind == "cross_ceremony_closed":
                record["schema_version"] = 1
            capability = Capability(writer, verb=capability.verb or "pair_show", ceremony_token=token)
    elif writer == "L":
        verb = {
            "cross_pair_revoked": "unpair" if record.get("cause") == "local" else "disable",
            "cross_peer_address": "peer_address", "remote_presented": "present", "remote_local_ack": "ack",
            "remote_outbound": "send", "remote_attempt": "send", "remote_transport_attempt": "send",
        }.get(kind, "disable")
        capability = Capability(writer, record.get("pair_id"), verb)
        if kind == "remote_local_ack" and "output_token" not in options:
            options["output_token"] = _DELIVERY_TOKEN
            release = next(row for row in reversed(journal.read_committed().records)
                           if row["kind"] == "remote_presented")
            record = dict(record, release_id=release["id"])
    elif writer == "M":
        capability = Capability(writer, verb="maintain")
    return journal.append(capability, record, **options)


def data_record(number: int) -> Dict[str, object]:
    return {
        "schema_version": 0,
        "id": f"remote-inbound-{number:04d}",
        "kind": "remote_inbound",
        "pair_id": "p1",
        "wire_seq": number,
    }


def _append_third(journal: CrossJournal) -> object:
    return _write(journal, "I", data_record(3))


def _create(journal: CrossJournal) -> object:
    return journal.create(ENABLE_ONE)


def _maintain(journal: CrossJournal) -> object:
    return journal.maintain("M")


def _compact(journal: CrossJournal) -> object:
    return journal.compact("L", "explicit")


class CrossJournalCrashMatrix(unittest.TestCase):
    """X27″, X6″ and X39′, with the reviewer's composition of failures.

    For every numbered point of a writer, recovery or compaction: SIGKILL the
    writer there, let an already-running reader in another process read,
    then reconstruct each directory a power loss could leave and recover it
    twice. Every record the reader exposed must survive in every image, and
    recovery must succeed and be idempotent.
    """

    reader: harness.ReaderProcess

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls._temporary.name).resolve()
        cls.reader = harness.ReaderProcess()
        cls.states: Dict[str, Dict[str, bytes]] = {}
        cls.models: Dict[str, dict] = {}

        empty = cls.base / "empty"
        empty.mkdir()
        cls._keep("empty", empty, harness.adopted_model(empty))

        two = cls.base / "two" / "cross"
        two.parent.mkdir()
        storage = harness.TracingStorage(two, model={"current": {}, "durable": {}, "data": {}, "next": 0})
        journal = CrossJournal(two, storage=storage)
        journal.create(ENABLE_ONE)
        _write(journal, "A", _optin("enable_complete", None))
        _write(journal, "A", _opened("original"))
        _write(journal, "A", _activation("p1", "original"))
        _write(journal, "I", data_record(1))
        _write(journal, "I", data_record(2))
        cls._keep("two", two, storage.model)

        delivery = cls.base / "completion" / "cross"
        harness.materialize(harness.snapshot(two), delivery)
        storage = harness.TracingStorage(delivery, model=_copy(cls.models["two"]))
        inbound = _accepted_delivery()
        assert _write(CrossJournal(delivery, storage=storage), "I", inbound).status == "committed"
        cls._keep("completion", delivery, storage.model)

        # A tail: the third append dies after its journal sync, before its checkpoint.
        _, labels = harness.count_points(two, _append_third)
        after_sync = next(
            index + 1 for index, label in enumerate(labels)
            if label.startswith("sync:") and label.endswith(".jsonl")
        )
        tail = cls.base / "tail" / "cross"
        harness.materialize(harness.snapshot(two), tail)
        storage = harness.TracingStorage(tail, model=_copy(cls.models["two"]), stop_at=after_sync)
        try:
            _append_third(CrossJournal(tail, storage=storage))
        except harness.SimulatedCrash:
            pass
        cls._keep("tail", tail, storage.model)

        # History compaction can retire: closed ceremonies around one active pair.
        history = cls.base / "history" / "cross"
        harness.materialize(harness.snapshot(two), history)
        storage = harness.TracingStorage(history, model=_copy(cls.models["two"]))
        journal = CrossJournal(history, storage=storage)
        for capability, record in (
            ("A", _record("cross_ceremony_opened", "cross-ceremony-1", ceremony_id="c1")),
            ("A", _ceremony_closed("c1")),
            ("A", _record("cross_ceremony_opened", "cross-ceremony-2", ceremony_id="c2")),
            ("A", _record("cross_pair_active", "cross-pair-active-p2", pair_id="p2", ceremony_id="c2",
                          ceremony_token="t" * 32, transcript_receipt_digest="0" * 64)),
        ):
            assert _write(journal, capability, record).status == "committed"
        cls._keep("history", history, storage.model)

    @classmethod
    def _keep(cls, name: str, directory: Path, model: dict) -> None:
        cls.states[name] = harness.snapshot(directory)
        cls.models[name] = _copy(model)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.reader.close()
        cls._temporary.cleanup()

    def _workdir(self, label: str) -> Path:
        directory = self.base / "work" / label / "cross"
        if directory.parent.exists():
            shutil.rmtree(directory.parent)
        directory.parent.mkdir(parents=True)
        return directory

    def _matrix(self, state: str, scenario: Callable[[CrossJournal], object]) -> List[str]:
        """Kill the scenario at every point; return each violated property."""

        violations: List[str] = []
        images_checked = 0
        origin = self._workdir("origin")
        harness.materialize(self.states[state], origin)
        points, labels = harness.count_points(origin, scenario)
        # Point `points` is completion: no kill, then the same power-loss images.
        for point in range(points + 1):
            label = labels[point - 1] if point else "start"
            work = self._workdir("kill")
            harness.materialize(self.states[state], work)
            model = _copy(self.models[state])
            harness.kill_at(work, model, scenario, point)
            observed = self.reader.read(work)
            if "error" in observed:
                violations.append(f"{state} point {point} after {label}: reader refused {observed['error']}")
            for image_label, files in harness.power_loss_images(work, model).items():
                found = self._workdir("image")
                harness.materialize(files, found)
                images_checked += 1
                recovered, failure = harness.recover_twice(found)
                where = f"{state} point {point} after {label}, {image_label}"
                if failure is not None:
                    violations.append(f"{where}: recovery failed {failure}")
                    continue
                final, failure = harness.final_view(found)
                if failure is not None:
                    violations.append(f"{where}: read after recovery refused {failure}")
                    continue
                missing = harness.lost(observed, final)
                if missing:
                    violations.append(f"{where}: exposed then lost {missing}")
        # The matrix is not vacuous: every point ran, and power loss produced
        # images that differ from what process death leaves.
        if points < 2 or images_checked <= points:
            violations.append(f"{state}: only {points} points and {images_checked} images were checked")
        return violations

    def assertNoViolations(self, violations: List[str]) -> None:
        if violations:
            shown = "\n".join(violations[:24])
            self.fail(f"{len(violations)} violations; first {min(24, len(violations))}:\n{shown}")

    def test_append_crash_never_publishes_an_undurable_record(self) -> None:
        self.assertNoViolations(self._matrix("two", _append_third))

    def test_completion_crash_never_publishes_an_undurable_record(self) -> None:
        self.assertNoViolations(self._matrix("completion", _append_completion))

    def test_create_crash_never_publishes_an_undurable_record(self) -> None:
        self.assertNoViolations(self._matrix("empty", _create))

    def test_recovery_crash_never_publishes_and_always_resumes(self) -> None:
        self.assertNoViolations(self._matrix("tail", _maintain))

    def test_compaction_crash_never_publishes_and_always_resumes(self) -> None:
        self.assertNoViolations(self._matrix("history", _compact))

    def _found_states(self, sources) -> List[Dict[str, bytes]]:
        """Every distinct directory a crash of the named scenarios leaves."""

        found: Dict[tuple, Dict[str, bytes]] = {}
        for state, scenario in sources:
            origin = self._workdir("origin")
            harness.materialize(self.states[state], origin)
            points, _ = harness.count_points(origin, scenario)
            for point in range(points):
                work = self._workdir("kill")
                harness.materialize(self.states[state], work)
                storage = harness.TracingStorage(work, model=_copy(self.models[state]), stop_at=point)
                try:
                    scenario(CrossJournal(work, storage=storage))
                except (harness.SimulatedCrash, FloatiError, OSError):
                    pass
                for files in harness.power_loss_images(work, storage.model).values():
                    found.setdefault(tuple(sorted(files.items())), files)
        return list(found.values())

    def test_an_interrupted_recovery_converges_after_a_writer_or_recovery_crash(self) -> None:
        self.assertNoViolations(self._converges((("two", _append_third), ("tail", _maintain))))

    def test_an_interrupted_recovery_converges_after_a_compaction_crash(self) -> None:
        self.assertNoViolations(self._converges((("history", _compact),)))

    def _converges(self, sources) -> List[str]:
        """Interrupt recovery at every point, then lose power: the end state is unchanged.

        For each directory a crash can leave, recovery run to completion gives
        one reference. Stopping that recovery at any point, letting the reader
        read, reconstructing every power-loss image and recovering again must
        give the reference byte for byte, and lose nothing the reader exposed.
        """

        violations: List[str] = []
        interrupted = 0
        for number, files in enumerate(self._found_states(sources)):
            reference_directory = self._workdir("reference")
            harness.materialize(files, reference_directory)
            reference, failure = harness.recover_twice(reference_directory)
            if failure is not None:
                violations.append(f"state {number}: uninterrupted recovery failed {failure}")
                continue
            kept, _ = harness.final_view(reference_directory)
            points, labels = harness.count_points(self._seeded(files), _maintain)
            for point in range(points):
                work = self._workdir("interrupt")
                harness.materialize(files, work)
                storage = harness.TracingStorage(work, stop_at=point)
                try:
                    _maintain(CrossJournal(work, storage=storage))
                except (harness.SimulatedCrash, FloatiError, OSError):
                    pass
                observed = self.reader.read(work)
                for image_label, image in harness.power_loss_images(work, storage.model).items():
                    found = self._workdir("image")
                    harness.materialize(image, found)
                    recovered, failure = harness.recover_once(found)
                    interrupted += 1
                    where = f"state {number} stopped after {labels[point - 1] if point else 'start'}, {image_label}"
                    if failure is not None:
                        violations.append(f"{where}: recovery failed {failure}")
                    elif recovered != reference:
                        violations.append(f"{where}: recovery diverged from the uninterrupted result")
                    else:
                        # Equal bytes, so the reference's committed records are these.
                        missing = harness.lost(observed, kept)
                        if missing:
                            violations.append(f"{where}: exposed then lost {missing}")
        if interrupted < 50:
            violations.append(f"only {interrupted} interrupted recoveries were checked")
        return violations

    def _seeded(self, files: Dict[str, bytes]) -> Path:
        directory = self._workdir("seed")
        harness.materialize(files, directory)
        return directory

    def test_a_failed_sync_reports_an_outcome_recovery_honours(self) -> None:
        """Every I/O failure reports the durability boundary it reached."""

        violations: List[str] = []
        origin = self._workdir("origin")
        harness.materialize(self.states["two"], origin)
        points, labels = harness.count_points(origin, _append_third)
        rename = labels.index("rename:journal.commit")
        directory_sync = labels.index("sync_directory:.", rename)
        outcomes = set()
        for point in range(points):
            work = self._workdir("fail")
            harness.materialize(self.states["two"], work)
            storage = harness.TracingStorage(work, model=_copy(self.models["two"]), fail_at=point)
            try:
                outcome = _append_third(CrossJournal(work, storage=storage)).status
            except FloatiError as exc:
                outcome = exc.code
            outcomes.add(outcome)
            if point < rename:
                expected = "failed"
            elif point <= directory_sync:
                expected = "indeterminate"
            else:
                expected = "committed_unpublished"
            if outcome != expected:
                violations.append(f"failure at {labels[point]}: expected {expected}, got {outcome}")
            observed = self.reader.read(work)
            if outcome == "committed_unpublished":
                if not observed or not observed.get("pending"):
                    violations.append("the second process reported a conclusive read without the current seal")
                if data_record(3)["id"] in (observed or {"ids": []})["ids"]:
                    violations.append("the reader exposed a record without its seal")
            for image_label, files in harness.power_loss_images(work, storage.model).items():
                found = self._workdir("image")
                harness.materialize(files, found)
                recovered, failure = harness.recover_twice(found)
                where = f"failure at {labels[point]} -> {outcome}, {image_label}"
                if failure is not None:
                    violations.append(f"{where}: recovery failed {failure}")
                    continue
                final, _ = harness.final_view(found)
                present = data_record(3)["id"] in (final or {"ids": []})["ids"]
                if expected == "committed_unpublished" and not present:
                    violations.append(f"{where}: a committed record was lost")
                if outcome == "failed" and present:
                    violations.append(f"{where}: a failed record became committed")
                missing = harness.lost(observed, final)
                if missing:
                    violations.append(f"{where}: exposed then lost {missing}")
        if outcomes != {"failed", "indeterminate", "committed_unpublished"}:
            violations.append(f"the failure matrix constructed {sorted(outcomes)}")
        self.assertNoViolations(violations)

    def test_unpublished_first_enable_does_not_bind_after_repair(self) -> None:
        frame = encode_frame(dict(_optin("enable_started", None), enable_generation=1, seq=1,
                                  prev=genesis_head(f"cross-{ENABLE_ONE}-g1")))
        for tail in (frame[: len(frame) // 2], frame):
            with self.subTest(complete=tail.endswith(b"\n")):
                directory = self._seeded(self.states["empty"])
                crash_after_seal = lambda name: name == "rename:journal.commit.seal"
                crashing = harness.TracingStorage(directory, stop_after=crash_after_seal)
                with self.assertRaises(harness.SimulatedCrash):
                    CrossJournal(directory, storage=crashing).create(ENABLE_ONE)
                storage = harness.TracingStorage(directory)
                storage.append("journal-g1.jsonl", tail)
                CrossJournal(directory, storage=storage).maintain()
                adoption_cut = harness.TracingStorage(directory, stop_after=crash_after_seal)
                with self.assertRaises(harness.SimulatedCrash):
                    CrossJournal(directory, storage=adoption_cut).create(ENABLE_TWO)
                view = CrossJournal(directory, storage=storage).create(ENABLE_TWO)
                self.assertEqual([ENABLE_TWO], [r["enable_id"] for r in view.records
                                               if r.get("phase") == "enable_started"])
                repairs = [r for r in view.records if r["kind"] == "cross_journal_tail_repaired"]
                self.assertEqual([(0, len(tail), hashlib.sha256(tail).hexdigest())],
                                 [(r["offset"], r["tail_len"], r["tail_sha256"]) for r in repairs])
                self.assertEqual(view, CrossJournal(directory).create(ENABLE_TWO))

    def test_legacy_tail_recovers_in_its_own_format_with_admission_fenced(self) -> None:
        directory = self._seeded(self.states["empty"])
        directory.mkdir(exist_ok=True)
        legacy_id = "cross-legacy-g1"
        journal_path = directory / "journal-g1.jsonl"
        journal_path.write_bytes(b'{"kind":')
        checkpoint = encode_frame({
            "format": "floati-cross-checkpoint-v1", "journal_file": journal_path.name,
            "journal_id": legacy_id, "generation": 1, "through_seq": 0,
            "head_sha256": genesis_head(legacy_id), "byte_length": 0,
            "previous": None, "repair_intent": None, "compaction_intent": None,
        })
        checkpoint_path = directory / "journal.commit"
        checkpoint_path.write_bytes(checkpoint)
        (directory / "journal.commit.seal").write_bytes(encode_frame({
            "format": "floati-cross-seal-v1", "checkpoint_sha256": hashlib.sha256(checkpoint).hexdigest(),
        }))
        journal = CrossJournal(directory)
        self.assertEqual("repaired", journal.maintain()["state"])
        view = journal.read_committed(require_settled=True)
        self.assertEqual(["cross_journal_tail_repaired"], [row["kind"] for row in view.records])
        self.assertIn(b'"floati-cross-checkpoint-v1"', checkpoint_path.read_bytes())
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(journal, "I", data_record(1))
        self.assertEqual("cross_legacy_admission_fenced", caught.exception.code)
        self.assertEqual(view, journal.read_committed(require_settled=True))

    def test_missing_local_prefix_requires_independent_absence(self) -> None:
        from floati.cross_anchor import Anchor, Handoff, Narrowing
        from floati.cross_journal import verify_prefix
        from floati.errors import IntegrityFailure

        class Store:
            def __init__(self, value):
                self.value = value

            def read(self):
                if self.value == "unavailable":
                    raise OSError(errno.EIO, "unknown independent evidence")
                return self.value

        brand_new = self.base / "new-root"
        self.assertEqual("absent", CrossJournal(brand_new, anchor_store=Store(None), fresh_root=lambda: True, missing_exclusion=nullcontext)
                         .read_committed(require_settled=True).state)
        brand_new.mkdir()
        self.assertEqual("absent", CrossJournal(brand_new, anchor_store=Store(None), fresh_root=lambda: True, missing_exclusion=nullcontext)
                         .read_committed(require_settled=True).state)
        anchor = Anchor((ENABLE_ONE, 1, 1, "a" * 64), 1, "b" * 64)
        narrowing = Narrowing(anchor, (), "c" * 64, b"{}\n")
        handoff = Handoff(anchor, (), "c" * 64, "d" * 64, (), "e" * 64, "explicit")
        for name, value in (("stable", anchor), ("narrowing", narrowing),
                            ("handoff", handoff), ("store unavailable", "unavailable")):
            for exists in (False, True):
                with self.subTest(value=name, directory_exists=exists):
                    directory = self.base / f"missing-{name}-{exists}"
                    if exists:
                        directory.mkdir()
                    journal = CrossJournal(directory, anchor_store=Store(value))
                    self.assertTrue(journal.read_committed().pending)
                    with self.assertRaises(ProtocolRefusal) as caught:
                        journal.read_committed(require_settled=True)
                    self.assertEqual("cross_publication_pending", caught.exception.code)
                    self.assertFalse((directory / "journal.commit").exists())
        journal_id = f"cross-{ENABLE_ONE}-g1"
        frame = encode_frame(dict(_optin("enable_started", None), seq=True, prev=genesis_head(journal_id)))
        with self.assertRaises(IntegrityFailure):
            verify_prefix(frame, journal_id)

    def test_missing_local_state_checks_independent_evidence(self) -> None:
        class EmptyStore:
            def read(self):
                return None

        new_root = self.base / "unverified-missing"
        unknown = CrossJournal(new_root, anchor_store=EmptyStore())
        self.assertTrue(unknown.read_committed().pending)
        with self.assertRaises(ProtocolRefusal):
            unknown.read_committed(require_settled=True)
        known = CrossJournal(new_root, anchor_store=EmptyStore(), fresh_root=lambda: True, missing_exclusion=nullcontext)
        self.assertEqual("absent", known.read_committed(require_settled=True).state)

    def test_r9_missing_state_preserves_pending_create_handoff_and_migration(self) -> None:
        from floati.cross_anchor import Anchor, EnableCreate, Handoff, OutstandingWrite, QuotaMigration

        class Store:
            def __init__(self, value):
                self.value = value

            def read(self):
                return self.value

        anchor = Anchor((ENABLE_ONE, 1, 1, "a" * 64), 1, "b" * 64)
        old = ("journal-g1.jsonl", f"cross-{ENABLE_ONE}-g1", 1, 1, "c" * 64, 123)
        new = ("journal-g2.jsonl", f"cross-{ENABLE_ONE}-g2", 2, 2, "d" * 64, 234)
        values = (
            (EnableCreate(anchor, old, "e" * 64, "f" * 32, "0" * 64), None),
            (Handoff(anchor, old, "e" * 64, "f" * 64, new, "0" * 64, "explicit"), None),
            (OutstandingWrite(), None),
            (None, QuotaMigration(old, "e" * 64, new, "0" * 64, "a" * 64, "b" * 32)),
        )
        for number, (anchor_value, migration_value) in enumerate(values):
            for established_lock in (False, True):
                with self.subTest(value=number, lock=established_lock):
                    directory = self.base / f"missing-r9-{number}-{established_lock}"
                    path = directory / "journal.lock"
                    if established_lock:
                        directory.mkdir()
                        path.touch()
                    before = (path.stat().st_dev, path.stat().st_ino) if established_lock else None
                    reader = CrossJournal(directory, anchor_store=Store(anchor_value), quota_store=Store(migration_value),
                                          fresh_root=lambda: True, missing_exclusion=nullcontext)
                    self.assertTrue(reader.read_committed().pending)
                    with self.assertRaises(ProtocolRefusal):
                        reader.read_committed(require_settled=True)
                    self.assertEqual(established_lock, directory.exists())
                    if established_lock:
                        self.assertEqual(["journal.lock"], [entry.name for entry in directory.iterdir()])
                        self.assertEqual(before, (path.stat().st_dev, path.stat().st_ino))
                    self.assertEqual([], reader.census)

    def test_r9_missing_read_uses_independent_exclusion_and_keeps_paths(self) -> None:
        from contextlib import contextmanager

        seen = []
        class Store:
            def read(self):
                seen.append("store")
                return None

        @contextmanager
        def exclusion():
            seen.append("take")
            yield
            seen.append("release")

        directory = self.base / "r9-unmaterialized"
        unqualified = CrossJournal(directory, anchor_store=Store(), fresh_root=lambda: True)
        self.assertEqual("pending", unqualified.read_committed().state)
        self.assertFalse(directory.exists())

        for existing in (False, True):
            if existing:
                directory.mkdir()
            reader = CrossJournal(directory, anchor_store=Store(), fresh_root=lambda: True,
                                  missing_exclusion=exclusion)
            self.assertEqual("absent", reader.read_committed(require_settled=True).state)
            self.assertEqual(existing, directory.exists())
            if existing:
                self.assertEqual([], list(directory.iterdir()))
        self.assertEqual(["take", "store", "release", "take", "store", "release"], seen)

    def test_root_scoped_reader_requires_both_store_slots_and_monotone_provenance(self) -> None:
        import fcntl
        from contextlib import contextmanager

        from floati.cross_anchor import OutstandingWrite
        from floati.root import FloatiRoot

        home = self.base / "root-read"
        home.mkdir()
        root = FloatiRoot.open_direct_home(home)
        directory = home / "cross"
        lock = home / "root.exclusion"
        lock.touch()
        original = lock.stat()

        class Source:
            held = False
            established = False

            @contextmanager
            def exclude(self, scope):
                self.assert_scope(scope)
                with lock.open("rb") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    self.held = True
                    try:
                        yield
                    finally:
                        self.held = False

            def read(self, scope):
                self.assert_scope(scope)
                self_test.assertTrue(self.held)
                return "established" if self.established else "never_established"

            def mark_established(self, scope):
                self.assert_scope(scope)
                self_test.assertTrue(self.held)
                self.established = True
                return True

            def assert_scope(self, scope):
                self_test.assertEqual(root, scope)

        class Slot:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, replacement):
                if self.value != expected:
                    return False
                self.value = replacement
                return True

        self_test = self
        source, anchor, quota = Source(), Slot(), Slot()
        self.assertIn("provenance", inspect.signature(CrossJournal.for_root).parameters)
        reader = CrossJournal.for_root(root, provenance=source, anchor_store=anchor, quota_store=quota)
        for slot in (anchor, quota):
            slot.value = OutstandingWrite()
            self.assertEqual("pending", reader.read_committed().state)
            slot.value = None
        incomplete = CrossJournal.for_root(root, provenance=source, anchor_store=anchor)
        self.assertEqual("pending", incomplete.read_committed().state)
        self.assertEqual("absent", reader.read_committed(require_settled=True).state)
        self.assertEqual([lock], list(home.iterdir()))
        directory.mkdir()
        (directory / "journal.lock").touch()
        self.assertEqual("pending", reader.resolve(Capability("L", verb="disable"),
                                                   _optin("disable_started", "local")))
        self.assertEqual("absent", reader.read_committed(require_settled=True).state)
        reader.create(ENABLE_ONE)
        self.assertTrue(source.established)
        shutil.rmtree(directory)
        anchor.value = None
        self.assertEqual("pending", reader.read_committed().state)
        self.assertEqual([lock], list(home.iterdir()))
        self.assertTrue(os.path.samestat(original, lock.stat()))

    def test_missing_state_read_leaves_every_path_and_the_lock_identity_unchanged(self) -> None:
        class EmptyStore:
            def read(self):
                return None

        def contents(directory):
            if not directory.is_dir():
                return None
            return {path.name: (path.stat().st_dev, path.stat().st_ino, path.read_bytes())
                    for path in directory.iterdir()}

        cases = (("no backend", None, None, "pending"),
                 ("established", EmptyStore(), lambda: False, "pending"),
                 ("new", EmptyStore(), lambda: True, "absent"))
        for label, store, provenance, expected in cases:
            for image in ("missing directory", "empty directory", "existing lock"):
                with self.subTest(label=label, image=image):
                    directory = self.base / (label + "-" + image)
                    if image != "missing directory":
                        directory.mkdir()
                    if image == "existing lock":
                        (directory / "journal.lock").write_bytes(b"")
                    before = contents(directory)
                    journal = CrossJournal(directory, anchor_store=store, fresh_root=provenance, missing_exclusion=nullcontext)
                    self.assertEqual(expected, journal.read_committed().state)
                    self.assertEqual(before, contents(directory))
                    if expected == "pending":
                        with self.assertRaises(ProtocolRefusal):
                            journal.read_committed(require_settled=True)
                    else:
                        self.assertEqual("absent", journal.read_committed(require_settled=True).state)
                    self.assertEqual(before, contents(directory))

    def test_unsettled_tail_refuses_a_conclusive_read(self) -> None:
        directory = self._seeded(self.states["tail"])
        journal = CrossJournal(directory)
        with self.assertRaises(ProtocolRefusal) as caught:
            journal.read_committed(require_settled=True)
        self.assertEqual("cross_publication_pending", caught.exception.code)
        journal.maintain()
        view = journal.read_committed(require_settled=True)
        self.assertNotIn(data_record(3)["id"], [record["id"] for record in view.records])
        self.assertFalse(view.pending)

    def test_first_enable_requires_the_exact_wire_identity(self) -> None:
        for malformed in ("short-id", "A" * 32, "a" * 31, "a" * 33):
            with self.subTest(enable_id=malformed):
                directory = self._seeded(self.states["empty"])
                with self.assertRaises(ProtocolRefusal) as caught:
                    CrossJournal(directory).create(malformed)
                self.assertEqual("cross_enable_id_invalid", caught.exception.code)
                self.assertEqual((), CrossJournal(directory).read_committed().records)

    def test_genesis_without_a_record_does_not_bind_the_next_enable(self) -> None:
        for enable_id in (ENABLE_ONE, ENABLE_TWO):
            with self.subTest(enable_id=enable_id):
                directory = self._seeded(self.states["empty"])
                crashing = harness.TracingStorage(
                    directory, stop_after=lambda label: label == "rename:journal.commit.seal"
                )
                with self.assertRaises(harness.SimulatedCrash):
                    CrossJournal(directory, storage=crashing).create(ENABLE_ONE)
                journal = CrossJournal(directory, storage=harness.TracingStorage(directory))
                self.assertEqual((), journal.read_committed().records)
                view = journal.create(enable_id)
                self.assertEqual(
                    [("enable_started", enable_id)],
                    [(row.get("phase"), row.get("enable_id")) for row in view.records],
                )
                self.assertEqual(view, journal.create(enable_id))
                with self.assertRaises(ProtocolRefusal) as caught:
                    journal.create(ENABLE_OTHER)
                self.assertEqual("cross_journal_exists", caught.exception.code)


# The close-out records' priced maxima (R5.2), read independently of the module.
PRICES = {"cross_optin": 544, "cross_ceremony_closed": 512, "cross_pair_revoked": 768,
          "cross_teardown_receipt": 49152}
TEST_BUDGET = Budget(72 * 1024, 40)
# The failure bound the reserve promises to absorb: interrupted close-out attempts.
CLOSEOUT_INTERRUPTIONS = 2


def _is_control(record) -> bool:
    kind = str(record.get("kind"))
    return kind.startswith("cross_") and kind != "cross_pair_seen"


def finishing(records) -> Tuple[int, int, List[Tuple[str, Dict[str, object]]]]:
    """What retiring every pair and finishing disable still needs, and the plan.

    The test's own projection of the committed records: each step of the plan
    is priced at its record's maximum, never at what this run happens to write.
    """

    phases = {row.get("phase") for row in records if row["kind"] == "cross_optin"}
    if "disable_complete" in phases:
        return 0, 0, []
    opened = [str(row["ceremony_id"]) for row in records if row["kind"] == "cross_ceremony_opened"]
    ended = {str(row["ceremony_id"]) for row in records
             if row["kind"] in {"cross_ceremony_closed", "cross_pair_active"}}
    active = [str(row["pair_id"]) for row in records if row["kind"] == "cross_pair_active"]
    revoked = {str(row["pair_id"]) for row in records if row["kind"] == "cross_pair_revoked"}
    plan: List[Tuple[str, Dict[str, object]]] = []
    if "disable_started" not in phases:
        plan.append(("disable_started", _optin("disable_started", "local")))
    plan += [(f"close {ceremony}", dict(_ceremony_closed(ceremony), cause="disabled"))
             for ceremony in sorted(set(opened) - ended)]
    plan += [(f"revoke {pair}", _revoked(pair, "disable")) for pair in sorted(set(active) - revoked)]
    if not any(row["kind"] == "cross_teardown_receipt" and row.get("outcome") == "complete"
               for row in records):
        plan.append(("receipt", _receipt("complete", 0)))
    plan.append(("disable_complete", _optin("disable_complete", None)))
    return sum(PRICES[str(record["kind"])] for _, record in plan), len(plan), plan


def _record(kind: str, identifier: str, **body: object) -> Dict[str, object]:
    return dict({"schema_version": 0, "id": identifier, "kind": kind}, **body)


def _optin(phase: str, cause: Optional[str], run: int = 0) -> Dict[str, object]:
    suffix = f"-{run}" if run else ""
    return _record("cross_optin", f"cross-optin-{ENABLE_ONE}-{phase.replace('_', '-')}{suffix}",
                   phase=phase, enable_id=ENABLE_ONE, cause=cause)


def _ceremony_closed(ceremony: str) -> Dict[str, object]:
    return _record("cross_ceremony_closed", f"cross-ceremony-closed-{ceremony}",
                   ceremony_id=ceremony, cause="abort")


def _revoked(pair: str, cause: str) -> Dict[str, object]:
    return _record("cross_pair_revoked", f"cross-pair-revoked-{pair}",
                   pair_id=pair, cause=cause, idempotency_key=None)


def _receipt(outcome: str, run: int) -> Dict[str, object]:
    return _record("cross_teardown_receipt", f"cross-teardown-{ENABLE_ONE}-{outcome}-{run}",
                   outcome=outcome, items=[{"item": "unit", "class": "confirmed_absent"}])


def _accepted_delivery():
    return _record("remote_inbound", "remote-inbound-p1", pair_id="p1",
                   envelope_id="envelope-a", recipient="node", local_message_id="remote-inbound-p1",
                   envelope_digest="b" * 64, direction="S-to-T", wire_seq=1,
                   receipt_id="rcpt-" + "c" * 32, receipt_digest="d" * 64, receipt_body={"result": {"accepted": {
                       "record_id": "remote-inbound-p1"}}})


def _delivery_completion(kind):
    inbound = _accepted_delivery()
    record = _record(kind, kind.replace("_", "-"), pair_id="p1", inbound_id=inbound["id"],
                     envelope_id="envelope-a")
    if kind == "remote_receipt_confirmed":
        record.update({key: inbound[key] for key in ("direction", "wire_seq", "receipt_id", "receipt_digest")})
    if kind in {"remote_presented", "remote_local_ack"}:
        record.update(schema_version=1, worker_session_id="worker-a", recipient=inbound["recipient"],
                      local_message_id=inbound["local_message_id"], envelope_digest=inbound["envelope_digest"])
    if kind == "remote_presented":
        record["token_sha256"] = hashlib.sha256(_DELIVERY_TOKEN).hexdigest()
    return record


def _append_completion(journal: CrossJournal) -> object:
    return _write(journal, "L", _delivery_completion("remote_presented"))


class CrossReserveProperty(unittest.TestCase):
    def test_ir20_attended_start_reserves_the_due_completion_in_both_dimensions(self):
        from floati.cross_attended import approved_completion, approved_start
        from floati.cross_reserve import admit, apply, closeout_cost, completion_hold, project

        before = project([])
        shutdown = closeout_cost(before)
        start = approved_start(ENABLE_ONE, 1, "a" * 32,
                               {"root": "/fixture/root", "listen": [], "store": "keychain", "unit": "none"})
        size = len(encode_frame(dict(start, seq=1, prev="b" * 64)))
        for budget in (Budget(8 * 1024 * 1024, shutdown.slots + 1), Budget(shutdown.bytes + max(size, 512), 100)):
            with self.assertRaises(ProtocolRefusal) as caught:
                admit(before, start, size, budget)
            self.assertEqual("cross_review_capacity", caught.exception.code)
        self.assertEqual("ordinary", admit(before, start, size, Budget(8 * 1024 * 1024, shutdown.slots + 2)))
        owned = apply(before, start, size)
        self.assertEqual(1, completion_hold(owned)[0].slots)
        self.assertEqual(0, completion_hold(apply(owned, approved_completion(start)))[0].slots)

    """X18″ and X41: no allowed transition spends the close-out credit.

    Generated sequences churn ceremonies, pairs, addresses, data and
    compaction, with interrupted appends throughout, then start disable and
    repeat incomplete teardowns, then finish with up to two interrupted
    close-out attempts. After every transition the room left in the control
    budget must cover the priced close-out plan, and disable must complete.
    """

    SEEDS = range(8)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def _journal(self, directory: Path, **options) -> CrossJournal:
        return CrossJournal(directory, storage=harness.TracingStorage(directory, **options),
                            budget=TEST_BUDGET)

    def _attempt(self, directory: Path, capability: str, record, crash: bool) -> str:
        if crash:
            # Settle any earlier tail first, so this append is the one that dies.
            self._journal(directory).maintain("M")
        journal = self._journal(
            directory,
            stop_after=(lambda label: label.startswith("sync:journal-")) if crash else None,
        )
        try:
            return _write(journal, capability, record).status
        except harness.SimulatedCrash:
            return "crashed"
        except ProtocolRefusal as exc:
            return "refused " + exc.code

    def _room(self, directory: Path) -> Tuple[List[Dict[str, object]], int, int]:
        self._journal(directory).maintain("M")
        records = list(self._journal(directory).read_committed().records)
        control = [row for row in records if _is_control(row)]
        used_bytes = sum(len(encode_frame(row)) for row in control)
        return records, TEST_BUDGET.bytes - used_bytes, TEST_BUDGET.slots - len(control)

    def _check(self, directory: Path, where: str, violations: List[str]) -> List[Dict[str, object]]:
        records, room_bytes, room_slots = self._room(directory)
        need_bytes, need_slots, _ = finishing(records)
        if room_bytes < need_bytes or room_slots < need_slots:
            violations.append(
                f"{where}: room {room_bytes} B/{room_slots} slots < close-out {need_bytes} B/{need_slots} slots"
            )
        return records

    def _churn(self, seed: int, violations: List[str], seen: Dict[str, int]) -> None:
        rng = random.Random(seed)
        directory = self.base / f"seed-{seed}" / "cross"
        directory.parent.mkdir()
        self._journal(directory).create(ENABLE_ONE)
        self._attempt(directory, "A", _optin("enable_complete", None), False)
        records = self._check(directory, f"seed {seed} enabled", violations)
        ceremonies = pairs = data = 0
        for step in range(70):
            open_ceremonies = sorted(
                {str(r["ceremony_id"]) for r in records if r["kind"] == "cross_ceremony_opened"}
                - {str(r["ceremony_id"]) for r in records
                   if r["kind"] in {"cross_ceremony_closed", "cross_pair_active"}})
            active = sorted(
                {str(r["pair_id"]) for r in records if r["kind"] == "cross_pair_active"}
                - {str(r["pair_id"]) for r in records if r["kind"] == "cross_pair_revoked"})
            choice = rng.choice(("open", "open", "activate", "abort", "address", "unpair",
                                 "data", "data", "compact"))
            crash = rng.random() < 0.3
            if choice == "open":
                ceremonies += 1
                capability, record = "A", _record(
                    "cross_ceremony_opened", f"cross-ceremony-{ceremonies}", ceremony_id=f"c{ceremonies}")
            elif choice == "activate" and open_ceremonies:
                pairs += 1
                capability, record = "A", _record(
                    "cross_pair_active", f"cross-pair-active-p{pairs}", pair_id=f"p{pairs}",
                    ceremony_id=open_ceremonies[0], ceremony_token="t" * 32,
                    transcript_receipt_digest="0" * 64)
            elif choice == "abort" and open_ceremonies:
                capability, record = "A", _ceremony_closed(open_ceremonies[-1])
            elif choice == "address" and active:
                capability, record = "L", _record(
                    "cross_peer_address", f"cross-peer-address-{step}",
                    pair_id=active[0], addresses=["192.0.2.1:7443"])
            elif choice == "unpair" and active:
                capability, record = "L", _revoked(active[0], "local")
            elif choice == "compact":
                try:
                    self._journal(directory).compact("L", "explicit")
                    outcome = "compacted"
                except ProtocolRefusal as exc:
                    outcome = "refused " + exc.code
                records = self._check(directory, f"seed {seed} step {step} compact -> {outcome}", violations)
                continue
            else:
                data += 1
                capability, record = "I", _pair_record("remote_inbound", active[0], 1000 * seed + data) if active else (
                    _record("remote_endpoint_registered", f"endpoint-{data}")
                )
            outcome = self._attempt(directory, capability, record, crash)
            seen[outcome] = seen.get(outcome, 0) + 1
            records = self._check(
                directory, f"seed {seed} step {step} {record['kind']} -> {outcome}", violations)

        # X41: on even seeds, churn until ordinary control appends are refused.
        while seed % 2 == 0:
            ceremonies += 1
            outcome = self._attempt(directory, "A", _record(
                "cross_ceremony_opened", f"cross-ceremony-{ceremonies}", ceremony_id=f"c{ceremonies}"), False)
            records = self._check(directory, f"seed {seed} fill -> {outcome}", violations)
            if outcome != "committed":
                seen["fill " + outcome] = seen.get("fill " + outcome, 0) + 1
                break

        # Disable starts; incomplete teardowns repeat.
        for run in range(1, rng.randint(1, 4) + 1):
            started = [_optin("disable_started", "local")] if run == 1 else []
            for record in started + [_receipt("incomplete", run), _optin("disable_incomplete", None, run)]:
                outcome = self._attempt(directory, "L", record, False)
                seen["incomplete " + outcome] = seen.get("incomplete " + outcome, 0) + 1
                records = self._check(
                    directory, f"seed {seed} incomplete run {run} {record['kind']} -> {outcome}", violations)

        # Finish, absorbing up to the promised number of interrupted attempts.
        interrupted = 0
        for _ in range(200):
            _, _, plan = finishing(records)
            if not plan:
                break
            name, record = plan[0]
            crash = interrupted < CLOSEOUT_INTERRUPTIONS and rng.random() < 0.4
            outcome = self._attempt(directory, "L", record, crash)
            if outcome == "crashed":
                interrupted += 1
                seen["finish crashed"] = seen.get("finish crashed", 0) + 1
            elif outcome not in {"committed", "existing"}:
                violations.append(f"seed {seed} close-out {name} -> {outcome}")
                break
            records = self._check(directory, f"seed {seed} close-out {name} -> {outcome}", violations)
        if finishing(records)[2]:
            violations.append(f"seed {seed}: disable did not complete")

    def test_priced_r7_shutdown_frame_matches_the_maximum_width_vector(self) -> None:
        import json

        from floati.cross_coordinates import enc, envelope_id, receipt_id
        from floati.cross_records import priced_frame
        from floati.cross_reserve import completion_id, delivery_key

        limit = 2**53 - 1
        enable, pair, ceremony, token = ("a" * 32, "b" * 32, "c" * 32, "d" * 32)
        scope = {"enable_id": enable, "enable_generation": limit - 101}
        rows = []

        def control(kind, identity, **fields):
            return _record(kind, identity, schema_version=1, **scope, **fields)

        def phase(name, cause):
            return control("cross_optin", f"cross-optin-{enable}-{name.replace('_', '-')}", phase=name, cause=cause)

        start = phase("disable_started", "local")
        reconstructed = phase("disable_started", "anchor_absent_reconstructed")
        rows.extend([
            (start, 521, "1b3b149ef704fe86ff6cd8b1c6656f10b1b3cdba3816a45d9a6b2e0c1fddd354"),
            (reconstructed, 543, "0e39f224c13244fac2d734bcb86cf3671a39bf7ea0518825f9360e66199bc906"),
            (phase("disable_complete", None), 520, "d7f2711f73901d43edf0fbc34ab473576496bf9d4269c3e6ee0c4472b6886a9d"),
        ])
        for cause, size, sha in (
            ("abort", 402, "6e3901b84f8934ff061448880beebfb30ec3be9b93f652196b317353f13005b4"),
            ("crash", 402, "0f2afaa8dc5fff97a3bb664dc748abb407e1b8d956e101d2437033033e746833"),
            ("disabled", 405, "f94f89ba93104d03a27ee8e4cf88e7cb4865f4eea534b66fb1db8e86c6944ed2"),
        ):
            body = _record("cross_ceremony_closed", f"cross-ceremony-closed-{ceremony}",
                           schema_version=2, ceremony_id=ceremony, ceremony_token=token, cause=cause, **scope)
            rows.append((body, size, sha))
        for cause, size, sha in (
            ("local", 687, "c0880bc7cee93c78a0f57093b8a4ddafb507955ba4b35b95690b14f9579fcc88"),
            ("disable", 563, "77faeb800706943d322a226180df086ff7b888397c6f7f22b36c7d136b1943fd"),
            ("key_absent_reconstructed", 580, "b111fc1ec9c33d41300718b538046630a377b31ffec24769fc7ca7caa32b278d"),
        ):
            rows.append((control("cross_pair_revoked", f"cross-pair-revoked-{pair}", pair_id=pair,
                                 cause=cause, idempotency_key="k" * 128 if cause == "local" else None), size, sha))
        full = control(
            "cross_teardown_receipt", f"cross-teardown-{enable}-complete", outcome="complete",
            items=[{"item": f"i{i:0127d}", "class": "confirmed_absent", "cause": "removed_and_verified"}
                   for i in range(160)],
            notices=[{"pair_id": f"{i:032x}", "outcome": "notified"} for i in range(64)],
            checks={key: True for key in ("unit_absent", "unit_not_loadable", "no_listener", "no_live_endpoint")},
            step_ms=[10**15] * 7, total_ms=7 * 10**15,
        )
        rows.extend([
            (full, 36620, "4e8d15f433c5767baa71a268dc971fde8095d1fb31aa3859c91b9d93ccbe5273"),
            (dict(full, items=[], notices=[]), 590, "d5506c96d747ca2d7739af29a314c0004ba29953f5b6236e5691906428de58d6"),
        ])
        direction, envelope = "T-to-S", "1" * 64
        wire = envelope_id(pair, direction, limit)
        inbound = f"remote-inbound-{enable}-{wire}"
        delivery = delivery_key(inbound)
        tail = b'{"pad":"' + b"x" * 65525 + b'"}\n'
        tail_hash = hashlib.sha256(tail).hexdigest()
        offset = 67108864
        repair_id = hashlib.sha256(f"cross-{enable}-g1:{offset}:{tail_hash}".encode("ascii")).hexdigest()[:32]
        repair = _record("cross_journal_tail_repaired", f"cross-repair-{repair_id}", offset=offset,
                         tail_len=len(tail), tail_sha256=tail_hash, complete_frames=1)
        rows.extend([
            (repair, 334, "5ec50962afd43d3d7ac23441136bb1837cd7502fb1d46f926fcc8703c3f68e73"),
            (dict(repair, completion_delivery=delivery), 423, "45f507b5bb43411a2edba3a3459ef038209480ccbbb1a2e2a7328f271be49546"),
        ])
        local = "0190f0e1a2b37c4d8e9f0a1b2c3d4e5f"
        local_id, recipient, worker = f"rmsg-{local}", "n" * 64, "w" * 256
        token_hash = hashlib.sha256(bytes(range(32))).hexdigest()
        release_id = completion_id(token_hash, "remote_presented")

        def completion(kind, **fields):
            return _record(kind, release_id if kind == "remote_presented" else completion_id(delivery, kind),
                           schema_version=2, pair_id=pair, direction=direction, delivery_seq=limit,
                           inbound_id=inbound, envelope_digest=envelope, **scope, **fields)

        release = completion("remote_presented", local_message_id=local_id, recipient=recipient,
                             worker_session_id=worker, presented_at="2026-11-25T00:00:00.000Z", token_sha256=token_hash)
        rows.extend([
            (release, 1149, "2f9f0114396afef697bb11b5301aaef6c7c2b186089f63c8537895ebfda22eba"),
            (completion("remote_local_ack", local_message_id=local_id, recipient=recipient,
                        worker_session_id=worker, release_id=release_id),
             1139, "690bcb0adeb1461fc246f0060fc578da654fadb3c27bd72acf0a554d2200f53d"),
            (completion("remote_hold_rejected", local_message_id=local_id, recipient=recipient,
                        hold_id=f"inbound-hold-{local}", hold_digest="2" * 64,
                        decision_id=f"inbound-hold-{local[:-1]}0", decision_sha256="3" * 64),
             1040, "f9a42a2ed13d59a266ec8320e2ed29d62335eb93f7e5f5d2eba7de4af6ada384"),
        ])
        for result, sha in [
            ({"accepted": {"record_id": local_id}}, "01f9c6d531171f030345b84b165372be7a489bd8fdf411bfd9f3b794f6f3e054"),
            ({"refused": {"code": "cross_node_not_allowed"}}, "b71afd2b9ed6b6f7caea15f27962edfead4187f93188e44905b78584331c0e11"),
        ]:
            receipt_identity = receipt_id(pair, direction, limit, envelope)
            receipt = dict(v=5, pair_id=pair, direction=direction, seq=limit, envelope_id=wire,
                           envelope_digest=envelope, receipt_id=receipt_identity, result=result)
            data = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("ascii")
            receipt_hash = hashlib.sha256(enc(b"floati-remote-receipt-v5") + enc(data)).hexdigest()
            rows.append((completion("remote_receipt_confirmed", receipt_id=receipt_identity, receipt_digest=receipt_hash),
                         761, sha))
        rows.extend([
            (dict(start, enable_generation=7), 494, "65811d2ca7d3db89e094a06f14fbbd0dd875c4dbb91d335e02c55bcfbef12c17"),
            (dict(reconstructed, enable_generation=7), 516, "6edf06233a6abaeae6cd4aafec7ba2ec00124bb29310d50d099ad992e7e48224"),
        ])
        for body, size, sha in rows:
            with self.subTest(kind=body["kind"], cause=body.get("cause"), size=size):
                position = 10000 if body.get("enable_generation") == 7 else 100000
                trust = ("e" * 64, 10000 if position == 10000 else limit - 100)
                if body["kind"] not in {"cross_optin", "cross_pair_revoked"}:
                    trust = None
                framed = priced_frame(body, position, "f" * 64, trust=trust)
                independent = json.dumps(json.loads(framed), sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
                self.assertEqual(independent, framed)
                self.assertEqual((size, sha), (len(framed), hashlib.sha256(framed).hexdigest()))
        for invalid in (dict(start, unrelated=True), dict(start, enable_generation=True),
                        dict(full, items=full["items"] + [full["items"][-1]]),
                        dict(release, worker_session_id="w" * 257)):
            with self.subTest(kind=invalid["kind"]), self.assertRaises(ProtocolRefusal):
                priced_frame(invalid, 100000, "f" * 64, trust=("e" * 64, limit - 100))

    def test_successor_counts_data_against_the_originating_pair(self) -> None:
        from floati.cross_reserve import ControlState, admit_compaction

        before = ControlState(control_bytes=4000, control_slots=10)
        after = ControlState(control_bytes=80, control_slots=1)
        after.pair_data = {"p1": Budget(3_670_017, 1)}
        with self.assertRaises(ProtocolRefusal) as caught:
            admit_compaction(before, after, Budget(65536, 32), Budget(56 * 1024 * 1024, 90000))
        self.assertEqual("cross_storage_cap", caught.exception.code)

    def test_successor_checks_data_caps_when_no_delivery_holds_exist(self) -> None:
        from floati.cross_reserve import ControlState, admit_compaction

        before = ControlState(control_bytes=4000, control_slots=10, data_bytes=999, data_slots=2)
        for after in (ControlState(control_bytes=80, control_slots=1, data_bytes=1001, data_slots=2),
                      ControlState(control_bytes=80, control_slots=1, data_bytes=999, data_slots=3)):
            with self.subTest(bytes=after.data_bytes, slots=after.data_slots):
                with self.assertRaises(ProtocolRefusal) as caught:
                    admit_compaction(before, after, Budget(65536, 32), Budget(1000, 2))
                self.assertEqual("cross_storage_cap", caught.exception.code)

    def test_data_prune_can_buy_a_control_checkpoint_with_data_gain(self) -> None:
        from floati.cross_reserve import ControlState, admit_compaction

        before = ControlState(control_bytes=4000, control_slots=10, data_bytes=970, data_slots=8)
        after = ControlState(control_bytes=4500, control_slots=11, data_bytes=900, data_slots=7)
        self.assertIsNone(admit_compaction(before, after, Budget(65536, 32), Budget(1000, 8), resource="data"))

    def test_no_allowed_transition_spends_the_closeout_credit(self) -> None:
        violations: List[str] = []
        seen: Dict[str, int] = {}
        for seed in self.SEEDS:
            self._churn(seed, violations, seen)
        # Not vacuous: churn reached the reserve, appends died along the way,
        # an incomplete teardown was refused once the plan needed the room, and
        # the finish absorbed interrupted attempts.
        for outcome in ("refused cross_reserve_protected", "fill refused cross_reserve_protected",
                        "crashed", "finish crashed",
                        "incomplete refused cross_reserve_protected"):
            if not seen.get(outcome):
                violations.append(f"no transition ended {outcome!r}: {sorted(seen.items())}")
        if violations:
            self.fail(f"{len(violations)} violations; first 20:\n" + "\n".join(violations[:20]))


class CrossCapacityTests(unittest.TestCase):
    """X33′ in part: disk full at either site, and the physical reserve."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve() / "cross"
        journal = CrossJournal(self.directory, storage=harness.TracingStorage(self.directory))
        journal.create(ENABLE_ONE)
        journal.provision_reserve_file(64)
        for capability, record in (
            ("A", _optin("enable_complete", None)),
            ("A", _record("cross_ceremony_opened", "cross-ceremony-1", ceremony_id="c1")),
            ("A", _record("cross_pair_active", "cross-pair-active-p1", pair_id="p1", ceremony_id="c1",
                          ceremony_token="t" * 32, transcript_receipt_digest="0" * 64)),
        ):
            _write(journal, capability, record)

    def _full_at(self, site: Callable[[str, str], bool]) -> CrossJournal:
        return CrossJournal(self.directory, storage=harness.TracingStorage(
            self.directory, fail_when=site, fail_errno=errno.ENOSPC))

    def test_disk_full_is_one_code_at_both_sites_and_publishes_nothing(self) -> None:
        sites = {
            "journal append": lambda operation, name: operation == "append" and name.startswith("journal-"),
            "checkpoint replace": lambda operation, name: operation == "create" and name == ".journal.commit.tmp",
        }
        for number, (label, site) in enumerate(sites.items(), start=900):
            with self.subTest(site=label):
                record = data_record(number)
                result = _write(self._full_at(site), "I", record)
                self.assertEqual(("failed", "disk_full"), (result.status, result.code))
                published = [row["id"] for row in CrossJournal(self.directory).read_committed().records]
                self.assertNotIn(record["id"], published)
                self.assertIsNotNone((self.directory / "closeout.ballast").stat())

    def test_a_closeout_step_releases_the_reserve_file_and_commits(self) -> None:
        site = lambda operation, name: operation == "append" and name.startswith("journal-")
        ordinary = _write(self._full_at(site), "L", _record("cross_peer_address", "cross-peer-address-1", pair_id="p1", addresses=["192.0.2.1:1"]))
        self.assertEqual(("failed", "disk_full"), (ordinary.status, ordinary.code))
        self.assertTrue((self.directory / "closeout.ballast").exists())
        closeout = _write(self._full_at(site), "L", _revoked("p1", "local"))
        self.assertEqual("committed", closeout.status)
        self.assertFalse((self.directory / "closeout.ballast").exists())
        published = [row["id"] for row in CrossJournal(self.directory).read_committed().records]
        self.assertIn(_revoked("p1", "local")["id"], published)

    def test_closeout_ballast_covers_dirty_tail_recovery(self) -> None:
        storage = harness.TracingStorage(self.directory)
        storage.append("journal-g1.jsonl", b'{"torn":')
        journal = self._full_at(lambda operation, name:
                                operation == "create" and name == ".journal.commit.tmp")
        self.assertEqual("committed", _write(journal, "L", _revoked("p1", "local")).status)
        fresh = CrossJournal(self.directory)
        rows = fresh.read_committed().records
        self.assertEqual(1, sum(row["kind"] == "cross_journal_tail_repaired" for row in rows))
        self.assertEqual(1, sum(row["kind"] == "cross_pair_revoked" for row in rows))
        self.assertFalse((self.directory / "closeout.ballast").exists())

    def test_pinned_blocks_and_substituted_ballast_leave_closeout_pending(self) -> None:
        class PinnedStorage(harness.TracingStorage):
            def before(self, operation, name):
                if operation == "create" and name == ".journal.commit.tmp":
                    raise OSError(errno.ENOSPC, "no writable blocks")
                super().before(operation, name)

        for mode in ("pinned", "substituted"):
            with self.subTest(mode=mode):
                directory = self.directory.parent / mode
                from_seed = harness.snapshot(self.directory)
                from_seed.pop("closeout.ballast")
                from_seed.pop("closeout.ballast.inventory")
                harness.materialize(from_seed, directory)
                CrossJournal(directory).provision_reserve_file(64)
                ballast = directory / "closeout.ballast"
                if mode == "substituted":
                    ballast.unlink()
                    ballast.write_bytes(b"foreign")
                harness.TracingStorage(directory).append("journal-g1.jsonl", b'{"torn":')
                journal = CrossJournal(directory, storage=PinnedStorage(directory))
                result = _closeout(journal)
                self.assertEqual(("interrupted", "disable_started"), (result["state"], result["step"]))
                if mode == "substituted":
                    self.assertEqual(b"foreign", ballast.read_bytes())
                    self.assertEqual("cross_closeout_unproved", result["code"])
                else:
                    self.assertFalse(ballast.exists())
                    self.assertEqual("disk_full", result["code"])
                fresh = CrossJournal(directory)
                self.assertTrue(fresh.read_committed().pending)
                self.assertEqual("complete", _closeout(fresh)["state"])

    def test_shutdown_releases_the_owned_ballast_before_dirty_tail_recovery(self) -> None:
        harness.TracingStorage(self.directory).append("journal-g1.jsonl", b'{"torn":')
        journal = self._full_at(lambda operation, name:
                                operation == "create" and name == ".journal.commit.tmp")
        self.assertEqual("complete", _closeout(journal)["state"])
        self.assertFalse((self.directory / "closeout.ballast").exists())
        self.assertFalse(CrossJournal(self.directory).read_committed().pending)

    def _control_line(self, directory, *, extra=0, bytes_limit=False):
        records = CrossJournal(directory).read_committed().records
        plan_bytes, plan_slots, _ = finishing(records)
        control_records = [row for row in records if _is_control(row)]
        if bytes_limit:
            used = sum(len(encode_frame(row)) for row in control_records)
            return Budget(used + plan_bytes + (CLOSEOUT_INTERRUPTIONS + 1 + extra) * 512,
                          TEST_BUDGET.slots)
        return Budget(TEST_BUDGET.bytes,
                      len(control_records) + plan_slots + CLOSEOUT_INTERRUPTIONS + 1 + extra)

    def test_delivery_completion_still_commits_at_each_capacity_line(self) -> None:
        base = harness.snapshot(self.directory)
        for limit in ("data", "control slots", "control bytes"):
            directory = self.directory.parent / limit
            harness.materialize(base, directory)
            storage = harness.TracingStorage(directory)
            options = (
                {"data_budget": Budget(56 * 1024 * 1024, 4)} if limit == "data"
                else {"budget": self._control_line(directory, bytes_limit=limit == "control bytes")}
            )
            journal = CrossJournal(directory, storage=storage, **options)
            accepted = _accepted_delivery()
            self.assertEqual("committed", _write(journal, "I", accepted).status)
            original = harness.snapshot(directory)
            self.assertEqual("existing", _write(journal, "I", accepted).status)
            self.assertEqual(original, harness.snapshot(directory))
            probe = _pair_record("remote_session_opened", "p1", 1)
            code = "cross_storage_cap" if limit == "data" else "cross_reserve_protected"
            for number in range(4):
                with self.subTest(limit=limit, fill=number):
                    try:
                        self.assertEqual("committed", _write(journal, "I", dict(probe, id=f"session-fill-{number}")
                        ).status)
                    except ProtocolRefusal as exc:
                        self.assertEqual(code, exc.code)
                        break
            else:
                self.fail(f"ordinary data never reached the {limit} admission line")
            for writer, kind in (
                ("I", "remote_receipt_confirmed"),
                ("L", "remote_presented"),
                ("L", "remote_local_ack"),
            ):
                with self.subTest(limit=limit, kind=kind):
                    record = _delivery_completion(kind)
                    result = _write(journal, writer, record)
                    self.assertEqual("committed", result.status)
                    self.assertIn(result.record_id, {row["id"] for row in journal.read_committed().records})
            if limit != "data":
                self.assertEqual("committed", _write(journal, "I", probe).status)

    def test_acceptance_refuses_when_either_completion_reservation_cannot_fit(self) -> None:
        for options, code in (
            ({"data_budget": Budget(56 * 1024 * 1024, 3)}, "cross_storage_cap"),
            ({"data_budget": Budget(1536, 90_000)}, "cross_storage_cap"),
            ({"budget": self._control_line(self.directory, extra=-1)}, "cross_reserve_protected"),
        ):
            with self.subTest(code=code, budget=options):
                journal = CrossJournal(self.directory, **options)
                before = journal.read_committed()
                with self.assertRaises(ProtocolRefusal) as caught:
                    _write(journal, "I", _accepted_delivery())
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(before, journal.read_committed())

    def test_refused_receipt_reserves_its_confirmation_at_admission(self) -> None:
        refused = dict(_accepted_delivery(), receipt_body={"result": {"refused": {"code": "target_absent"}}})
        for cap, code in (
            (Budget(56 * 1024 * 1024, 1), "cross_storage_cap"),
            (Budget(2048, 2), "cross_storage_cap"),
        ):
            with self.subTest(cap=cap):
                journal = CrossJournal(self.directory, data_budget=cap)
                before = journal.read_committed()
                with self.assertRaises(ProtocolRefusal) as caught:
                    _write(journal, "I", refused)
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(before, journal.read_committed())
        cap = Budget(56 * 1024 * 1024, 2)
        self.assertEqual("committed", _write(CrossJournal(self.directory, data_budget=cap), "I", refused).status)
        journal = CrossJournal(self.directory, data_budget=cap)
        confirmation = _delivery_completion("remote_receipt_confirmed")
        self.assertEqual("committed", _write(journal, "I", confirmation).status)
        self.assertEqual("existing", _write(journal, "I", confirmation).status)
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(journal, "L", _delivery_completion("remote_presented"))
        self.assertEqual("cross_completion_delivery_invalid", caught.exception.code)

    def test_unconfirmed_refusal_stops_retirement_until_reack(self) -> None:
        import importlib

        delivery = importlib.import_module("floati.cross_delivery")
        item = dict(_accepted_delivery(), delivery_seq=1,
                    receipt_body={"result": {"refused": {"code": "target_absent"}}})
        self.assertEqual(0, delivery.retirable_through(0, [item], []))
        ack = dict(_delivery_completion("remote_receipt_confirmed"), receipt_digest=item["receipt_digest"])
        self.assertEqual(0, delivery.retirable_through(0, [item], [dict(ack, receipt_digest="c" * 64)]))
        self.assertEqual(1, delivery.retirable_through(0, [item], [ack]))

    def test_refused_receipt_stays_above_the_published_frontier_until_confirmation(self) -> None:
        from floati.cross_coordinates import envelope_id, receipt_id
        from floati.cross_reserve import completion_id, delivery_key

        pair = "a" * 32
        direction, number, envelope = "S-to-T", 1, "1" * 64
        wire_id = envelope_id(pair, direction, number)
        scope = {"pair_id": pair, "direction": direction, "delivery_seq": number,
                 "enable_id": ENABLE_ONE, "enable_generation": 1, "envelope_digest": envelope}
        outcome = _record("remote_inbound", f"remote-inbound-{ENABLE_ONE}-{wire_id}",
                          envelope_id=wire_id, receipt_id=receipt_id(pair, direction, number, envelope),
                          receipt_digest="d" * 64,
                          receipt_body={"result": {"refused": {"code": "cross_node_not_allowed"}}}, **scope)
        journal = CrossJournal(self.directory, data_budget=Budget(65536, 2))
        _write(journal, "A", _opened("receipt-peer"))
        _write(journal, "A", _activation(pair, "receipt-peer"))
        self.assertEqual("committed", _write(journal, "I", outcome).status)
        prune = Capability("L", pair, verb="prune")
        self.assertEqual(0, journal.prune_receiver(prune, direction, 1)["frontier"])
        fresh = CrossJournal(self.directory, data_budget=Budget(65536, 2))
        confirmation = _record("remote_receipt_confirmed",
                               completion_id(delivery_key(outcome["id"]), "remote_receipt_confirmed"),
                               schema_version=2, inbound_id=outcome["id"], receipt_id=outcome["receipt_id"],
                               receipt_digest=outcome["receipt_digest"], **scope)
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(fresh, "I", dict(confirmation, receipt_digest="e" * 64))
        self.assertEqual("cross_completion_delivery_invalid", caught.exception.code)
        self.assertEqual("committed", _write(fresh, "I", confirmation).status)
        self.assertEqual(1, fresh.prune_receiver(prune, direction, 1)["frontier"])
        fresh = CrossJournal(self.directory)
        self.assertEqual(1, fresh.prune_receiver(prune, direction, 1)["frontier"])
        kinds = [row["kind"] for row in fresh.read_committed().records]
        self.assertNotIn("remote_inbound", kinds)
        self.assertIn("cross_prune_checkpoint", kinds)

    def test_byte_cap_allows_compaction_at_a_sequence_digit_boundary(self) -> None:
        source = self.directory.parent / "digit-source"
        journal = CrossJournal(source, storage=harness.TracingStorage(source))
        journal.create(ENABLE_ONE)
        self.assertEqual("committed", _write(journal, "A", _optin("enable_complete", None)).status)
        for number in range(7):
            self.assertEqual("committed", _write(journal, "I", _record("remote_endpoint_registered", f"endpoint-{number}")
            ).status)
        self.assertEqual("committed", _write(journal, "A", _opened("c1")).status)
        self.assertEqual("committed", _write(journal, "A", _activation("p1", "c1")).status)
        for number in range(4):
            ceremony = f"retired-{number}"
            self.assertEqual("committed", _write(journal, "A", _opened(ceremony)).status)
            self.assertEqual("committed", _write(journal, "A", _ceremony_closed(ceremony)).status)
        directory = self.directory.parent / "compaction"
        harness.materialize(harness.snapshot(source), directory)
        self.assertEqual("committed", _write(journal, "I", _accepted_delivery()).status)
        data_floor = 6144 + sum(len(encode_frame(row)) for row in journal.read_committed().records
                                if not _is_control(row))
        for limit in range(data_floor, data_floor + 17):
            capped = CrossJournal(directory, storage=harness.TracingStorage(directory),
                                  data_budget=Budget(limit, 90_000))
            try:
                self.assertEqual("committed", _write(capped, "I", _accepted_delivery()).status)
            except ProtocolRefusal as exc:
                self.assertEqual("cross_storage_cap", exc.code)
            else:
                break
        else:
            self.fail("the acceptance needed more than 16 extra bytes")
        self.assertEqual("compacted", capped.compact()["state"])
        self.assertEqual("committed", _append_completion(capped).status)

    def _interrupt_completion(self, journal, record):
        # Leave a tail with no complete frame. Attribution cannot use its body.
        append = journal.storage.append

        def torn_append(name, payload):
            append(name, payload[:8])
            journal.storage.sync(name)
            raise harness.SimulatedCrash()

        with patch.object(journal.storage, "append", torn_append):
            return _write(journal, "L", record)

    def test_interrupted_completion_retries_once_by_durable_identity(self) -> None:
        journal = CrossJournal(self.directory, storage=harness.TracingStorage(self.directory))
        self.assertEqual("committed", _write(journal, "I", _accepted_delivery()).status)
        record = _delivery_completion("remote_presented")
        with self.assertRaises(harness.SimulatedCrash):
            self._interrupt_completion(journal, record)
        journal = CrossJournal(self.directory, storage=harness.TracingStorage(self.directory))
        result = _write(journal, "L", record)
        self.assertEqual("committed", result.status)
        before = harness.snapshot(self.directory)
        retry = _write(journal, "L", dict(record, id="retry-with-a-new-caller-id"))
        self.assertEqual(("existing", result.record_id), (retry.status, retry.record_id))
        self.assertEqual(before, harness.snapshot(self.directory))
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(journal, "L", dict(record, immutable_payload_digest="1" * 64))
        self.assertEqual("cross_record_id_conflict", caught.exception.code)
        self.assertEqual(before, harness.snapshot(self.directory))

    def test_ack_requires_the_latest_worker_output_token(self) -> None:
        journal = CrossJournal(self.directory)
        self.assertEqual("committed", _write(journal, "I", _accepted_delivery()).status)
        token = bytes(range(32))
        verifier = "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd"
        release = dict(_delivery_completion("remote_presented"), token_sha256=verifier,
                       worker_session_id="worker-a", recipient="node", local_message_id="remote-inbound-p1",
                       envelope_digest="b" * 64)
        first = _write(journal, "L", release)
        self.assertEqual("committed", first.status)
        ack = dict(release, kind="remote_local_ack", id="ack-a", release_id=first.record_id)
        del ack["token_sha256"]
        before = journal.read_committed()
        for label, offered in (("missing", None), ("verifier", bytes.fromhex(verifier))):
            with self.subTest(label=label):
                with self.assertRaises(ProtocolRefusal) as caught:
                    _write(journal, "L", ack, output_token=offered)
                self.assertEqual("cross_output_token_invalid", caught.exception.code)
                self.assertEqual(before, journal.read_committed())
        replacement = dict(release, id="replacement", worker_session_id="worker-b",
                           token_sha256="72cd6e8422c407fb6d098690f1130b7ded7ec2f7f5e1d30bd9d521f015363793",
                           supersedes_id=first.record_id)
        second = _write(journal, "L", replacement)
        self.assertEqual("committed", second.status)
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(journal, "L", ack, output_token=token)
        self.assertEqual("cross_output_token_invalid", caught.exception.code)
        ack.update(worker_session_id="worker-b", release_id=second.record_id)
        fresh = CrossJournal(self.directory)
        self.assertEqual("committed", _write(fresh, "L", ack, output_token=b"\x01" * 32).status)
        self.assertEqual("existing", _write(fresh, "L", ack, output_token=b"\x01" * 32).status)

    def test_v2_worker_release_needs_current_private_token_after_restart(self) -> None:
        from floati.cross_coordinates import envelope_id
        from floati.cross_reserve import completion_id, delivery_key

        pair = "b" * 32
        scope = {"enable_id": ENABLE_ONE, "enable_generation": 1, "pair_id": pair,
                 "direction": "S-to-T", "delivery_seq": 1, "envelope_digest": "b" * 64}
        local = "rmsg-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f"
        wire = envelope_id(pair, "S-to-T", 1)
        inbound = dict(_accepted_delivery(), id=f"remote-inbound-{ENABLE_ONE}-{wire}",
                       envelope_id=wire, local_message_id=local,
                       receipt_body={"result": {"accepted": {"record_id": local}}}, **scope)
        journal = CrossJournal(self.directory)
        _write(journal, "A", _opened("worker-peer"))
        _write(journal, "A", _activation(pair, "worker-peer"))
        self.assertEqual("committed", _write(journal, "I", inbound).status)
        claims = dict(scope, schema_version=2, inbound_id=inbound["id"], local_message_id=local,
                      recipient=inbound["recipient"], worker_session_id="worker-a")
        clear = bytes(range(32))
        verifier = hashlib.sha256(clear).hexdigest()
        release = _record("remote_presented", completion_id(verifier, "remote_presented"), **claims,
                          presented_at="2026-11-25T00:00:00.000Z", token_sha256=verifier)
        self.assertEqual("committed", _write(journal, "L", release).status)
        fresh = CrossJournal(self.directory)
        ack = _record("remote_local_ack", completion_id(delivery_key(inbound["id"]), "remote_local_ack"),
                      **claims, release_id=release["id"])
        for proof in (None, bytes.fromhex(verifier)):
            with self.subTest(has_token=proof is not None), self.assertRaises(ProtocolRefusal):
                _write(fresh, "L", ack, output_token=proof)
        self.assertEqual("committed", _write(fresh, "L", ack, output_token=clear).status)
        self.assertEqual("existing", _write(fresh, "L", ack, output_token=clear).status)
        with self.assertRaises(ProtocolRefusal):
            _write(fresh, "L", release)

    def test_closeout_replenishes_before_a_third_interruption(self) -> None:
        journal = CrossJournal(self.directory)
        _write(journal, "A", _opened("retired"))
        _write(journal, "A", _ceremony_closed("retired"))
        record = _revoked("p1", "local")
        budget = self._control_line(self.directory, extra=-1)
        for attempt in range(3):
            journal = CrossJournal(
                self.directory, budget=budget,
                storage=harness.TracingStorage(
                    self.directory, stop_after=lambda label: label.startswith("append:journal-")),
            )
            with self.assertRaises(harness.SimulatedCrash):
                _write(journal, "L", record)
            journal = CrossJournal(self.directory, budget=budget)
            journal.maintain()
            observed = journal.read_committed()
            self.assertNotIn(record["id"], [row["id"] for row in observed.records])
            self.assertEqual(1 if attempt < 2 else 2, observed.generation)
        self.assertEqual("committed", _write(journal, "L", record).status)
        self.assertEqual("complete", _closeout(journal)["state"])

    def test_authoritative_rejection_releases_only_local_credit(self) -> None:
        rejection = _record("remote_hold_rejected", "rejection-accounting", pair_id="p1",
                            inbound_id=_accepted_delivery()["id"], local_message_id="remote-inbound-p1",
                            recipient="node", hold_id="hold-a", hold_digest="c" * 64,
                            decision_id="decision-a")
        held = dict(_accepted_delivery(), hold_id="hold-a", hold_digest="c" * 64)
        cap = Budget(56 * 1024 * 1024, 4)
        journal = CrossJournal(self.directory, data_budget=cap)
        self.assertEqual("committed", _write(journal, "I", held).status)
        before = journal.read_committed()
        with self.assertRaises(ProtocolRefusal) as caught:
            journal.append(Capability("M", "p1", verb="hold_accounting"), rejection)
        self.assertEqual("cross_hold_evidence_invalid", caught.exception.code)
        self.assertEqual(before, journal.read_committed())
        fresh = CrossJournal(self.directory, data_budget=cap, hold_rejection=lambda candidate: candidate == rejection)
        capability = Capability("M", "p1", verb="hold_accounting")
        self.assertEqual("committed", fresh.append(capability, rejection).status)
        self.assertEqual("existing", fresh.append(capability, rejection).status)
        self.assertEqual("committed", _write(fresh, "I", _delivery_completion("remote_receipt_confirmed")).status)
        with self.assertRaises(ProtocolRefusal) as caught:
            _append_completion(fresh)
        self.assertEqual("cross_completion_delivery_invalid", caught.exception.code)


    def test_historical_rejection_settles_only_its_local_obligation(self) -> None:
        from contextlib import nullcontext

        from floati.cross_coordinates import envelope_id
        from floati.cross_reserve import completion_hold, completion_id, delivery_key, project

        pair = "a" * 32
        scope = {"enable_id": ENABLE_ONE, "enable_generation": 1, "pair_id": pair,
                 "direction": "S-to-T", "delivery_seq": 1, "envelope_digest": "b" * 64}
        local = "0190f0e1a2b37c4d8e9f0a1b2c3d4e5f"
        wire = envelope_id(pair, scope["direction"], scope["delivery_seq"])
        held = dict(_accepted_delivery(), id=f"remote-inbound-{ENABLE_ONE}-{wire}",
                    envelope_id=wire, local_message_id=f"rmsg-{local}",
                    receipt_body={"result": {"accepted": {"record_id": f"rmsg-{local}"}}},
                    hold_id=f"inbound-hold-{local}", hold_digest="c" * 64, **scope)
        decision = {"id": f"inbound-hold-{local[:-1]}0", "phase": "rejected", "node_id": held["recipient"],
                    "message_id": held["local_message_id"], "hold_id": held["hold_id"],
                    "envelope_digest": held["hold_digest"]}
        rejection = _record("remote_hold_rejected", completion_id(delivery_key(held["id"]), "remote_hold_rejected"),
                            schema_version=2, inbound_id=held["id"], local_message_id=held["local_message_id"],
                            recipient=held["recipient"], hold_id=held["hold_id"], hold_digest=held["hold_digest"],
                            decision_id=decision["id"], decision_sha256=hashlib.sha256(encode_frame(decision)).hexdigest(),
                            **scope)
        journal = CrossJournal(self.directory)
        _write(journal, "A", _opened("held-peer"))
        _write(journal, "A", _activation(pair, "held-peer"))
        self.assertEqual("committed", _write(journal, "I", held).status)
        self.assertEqual("complete", _closeout(journal)["state"])
        resumed = CrossJournal(self.directory, hold_source=lambda record: nullcontext(decision))
        resumed.create(ENABLE_TWO, enable_generation=2)
        cap = Capability("M", pair, verb="hold_accounting")
        self.assertEqual(Budget(6144, 3), completion_hold(project(resumed.read_committed().records))[1])
        with self.assertRaises(ProtocolRefusal) as caught:
            resumed.append(cap, dict(rejection, decision_sha256="d" * 64))
        self.assertEqual("cross_hold_evidence_invalid", caught.exception.code)
        self.assertEqual("committed", resumed.append(cap, rejection).status)
        self.assertEqual("existing", resumed.append(cap, rejection).status)
        state = project(resumed.read_committed().records)
        self.assertEqual(Budget(2048, 1), completion_hold(state)[1])
        self.assertFalse(state.active_pairs)

    def test_replenishment_never_admits_between_independent_replacements(self) -> None:
        from floati.cross_anchor import Anchor, Narrowing, NarrowingHandoff

        class Store:
            value = None

            def __init__(self):
                self.replacements = []
                self.crash_on = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                self.replacements.append(type(successor))
                if type(successor) is self.crash_on:
                    self.crash_on = None
                    raise harness.SimulatedCrash()
                return True

        directory = self.directory.parent / "fenced"
        store = Store()
        journal = CrossJournal(directory, anchor_store=store)
        journal.create(ENABLE_ONE)
        _write(journal, "A", _optin("enable_complete", None))
        _write(journal, "A", _opened("live"))
        _write(journal, "A", _activation("p1", "live"))
        for ceremony in ("obsolete-1", "obsolete-2"):
            _write(journal, "A", _opened(ceremony))
            _write(journal, "A", _ceremony_closed(ceremony))
        budget = self._control_line(directory, extra=-1)
        store.replacements.clear()
        for cut in range(2):
            storage = harness.TracingStorage(directory)
            writer = CrossJournal(directory, storage=storage, budget=budget, anchor_store=store)
            append = storage.append

            def interrupted(name, payload):
                if b'"kind":"cross_pair_revoked"' in payload:
                    append(name, payload[:8])
                    storage.sync(name)
                    raise harness.SimulatedCrash()
                return append(name, payload)

            with patch.object(storage, "append", interrupted), self.assertRaises(harness.SimulatedCrash):
                if cut == 0:
                    _write(writer, "L", _revoked("p1", "local"))
                else:
                    writer.maintain()
            self.assertTrue(CrossJournal(directory, anchor_store=store).read_committed().pending)
        final = CrossJournal(directory, budget=budget, anchor_store=store)
        store.crash_on = NarrowingHandoff
        with self.assertRaises(harness.SimulatedCrash):
            final.maintain()
        self.assertTrue(final.read_committed().pending)
        restored = directory.with_name("fenced-restored")
        harness.materialize(harness.snapshot(directory), restored)
        final = CrossJournal(restored, budget=budget, anchor_store=store)
        store.crash_on = Narrowing
        with self.assertRaises(harness.SimulatedCrash):
            final.maintain()
        self.assertTrue(final.read_committed().pending)
        final.maintain()
        final.maintain()
        self.assertEqual([Narrowing, NarrowingHandoff, Narrowing, Anchor], store.replacements)
        self.assertEqual(1, sum(row["kind"] == "cross_pair_revoked" for row in final.read_committed().records))

    def test_r10_nested_v4_handoff_cancel_and_transfer_outcomes_before_two_restores(self) -> None:
        from floati.cross_anchor import Anchor, Narrowing, NarrowingHandoff, OutstandingWrite

        class Store:
            value = None

            def __init__(self):
                self.outstanding = None
                self.changes = []
                self.cut = None
                self.outcome = None
                self.pre_controller = None
                self.directory = None

            def read(self):
                return OutstandingWrite() if self.outstanding is not None else self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.changes.append((expected, successor))
                step = None
                if isinstance(successor, NarrowingHandoff):
                    self.pre_controller = harness.snapshot(self.directory)
                    step = "issue"
                elif isinstance(expected, NarrowingHandoff):
                    step = "cancel" if successor == expected.narrowing else "rebase"
                if step is not None and step == self.cut:
                    self.cut = None
                    if self.outcome == "applied":
                        self.value = successor
                    elif self.outcome.startswith("outstanding"):
                        self.outstanding = successor
                    return False
                self.value = successor
                return True

        seed = self.directory.parent / "v4-nested-seed"
        initial, quota = Store(), Store()
        utc = lambda: "2026-11-25"
        journal = CrossJournal(seed, anchor_store=initial, utc_day=utc)
        journal.create(ENABLE_ONE)
        _write(journal, "A", _optin("enable_complete", None))
        _write(journal, "A", _opened("live"))
        _write(journal, "A", _activation("p1", "live"))
        rows = journal.read_committed(require_settled=True).records
        history = tuple((row["id"], row["ceremony_token"], row["utc_day"])
                        for row in rows if row["kind"] == "cross_ceremony_opened")
        journal = CrossJournal(seed, anchor_store=initial, quota_store=quota, legacy_exclusion=nullcontext,
                               authenticate_quota_history=lambda source, proof: history, utc_day=utc)
        journal.seal_authenticated_quota_history(Capability("A", verb="legacy_rollover"), "fixture")
        for ceremony in ("obsolete-1", "obsolete-2"):
            _write(journal, "A", _opened(ceremony))
            _write(journal, "A", _ceremony_closed(ceremony))
        prepared = harness.snapshot(seed)
        stable = initial.value

        for operation in ("issue", "cancel", "rebase"):
            for outcome in ("applied", "unapplied", "outstanding-applied", "outstanding-unapplied"):
                with self.subTest(operation=operation, outcome=outcome):
                    directory = seed.with_name(f"v4-nested-{operation}-{outcome}")
                    harness.materialize(prepared, directory)
                    store = Store()
                    store.value, store.directory = stable, directory
                    owner = object()
                    options = dict(anchor_store=store, utc_day=utc,
                                   authenticate_open=lambda proof, handle: handle is owner)
                    journal = CrossJournal(directory, **options)
                    live = journal.open_authenticated(_record(
                        "remote_session_opened", "nested-old-stream", schema_version=1, pair_id="p1",
                        direction="S-to-T", stream_id="b" * 32, enable_id=ENABLE_ONE, enable_generation=1,
                        local_side="listener", peer_der_sha256="c" * 64, gate_counter=1, owner_id="nested-reader",
                    ), owner)
                    options["budget"] = self._control_line(directory, extra=-1)
                    for n in range(2):
                        storage = harness.TracingStorage(directory)
                        writer = CrossJournal(directory, storage=storage, **options)
                        append = storage.append

                        def torn(name, payload):
                            if b'"kind":"cross_pair_revoked"' in payload:
                                append(name, payload[:8])
                                storage.sync(name)
                                raise harness.SimulatedCrash()
                            return append(name, payload)

                        with patch.object(storage, "append", torn), self.assertRaises(harness.SimulatedCrash):
                            if n == 0:
                                _write(writer, "L", _revoked("p1", "local"))
                            else:
                                writer.maintain()

                    original = store.value
                    self.assertIsInstance(original, Narrowing)
                    store.cut, store.outcome = operation, outcome
                    storage = harness.TracingStorage(
                        directory, fail_when=lambda verb, name: operation == "cancel" and verb == "rename"
                        and name == "journal.commit" and isinstance(store.value, NarrowingHandoff))
                    with self.assertRaises((OSError, ProtocolRefusal)):
                        CrossJournal(directory, storage=storage, **options).maintain()
                    handoff = next(new for _, new in store.changes if isinstance(new, NarrowingHandoff))
                    self.assertEqual(original, handoff.narrowing)
                    self.assertIsNotNone(handoff.handoff.quota_sha256)
                    at_cut = harness.snapshot(directory)

                    def fence(reader):
                        self.assertTrue(reader.read_committed().pending)
                        with self.assertRaises(ProtocolRefusal):
                            reader.open_authenticated(dict(
                                id="new-endpoint", kind="remote_session_opened", schema_version=1, pair_id="p1",
                                direction="S-to-T", stream_id="d" * 32, enable_id=ENABLE_ONE, enable_generation=1,
                                local_side="listener", peer_der_sha256="c" * 64, gate_counter=2, owner_id="nested-reader",
                            ), owner)
                        with self.assertRaises(ProtocolRefusal):
                            journal.append(live.capability("envelope"), _accepted_delivery())

                    fence(CrossJournal(directory, **options))
                    if store.outstanding is not None:
                        with self.assertRaises(ProtocolRefusal):
                            CrossJournal(directory, **options).maintain()
                        if outcome == "outstanding-applied":
                            store.value = store.outstanding
                        store.outstanding = None
                    expected = store.value
                    earlier = store.pre_controller
                    for image in (earlier, at_cut):
                        harness.materialize(image, directory)
                        reader = CrossJournal(directory, **options)
                        fence(reader)
                        self.assertEqual(expected, store.value)
                        if image is earlier and isinstance(expected, Narrowing) and expected.original is not None:
                            with self.assertRaises(ProtocolRefusal):
                                reader.maintain()
                            self.assertEqual(expected, store.value)
                    fresh = CrossJournal(directory, **options)
                    fresh.maintain()
                    fresh.maintain()
                    self.assertIsInstance(store.value, Anchor)
                    settled = fresh.read_committed(require_settled=True)
                    self.assertEqual(1, sum(row["kind"] == "cross_pair_revoked" for row in settled.records))
                    checkpoint, _ = fresh._load()
                    self.assertEqual(("exact", "2026-11-25", 3),
                                     (checkpoint["quota"]["mode"], checkpoint["quota"]["day"], checkpoint["quota"]["count"]))
                    self.assertEqual(hashlib.sha256(store.pre_controller["journal.commit"]).hexdigest(),
                                     checkpoint["quota"]["source"]["checkpoint_sha256"])
                    if operation == "cancel" and outcome.endswith("applied") and "unapplied" not in outcome:
                        self.assertEqual(original, expected)
                    for previous, replacement in store.changes:
                        if isinstance(previous, NarrowingHandoff) and replacement != original:
                            self.assertIsInstance(replacement, Narrowing)
                            self.assertEqual(original, replacement.original)
                            self.assertNotEqual(original.prior.opening_sha256, replacement.prior.opening_sha256)

    def test_pending_completion_names_the_operation_and_limiting_resource(self) -> None:
        budget = self._control_line(self.directory)
        journal = CrossJournal(self.directory, budget=budget)
        self.assertEqual("committed", _write(journal, "I", _accepted_delivery()).status)
        record = _delivery_completion("remote_presented")
        with self.assertRaises(harness.SimulatedCrash):
            self._interrupt_completion(journal, record)
        journal = CrossJournal(self.directory, budget=budget)
        journal.maintain()
        result = _write(journal, "L", record)
        self.assertEqual(("deferred", "remote_presented", "control_slots"),
                         (result.status, getattr(result, "operation", None), getattr(result, "limiting_resource", None)))

    def test_repeated_interruptions_defer_without_spending_disable_credit(self) -> None:
        for resolution in ("compact", "disable", "ordinary repair then compact"):
            with self.subTest(resolution=resolution):
                directory = self.directory.parent / resolution
                harness.materialize(harness.snapshot(self.directory), directory)
                journal = CrossJournal(directory, storage=harness.TracingStorage(directory))
                for number in range(4):
                    ceremony = f"retired-{number}"
                    self.assertEqual("committed", _write(journal, "A", _opened(ceremony)).status)
                    self.assertEqual("committed", _write(journal, "A", _ceremony_closed(ceremony)).status)
                interruptions = 2 if resolution == "ordinary repair then compact" else 1
                budget = self._control_line(directory, extra=interruptions - 1)
                journal = CrossJournal(directory, storage=harness.TracingStorage(directory), budget=budget)
                self.assertEqual("committed", _write(journal, "I", _accepted_delivery()).status)
                record = _delivery_completion("remote_presented")
                for _ in range(interruptions):
                    with self.assertRaises(harness.SimulatedCrash):
                        self._interrupt_completion(journal, record)
                    journal = CrossJournal(directory, storage=harness.TracingStorage(directory), budget=budget)
                    journal.maintain()
                before = harness.snapshot(directory)
                result = self._interrupt_completion(journal, record)
                self.assertEqual(("deferred", "cross_completion_deferred"), (result.status, result.code))
                self.assertEqual(_accepted_delivery()["id"], result.delivery_id)
                self.assertEqual(before, harness.snapshot(directory))
                committed = journal.read_committed().records
                self.assertFalse(any(row["kind"] == record["kind"] for row in committed))
                if resolution != "disable":
                    self.assertEqual("compacted", journal.compact()["state"])
                    self.assertEqual("committed", _write(journal, "L", record).status)
                else:
                    self.assertEqual("complete", _closeout(journal)["state"])
                    self.assertTrue(any(row.get("phase") == "disable_complete"
                                        for row in journal.read_committed().records))


def _activation(pair: str, ceremony: str) -> Dict[str, object]:
    return _record("cross_pair_active", f"cross-pair-active-{pair}", pair_id=pair, ceremony_id=ceremony,
                   ceremony_token="t" * 32, transcript_receipt_digest="0" * 64)


def _opened(ceremony: str) -> Dict[str, object]:
    return _record("cross_ceremony_opened", f"cross-ceremony-{ceremony}", ceremony_id=ceremony)


def _pair_record(kind: str, pair: str, number: int) -> Dict[str, object]:
    return _record(kind, f"{kind.replace('_', '-')}-{pair}-{number}", pair_id=pair, wire_seq=number)


def _seed_pairs(directory: Path, *, utc_day=None) -> None:
    """Enabled, pairs p1 and p2 active, ceremony c3 open."""

    directory.parent.mkdir(parents=True, exist_ok=True)
    journal = CrossJournal(directory, storage=harness.TracingStorage(directory), utc_day=utc_day)
    journal.create(ENABLE_ONE)
    for capability, record in (
        ("A", _optin("enable_complete", None)),
        ("A", _opened("c1")), ("A", _activation("p1", "c1")),
        ("A", _opened("c2")), ("A", _activation("p2", "c2")),
        ("A", _opened("c3")),
    ):
        if _write(journal, capability, record).status != "committed":
            raise AssertionError(f"seeding {record['id']} failed")


def _journal_bytes(directory: Path) -> Dict[str, bytes]:
    return harness.snapshot(directory)


class CrossAuthorityTests(unittest.TestCase):
    """X2″ and X17″: one writer capability per entry point, checked on kind and values.

    The expectations below restate R5.3's rules, not the module's table.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve() / "cross"
        _seed_pairs(self.directory)

    def _journal(self, **options) -> CrossJournal:
        return CrossJournal(self.directory, storage=harness.TracingStorage(self.directory, **options))

    def test_each_writer_writes_only_its_own_row(self) -> None:
        from floati.cross_authority import Capability

        refused = [
            ("M never activates", "M", _activation("p9", "c3")),
            ("ingress never narrows trust", "I", _revoked("p1", "local")),
            ("ingress never starts disable", "I", _optin("disable_started", "local")),
            ("ingress never opens a ceremony", "I", _opened("c9")),
            ("a dialler's frames never write outbound", "I", _pair_record("remote_outbound", "p1", 1)),
            ("a dialler's frames never write a local ack", "I", _pair_record("remote_local_ack", "p1", 1)),
            ("a dialler's frames never write an address", "I", _record(
                "cross_peer_address", "cross-peer-address-i", pair_id="p1", addresses=["192.0.2.1:1"])),
            ("attended pairing never revokes", "A", _revoked("p1", "local")),
            ("local verbs never activate", "L", _activation("p9", "c3")),
            ("maintenance never revokes as local", "M", _revoked("p1", "local")),
            ("local verbs never reconstruct", "L", _revoked("p1", "key_absent_reconstructed")),
            ("local verbs never close as abort", "L", _ceremony_closed("c3")),
            ("attended pairing never closes as disabled", "A", dict(_ceremony_closed("c3"), cause="disabled")),
            ("maintenance never closes as abort", "M", _ceremony_closed("c3")),
            ("governed repair writes nothing here", "G", data_record(1)),
        ]
        principals = {
            "I": Capability("I", "p1", verb="envelope"),
            "A": Capability("A", verb="pair_show", ceremony_token="a" * 32),
            "L": Capability("L", "p1", verb="peer_address"),
            "M": Capability("M", verb="maintain"),
            "G": Capability("G"),
        }
        for why, writer, record in refused:
            with self.subTest(why):
                before = _journal_bytes(self.directory)
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._journal().append(principals[writer], record)
                self.assertEqual("cross_capability_refused", caught.exception.code)
                self.assertEqual(before, _journal_bytes(self.directory))
        allowed = [
            ("a session records its own pair's frame", principals["I"],
             _pair_record("remote_inbound", "p1", 1)),
            ("a local verb records an address", principals["L"], _record(
                "cross_peer_address", "cross-peer-address-l", pair_id="p1", addresses=["192.0.2.1:1"])),
            ("maintenance closes a crashed ceremony", principals["M"], _record(
                "cross_ceremony_closed", "cross-ceremony-closed-c3", ceremony_id="c3", cause="crash")),
            ("attended pairing opens a ceremony", principals["A"], dict(_opened("c4"), ceremony_token="a" * 32)),
        ]
        for why, capability, record in allowed:
            with self.subTest(why):
                self.assertEqual("committed", self._journal().append(capability, record).status)
        journal = self._journal()
        self.assertEqual("committed", journal.append(principals["I"], _accepted_delivery()).status)
        displayed = journal.append(Capability("L", "p1", verb="present"), _delivery_completion("remote_presented"))
        self.assertEqual("committed", displayed.status)
        ack = dict(_delivery_completion("remote_local_ack"), release_id=displayed.record_id)
        before = _journal_bytes(self.directory)
        with self.assertRaises(ProtocolRefusal):
            journal.append(principals["L"], ack, output_token=_DELIVERY_TOKEN)
        self.assertEqual(before, _journal_bytes(self.directory))
        self.assertEqual("committed", journal.append(Capability("L", "p1", verb="ack"), ack,
                                                      output_token=_DELIVERY_TOKEN).status)

    def test_a_session_cannot_write_for_another_pair(self) -> None:
        from floati.cross_authority import Capability

        before = _journal_bytes(self.directory)
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(self._journal(), Capability("I", pair_id="p1"), _pair_record("remote_inbound", "p2", 1))
        self.assertEqual("cross_pair_scope_refused", caught.exception.code)
        self.assertEqual(before, _journal_bytes(self.directory))

    def test_ingress_checks_nested_scope_and_refuses_pooled_capabilities(self) -> None:
        from floati.cross_authority import Capability

        record = _pair_record("remote_inbound", "p1", 49)
        journal = self._journal()
        before = journal.read_committed()
        with self.assertRaises(ProtocolRefusal) as caught:
            journal.append("I", record)
        self.assertEqual("cross_capability_refused", caught.exception.code)
        for writer, candidate, code in (
            (Capability("I", "p1", verb="envelope"), dict(record, envelope={"pair_id": "p2"}),
             "cross_pair_scope_refused"),
            (Capability("I", "p1", verb="receipt"), record, "cross_capability_refused"),
        ):
            with self.subTest(writer=writer, code=code), self.assertRaises(ProtocolRefusal) as caught:
                journal.append(writer, candidate)
            self.assertEqual(code, caught.exception.code)
            self.assertEqual(before, journal.read_committed())
        self.assertEqual("committed", journal.append(Capability("I", "p1", verb="envelope"), record).status)

    def test_ingress_attempt_requires_its_committed_local_outbound(self) -> None:
        from floati.cross_authority import Capability

        journal = self._journal()
        ingress = Capability("I", pair_id="p1")
        attempt = _record("remote_attempt", "remote-attempt-a-1", pair_id="p1",
                          envelope_id="envelope-a", attempt_no=1)
        for pair, envelope in (("p1", "envelope-b"), ("p2", "envelope-a"), ("p1", "envelope-a")):
            with self.subTest(outbound_pair=pair, outbound_envelope=envelope):
                before = _journal_bytes(self.directory)
                with self.assertRaises(ProtocolRefusal) as caught:
                    _write(journal, ingress, attempt)
                self.assertEqual("cross_capability_refused", caught.exception.code)
                self.assertEqual(before, _journal_bytes(self.directory))
            outbound = _record("remote_outbound", f"outbound-{pair}-{envelope}", pair_id=pair,
                               envelope_id=envelope)
            self.assertEqual("committed", _write(journal, "L", outbound).status)
        self.assertEqual("committed", _write(journal, ingress, attempt).status)

    def test_disable_admits_data_only_for_pairs_awaiting_revocation(self) -> None:
        from floati.cross_authority import Capability

        journal = self._journal()
        closing = Capability("I", pair_id="p1")
        active = Capability("I", pair_id="p2")
        self.assertEqual("committed", _write(journal, closing, _accepted_delivery()).status)
        confirmation = _delivery_completion("remote_receipt_confirmed")
        self.assertEqual("committed", _write(journal, "L", _optin("disable_started", "local")).status)
        for writer, record in (
            (closing, _pair_record("remote_inbound", "p1", 1)),
            (active, _pair_record("remote_inbound", "p2", 1)),
            (active, _pair_record("remote_attempt", "p2", 1)),
            (active, _pair_record("remote_transport_attempt", "p2", 1)),
            (active, _pair_record("remote_session_opened", "p2", 1)),
            (active, _pair_record("remote_endpoint_registered", "p2", 1)),
            ("L", _pair_record("remote_outbound", "p2", 1)),
        ):
            with self.subTest(kind=record["kind"], pair=record["pair_id"]):
                before = _journal_bytes(self.directory)
                with self.assertRaises(ProtocolRefusal) as caught:
                    _write(journal, writer, record)
                self.assertEqual("cross_disable_in_progress", caught.exception.code)
                self.assertEqual(before, _journal_bytes(self.directory))
        self.assertEqual("committed", _write(journal, closing, confirmation).status)
        self.assertEqual("committed", _write(journal, "L", _revoked("p1", "disable")).status)
        for phase, code in (("started", "cross_pair_scope_refused"), ("complete", "cross_disabled")):
            if phase == "complete":
                self.assertEqual("complete", _closeout(journal)["state"])
            with self.subTest(phase=phase):
                before = _journal_bytes(self.directory)
                with self.assertRaises(ProtocolRefusal) as caught:
                    _write(journal, closing, confirmation)
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(before, _journal_bytes(self.directory))

    def test_disable_completion_matches_the_pre_stop_receipt_coordinates(self) -> None:
        journal = self._journal()
        accepted = _accepted_delivery()
        self.assertEqual("committed", _write(journal, "I", accepted).status)
        completion = _delivery_completion("remote_receipt_confirmed")
        outbound = _record("remote_outbound", "outbound-p2", pair_id="p2", direction="T-to-S", wire_seq=1,
                           envelope_id="envelope-out", envelope_digest="a" * 64)
        attempt = dict(outbound, kind="remote_attempt", id="attempt-p2")
        self.assertEqual("committed", _write(journal, "L", outbound).status)
        self.assertEqual("committed", _write(journal, "L", attempt).status)
        self.assertEqual("committed", _write(journal, "L", _optin("disable_started", "local")).status)
        before = journal.read_committed()
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(journal, "I", dict(completion, envelope_id="another-envelope"))
        self.assertEqual("cross_completion_delivery_invalid", caught.exception.code)
        self.assertEqual(before, journal.read_committed())
        self.assertEqual("committed", _write(journal, "I", completion).status)
        peer_receipt = dict(outbound, kind="remote_peer_receipt", id="peer-receipt-p2")
        before = journal.read_committed()
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(journal, "I", dict(peer_receipt, envelope_digest="b" * 64))
        self.assertEqual("cross_disable_in_progress", caught.exception.code)
        self.assertEqual(before, journal.read_committed())
        self.assertEqual("committed", _write(journal, "I", peer_receipt).status)
        self.assertEqual("existing", _write(journal, "I", peer_receipt).status)
        self.assertEqual("committed", _write(journal, "L", _revoked("p2", "disable")).status)
        before = journal.read_committed()
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(journal, "I", peer_receipt)
        self.assertEqual("cross_pair_scope_refused", caught.exception.code)
        self.assertEqual(before, journal.read_committed())

    def test_resolution_compares_the_original_immutable_operation(self) -> None:
        journal = self._journal()
        caller = Capability("L", "p1", verb="send")
        body = _pair_record("remote_outbound", "p1", 3)
        self.assertEqual("absent", journal.resolve(caller, body))
        other = dict(body, wire_seq=4)
        self.assertEqual("committed", journal.append(caller, other).status)
        self.assertEqual("conflict", journal.resolve(caller, body))
        self.assertEqual("committed", journal.resolve(caller, other))
        with self.assertRaises(ProtocolRefusal) as caught:
            journal.resolve(Capability("L", "p2", verb="send"), other)
        self.assertEqual("cross_pair_scope_refused", caught.exception.code)

    def test_each_transport_role_proves_its_authenticated_open(self) -> None:
        for role in ("listener", "dialler"):
            with self.subTest(role=role):
                directory = self.directory.parent / role
                harness.materialize(harness.snapshot(self.directory), directory)
                owner = object()
                journal = CrossJournal(directory, authenticate_open=lambda proof, stream: stream is owner)
                proof = _record("remote_session_opened", f"opened-{role}", schema_version=1,
                                pair_id="p1", direction="S-to-T", stream_id="1" * 32,
                                enable_id=ENABLE_ONE, enable_generation=1, local_side=role,
                                peer_der_sha256="a" * 64, gate_counter=7, owner_id=f"owner-{role}")
                with self.assertRaises(ProtocolRefusal):
                    journal.open_authenticated(proof, object())
                live = journal.open_authenticated(proof, owner)
                inbound = _accepted_delivery()
                self.assertEqual("committed", journal.append(live.capability("envelope"), inbound).status)
                self.assertEqual("committed", _write(journal, "L", _optin("disable_started", "local")).status)
                confirmation = _delivery_completion("remote_receipt_confirmed")
                self.assertEqual("committed", journal.append(live.capability("receipt_ack"), confirmation).status)
                journal.close_authenticated(live)
                with self.assertRaises(ProtocolRefusal):
                    journal.append(live.capability("receipt_ack"), confirmation)
                with self.assertRaises(ProtocolRefusal):
                    journal.open_authenticated(proof, owner)

    def test_r9_first_dial_uses_local_prerequisites_before_an_open_exists(self) -> None:
        pair = "a" * 32
        sender = "sender"
        trace = []
        owner = _OutputStream("S-to-T")

        def prerequisites(capability, outbound):
            trace.append(("prerequisites", outbound["id"]))
            return capability.writer == "L"

        def authenticate(proof, stream):
            trace.append(("authenticate", proof["stream_id"]))
            return stream is owner

        journal = CrossJournal(self.directory, authorize_local_send=lambda capability, name: name == sender,
                               authenticate_open=authenticate, dial_prerequisites=prerequisites)
        _write(journal, "A", _activation(pair, "c3"))
        send = Capability("L", pair, verb="send")
        request = {"verb": "send", "envelope_fields": {
            "v": 5, "kind": "message", "message_class": "normal", "pair_id": pair,
            "direction": "S-to-T", "sender_node": sender, "recipient_node": "peer",
            "reply_to": None, "repo": None, "sha": None, "doc": None, "note": "first dial",
        }}
        item = journal.allocate_outbound(send, "first", request, "2026-11-25T00:00:00.000Z")
        self.assertEqual([], trace)
        self.assertEqual({}, journal._live_streams)
        with self.assertRaises(ProtocolRefusal):
            journal.begin_local_dial(Capability("I", pair, verb="reconcile"), item.record_id, sender)
        dispatch = journal.begin_local_dial(send, item.record_id, sender)
        self.assertEqual([("prerequisites", item.record_id)], trace)
        self.assertEqual({}, journal._live_streams)
        proof = _record("remote_session_opened", "r9-dial-open", schema_version=1, pair_id=pair,
                        direction="S-to-T", stream_id="a" * 32, enable_id=ENABLE_ONE, enable_generation=1,
                        local_side="dialler", peer_der_sha256="b" * 64, gate_counter=7, owner_id="dial-owner")
        with self.assertRaises(ProtocolRefusal):
            journal.open_authenticated(proof, owner)
        with self.assertRaises(ProtocolRefusal):
            journal.open_authenticated(proof, owner, dial=dispatch)
        from floati.framing import decode_frames
        proof["owner_id"] = decode_frames(dispatch.attempt_frame)[0]["id"]
        live = journal.open_authenticated(proof, owner, dial=dispatch)
        self.assertEqual(pair, live.pair_id)
        self.assertEqual("authenticate", trace[-1][0])
        owner.ready = False
        with self.assertRaises(ProtocolRefusal):
            journal.prepare_local_output(send, live, item.record_id, sender)
        owner.ready = True
        output = journal.prepare_local_output(send, live, item.record_id, sender)
        self.assertTrue(journal.deposit_local_output(output))
        self.assertEqual(1, owner.queue.qsize())
        with self.assertRaises(ProtocolRefusal):
            journal.open_authenticated(dict(proof, id="other-open", stream_id="b" * 32), owner, dial=dispatch)
        rows = journal.read_committed(require_settled=True).records
        self.assertEqual(1, sum(row["kind"] == "remote_transport_attempt" for row in rows))
        self.assertEqual(1, sum(row["kind"] == "remote_session_opened" for row in rows))

    def test_listener_first_local_send_uses_its_existing_open(self) -> None:
        pair = "a" * 32
        owner = _OutputStream("T-to-S")
        journal = CrossJournal(self.directory, authenticate_open=lambda proof, stream: stream is owner,
                               authorize_local_send=lambda capability, sender: sender == "sender")
        _write(journal, "A", _activation(pair, "c3"))
        send = Capability("L", pair, verb="send")
        request = {"verb": "send", "envelope_fields": {
            "v": 5, "kind": "message", "message_class": "normal", "pair_id": pair,
            "direction": "T-to-S", "sender_node": "sender", "recipient_node": "peer",
            "reply_to": None, "repo": None, "sha": None, "doc": None, "note": "reverse",
        }}
        intent = journal.allocate_outbound(send, "reverse", request, "2026-11-25T00:00:00.000Z")
        proof = _record("remote_session_opened", "listener-open", schema_version=1, pair_id=pair,
                        direction="S-to-T", stream_id="a" * 32, enable_id=ENABLE_ONE, enable_generation=1,
                        local_side="listener", peer_der_sha256="b" * 64, gate_counter=7, owner_id="endpoint")
        outbound = next(row for row in journal.read_committed().records if row["id"] == intent.record_id)
        attempted = _record("remote_attempt", "unowned-first-attempt", pair_id=pair,
                            envelope_id=outbound["envelope_id"], outbound_id=outbound["id"], direction="T-to-S")
        with self.assertRaises(ProtocolRefusal):
            journal.append(send, attempted)
        live = journal.open_authenticated(proof, owner)
        with self.assertRaises(ProtocolRefusal):
            journal.prepare_local_output(live.capability("reconcile"), live, intent.record_id, "sender")
        dispatch = journal.prepare_local_output(send, live, intent.record_id, "sender")
        self.assertTrue(journal.deposit_local_output(dispatch))
        first = owner.queue.get_nowait()
        self.assertFalse(journal.deposit_local_output(dispatch))
        def prepared(key):
            intent = journal.allocate_outbound(send, key, request, "2026-11-25T00:00:00.000Z")
            return journal.prepare_local_output(send, live, intent.record_id, "sender")

        full = prepared("full")
        owner.queue.put_nowait(b"already queued")
        self.assertFalse(journal.deposit_local_output(full))
        self.assertEqual(1, len(owner.denied))
        self.assertEqual(b"already queued", owner.queue.get_nowait())
        self.assertTrue(owner.queue.empty())
        self.assertFalse(journal.deposit_local_output(full))

        revoked = prepared("withdrawn-permit")
        owner.ready = False
        self.assertFalse(journal.deposit_local_output(revoked))
        owner.ready = True
        disconnected = prepared("disconnected")
        journal.close_authenticated(live)
        self.assertFalse(journal.deposit_local_output(disconnected))

        live = journal.open_authenticated(dict(proof, stream_id="b" * 32, id="listener-reopen", gate_counter=8), owner)
        stopped = prepared("stop-first")
        self.assertEqual("committed", _write(journal, "L", _optin("disable_started", "local")).status)
        self.assertFalse(journal.deposit_local_output(stopped))
        self.assertTrue(owner.queue.empty())
        self.assertEqual(1, len(owner.denied))
        self.assertEqual(int.from_bytes(first[:4], "big"), len(first) - 4)
        from floati.framing import decode_frames

        queued = decode_frames(first[4:] + b"\n")[0]
        self.assertEqual(("envelope", "T-to-S", 1), (queued["t"], queued["envelope"]["direction"], queued["envelope"]["seq"]))
        records = journal.read_committed(require_settled=True).records
        self.assertEqual(5, sum(row["kind"] == "remote_attempt" for row in records))
        self.assertFalse(any(row["kind"] == "remote_transport_attempt" for row in records))

    def test_ingress_defers_when_maintenance_is_due(self) -> None:
        """C3: an I frame never causes a repair, a seal or a successor write."""

        from floati.cross_authority import Capability

        crashes = {
            "a tail": lambda label: label.startswith("sync:journal-"),
            "an unsealed checkpoint": lambda label: label == "rename:journal.commit",
        }
        for number, (why, stop_after) in enumerate(crashes.items(), start=1):
            with self.subTest(why):
                try:
                    _write(self._journal(stop_after=stop_after), Capability("L"), _record("cross_peer_address", f"cross-peer-address-{number}",
                                                 pair_id="p1", addresses=["192.0.2.1:1"]))
                except harness.SimulatedCrash:
                    pass
                before = _journal_bytes(self.directory)
                ingress = Capability("I", pair_id="p1")
                for attempt in (
                    lambda journal: _write(journal, ingress, _pair_record("remote_inbound", "p1", number)),
                    lambda journal: journal.maintain(ingress),
                    lambda journal: journal.compact(ingress, "explicit"),
                ):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        attempt(self._journal())
                    self.assertIn(caught.exception.code, {"cross_maintenance_due", "cross_capability_refused"})
                    self.assertEqual(before, _journal_bytes(self.directory))
                local = self._journal()
                result = _write(local, Capability("L"), _pair_record("remote_outbound", "p1", number))
                self.assertEqual("committed", result.status)
                origins = {(origin, authority) for origin, authority, _ in local.census}
                self.assertIn(("L", "M"), origins)
                self.assertNotIn(("I", "M"), origins)

    def test_a_damaged_committed_prefix_fences_every_writer(self) -> None:
        from floati.cross_authority import Capability

        journal_path = next(self.directory.glob("journal-g*.jsonl"))
        data = bytearray(journal_path.read_bytes())
        data[data.index(b'"enable_complete"') + 1] ^= 0x01
        journal_path.write_bytes(bytes(data))
        before = _journal_bytes(self.directory)
        for writer in ("I", "A", "L", "M", "G"):
            with self.subTest(writer=writer):
                with self.assertRaises(FloatiError) as caught:
                    self._journal().maintain(Capability(writer))
                self.assertIn(caught.exception.code, {"cross_journal_corrupt", "cross_capability_refused"})
                self.assertEqual(before, _journal_bytes(self.directory))
        with self.assertRaises(FloatiError) as caught:
            CrossJournal(self.directory).read_committed()
        self.assertEqual("cross_journal_corrupt", caught.exception.code)


RECEIPT_ITEMS = [{"item": "unit", "class": "confirmed_absent"}]


def _closeout(journal: CrossJournal) -> object:
    return journal.closeout(Capability("L", verb="disable"), RECEIPT_ITEMS)


def _plan_records(records) -> List[str]:
    return [str(row["id"]) for row in records if row["kind"] not in {
        "cross_journal_tail_repaired", "cross_journal_link", "cross_trust_checkpoint"}]


class CrossRestartTests(unittest.TestCase):
    """Partial shutdown resumes deterministically; racing processes converge."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.seeded = self.base / "seeded" / "cross"
        _seed_pairs(self.seeded, utc_day=lambda: "2026-11-24")

    def _copy_of_seed(self, name: str) -> Path:
        directory = self.base / name / "cross"
        harness.materialize(harness.snapshot(self.seeded), directory)
        return directory

    def test_disable_closes_the_open_ceremony_with_its_own_cause(self) -> None:
        journal = CrossJournal(self.seeded, storage=harness.TracingStorage(self.seeded))
        self.assertEqual("complete", _closeout(journal)["state"])
        self.assertEqual(
            [("c3", "disabled")],
            [(row["ceremony_id"], row["cause"]) for row in journal.read_committed().records
             if row["kind"] == "cross_ceremony_closed"],
        )

    def test_enable_generation_is_carried_into_the_trust_checkpoint(self) -> None:
        directory = self.base / "enabled"
        journal = CrossJournal(directory, storage=harness.TracingStorage(directory))
        view = journal.create(ENABLE_ONE, enable_generation=7)
        enable = next(row for row in view.records if row.get("phase") == "enable_started")
        with self.subTest(record="enable"):
            self.assertEqual(7, enable.get("enable_generation"))
            self.assertEqual(view, journal.create(ENABLE_ONE, enable_generation=7))
            with self.assertRaises(ProtocolRefusal) as caught:
                journal.create(ENABLE_ONE, enable_generation=8)
            self.assertEqual("cross_record_id_conflict", caught.exception.code)
        for cycle in range(2):
            for number in range(3):
                ceremony = f"retired-{cycle}-{number}"
                self.assertEqual("committed", _write(journal, "A", _opened(ceremony)).status)
                self.assertEqual("committed", _write(journal, "A", _ceremony_closed(ceremony)).status)
            journal.compact()
            view = journal.read_committed()
            trust = next(row for row in view.records if row["kind"] == "cross_trust_checkpoint")
            with self.subTest(record="trust checkpoint", journal_generation=view.generation):
                self.assertEqual(7, trust.get("enable_generation"))

    def test_ordered_trust_checkpoint_matches_v6_vector(self) -> None:
        enable = "a" * 32
        journal = CrossJournal(self.base / "ordered")
        journal.create(enable, enable_generation=7)
        _write(journal, "A", _record("cross_optin", f"cross-optin-{enable}-enable-complete",
                                    phase="enable_complete", enable_id=enable, enable_generation=7, cause=None))
        for number in range(3):
            ceremony = f"retired-{number}"
            _write(journal, "A", _opened(ceremony))
            _write(journal, "A", _ceremony_closed(ceremony))
        journal.compact()
        rows = journal.read_committed().records
        trust = next(row for row in rows if row["kind"] == "cross_trust_checkpoint")
        self.assertEqual((enable, 7, 2, "1aee40f0d1f81f1bb5701938c27866b8005e4586a3294d09e7c6e43bbdef7491"),
                         tuple(trust.get(k) for k in ("enable_id", "enable_generation", "trust_count", "trust_head")))

    def test_ceremony_quota_survives_abort_compaction_and_restart(self) -> None:
        directory = self.base / "quota"
        clock = lambda: "2026-01-02"
        journal = CrossJournal(directory, utc_day=clock)
        journal.create(ENABLE_ONE)
        _write(journal, "A", _optin("enable_complete", None))
        for number in range(32):
            ceremony = f"charged-{number}"
            verb = "pair_show" if number % 2 == 0 else "pair_join"
            capability = Capability("A", verb=verb)
            self.assertEqual("committed", _write(journal, capability, _opened(ceremony)).status)
            if number < 31:
                _write(journal, capability, _ceremony_closed(ceremony))
        journal.compact()
        fresh = CrossJournal(directory, utc_day=clock)
        before = fresh.read_committed()
        self.assertEqual("existing", _write(fresh, Capability("A", verb="pair_join"), _opened("charged-31")).status)
        self.assertEqual(before, fresh.read_committed())
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(fresh, "A", _opened("over-limit"))
        self.assertEqual("cross_ceremony_quota", caught.exception.code)
        self.assertEqual(before, fresh.read_committed())
        later = CrossJournal(directory, utc_day=lambda: "2026-01-03")
        self.assertEqual("committed", _write(later, "A", _opened("next-date")).status)
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(fresh, "A", _opened("backwards-date"))
        self.assertEqual("cross_ceremony_quota", caught.exception.code)

    def test_legacy_quota_requires_a_provably_fresh_bucket(self) -> None:
        from floati import cross_coordinates

        cutoff = getattr(cross_coordinates, "legacy_quota_cutoff", lambda *args, **kwargs: None)
        self.assertEqual("2026-11-25", cutoff("2026-11-25", ["2026-11-24"], exclusive=True))
        self.assertEqual("2026-11-28", cutoff("2026-11-25", ["2026-11-28"], exclusive=True))
        with self.assertRaises(ProtocolRefusal):
            cutoff("2026-11-25", [], exclusive=False)

        from contextlib import nullcontext

        self.assertEqual("complete", _closeout(CrossJournal(self.seeded))["state"])
        epoch = Capability("A", verb="legacy_rollover")
        date = lambda: "2026-11-25"
        with self.assertRaises(ProtocolRefusal):
            CrossJournal(self.seeded, utc_day=date).seal_legacy_quota_cutoff(epoch)
        journal = CrossJournal(self.seeded, utc_day=date, legacy_exclusion=lambda: nullcontext(["2026-11-24"]))
        with self.assertRaises(ProtocolRefusal):
            journal.seal_legacy_quota_cutoff(epoch)

        # Preserve the old v3 cutoff control with its historical framed shape.
        # No production migration entry can publish this unbound format.
        checkpoint, raw = journal._load()
        predecessor = journal._previous(checkpoint, raw)
        historic = dict(checkpoint, format="floati-cross-checkpoint-v3", previous=predecessor,
                        quota={"mode": "legacy_blocked_through", "day": date(), "predecessor": predecessor})
        frame = encode_frame(historic)
        (self.seeded / "journal.commit").write_bytes(frame)
        (self.seeded / "journal.commit.seal").write_bytes(encode_frame({
            "format": "floati-cross-seal-v1", "checkpoint_sha256": hashlib.sha256(frame).hexdigest(),
        }))
        same_day = CrossJournal(self.seeded, utc_day=date)
        same_day.create(ENABLE_TWO, enable_generation=2)
        complete = _record("cross_optin", f"cross-optin-{ENABLE_TWO}-enable-complete", phase="enable_complete",
                           enable_id=ENABLE_TWO, enable_generation=2, cause=None)
        _write(same_day, "A", complete)
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(same_day, "A", _opened("blocked"))
        self.assertEqual("cross_ceremony_quota", caught.exception.code)
        later = CrossJournal(self.seeded, utc_day=lambda: "2026-11-26")
        self.assertEqual("committed", _write(later, "A", _opened("next-date")).status)
        self.assertEqual("existing", _write(later, "A", _opened("next-date")).status)

    def test_quota_stays_charged_across_enable_rollover(self) -> None:
        directory = self.base / "rollover"
        clock = lambda: "2026-01-02"
        journal = CrossJournal(directory, utc_day=clock)
        journal.create(ENABLE_ONE)
        _write(journal, "A", _optin("enable_complete", None))
        for number in range(32):
            ceremony = f"old-{number}"
            _write(journal, "A", _opened(ceremony))
            _write(journal, "A", _ceremony_closed(ceremony))
        self.assertEqual("complete", _closeout(journal)["state"])
        journal.compact()
        fresh = CrossJournal(directory, utc_day=clock)
        view = fresh.create(ENABLE_TWO, enable_generation=2)
        self.assertEqual(ENABLE_TWO, next(row["enable_id"] for row in reversed(view.records)
                                    if row.get("phase") == "enable_started"))
        _write(fresh, "A", _record("cross_optin", f"cross-optin-{ENABLE_TWO}-enable-complete", phase="enable_complete",
                                  enable_id=ENABLE_TWO, enable_generation=2, cause=None))
        before = fresh.read_committed()
        with self.assertRaises(ProtocolRefusal) as caught:
            _write(fresh, "A", _opened("still-charged"))
        self.assertEqual("cross_ceremony_quota", caught.exception.code)
        self.assertEqual(before, fresh.read_committed())

    def test_checked_frontier_survives_compaction_and_rejects_regression(self) -> None:
        journal = CrossJournal(self.seeded)
        capability = Capability("I", "p1", verb="reconcile")
        record = _record("remote_reconcile_frontier", "frontier-1", pair_id="p1", direction="S-to-T",
                         frontier=1, checkpoint_digest="a" * 64)
        self.assertEqual("committed", journal.append(capability, record).status)
        for name in ("obsolete-1", "obsolete-2"):
            _write(journal, "A", _opened(name))
            _write(journal, "A", _ceremony_closed(name))
        journal.compact()
        fresh = CrossJournal(self.seeded)
        before = fresh.read_committed()
        with self.assertRaises(ProtocolRefusal) as caught:
            fresh.append(capability, dict(record, id="frontier-0", frontier=0, checkpoint_digest=None))
        self.assertEqual("cross_delivery_history_lost", caught.exception.code)
        self.assertEqual(before, fresh.read_committed())


    def test_independent_narrowing_encoding_matches_v7(self) -> None:
        from floati.cross_anchor import Anchor, Narrowing

        enable, pair = "a" * 32, "b" * 32
        prior = Anchor((enable, 7, 8, "2" * 64), 3, "1" * 64)
        position = ("journal-g3.jsonl", f"cross-{enable}-g3", 3, 12, "3" * 64, 6500)
        body = _record("cross_pair_revoked", f"cross-pair-revoked-{pair}", schema_version=1, pair_id=pair,
                       cause="local", idempotency_key="retire-q", enable_id=enable, enable_generation=7)
        payload = Narrowing(prior, position, "4" * 64, encode_frame(body)).encoded()
        self.assertEqual((875, "0aedbbb78abd55ecb9a763232ef651fae1c8035bba6462326bdba226e92c5f31"),
                         (len(payload), hashlib.sha256(payload).hexdigest()))

    def test_narrowing_results_survive_journal_only_restore(self) -> None:
        from floati.cross_anchor import Anchor, Narrowing

        class Store:
            value = None

            def __init__(self):
                self.cut_on = None
                self.apply = False
                self.trace = []

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                if type(successor) is self.cut_on:
                    self.cut_on = None
                    if self.apply:
                        self.value = successor
                        self.trace.append(type(successor))
                    return False
                self.value = successor
                self.trace.append(type(successor))
                return True

        for scope in ("pair", "root"):
            for cut in (Narrowing, Anchor):
                for applied in (False, True):
                    with self.subTest(scope=scope, cut=cut.__name__, applied=applied):
                        directory = self.base / f"independent-{scope}-{cut.__name__}-{applied}"
                        store = Store()
                        journal = CrossJournal(directory, anchor_store=store)
                        journal.create(ENABLE_ONE)
                        _write(journal, "A", _optin("enable_complete", None))
                        _write(journal, "A", _opened("original"))
                        _write(journal, "A", _activation("p1", "original"))
                        old = journal.read_committed()
                        earlier = harness.snapshot(directory)
                        store.cut_on, store.apply = cut, applied
                        store.trace.clear()
                        body = (_revoked("p1", "local") if scope == "pair"
                                else _optin("disable_started", "local"))
                        result = _write(journal, "L", body)
                        self.assertNotEqual("committed", result.status)
                        self.assertEqual("unknown", result.independent_denial)
                        restored = directory.with_name(directory.name + "-restored")
                        harness.materialize(earlier, restored)
                        fresh = CrossJournal(restored, anchor_store=store)
                        if cut is Narrowing and not applied:
                            self.assertEqual(old, fresh.read_committed(require_settled=True))
                            self.assertEqual([], store.trace)
                            continue
                        self.assertTrue(fresh.read_committed().pending)
                        if cut is Anchor and applied:
                            for _ in range(2):
                                with self.assertRaises(ProtocolRefusal):
                                    fresh.maintain()
                            self.assertEqual([Narrowing, Anchor], store.trace)
                            continue
                        fresh.maintain()
                        fresh.maintain()
                        rows = fresh.read_committed(require_settled=True).records
                        self.assertEqual(1, sum(row["id"] == body["id"] for row in rows))
                        self.assertEqual([Narrowing, Anchor], store.trace)

    def test_outstanding_store_write_keeps_the_predecessor_fenced(self) -> None:
        from floati import cross_anchor

        class Store:
            value = None
            outstanding = None
            lose = False

            def read(self):
                if self.outstanding is not None and hasattr(cross_anchor, "OutstandingWrite"):
                    return cross_anchor.OutstandingWrite()
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                if self.lose:
                    self.lose = False
                    self.outstanding = successor
                    return False
                self.value = successor
                return True

        store = Store()
        directory = self.base / "outstanding"
        journal = CrossJournal(directory, anchor_store=store)
        journal.create(ENABLE_ONE)
        _write(journal, "A", _optin("enable_complete", None))
        _write(journal, "A", _opened("original"))
        _write(journal, "A", _activation("p1", "original"))
        earlier = harness.snapshot(directory)
        store.lose = True
        revocation = _revoked("p1", "local")
        self.assertEqual("indeterminate", _write(journal, "L", revocation).status)
        restored = self.base / "restore-outstanding"
        harness.materialize(earlier, restored)
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(reader)
            try:
                fresh = CrossJournal(restored, anchor_store=store)
                os.write(writer, b"pending" if fresh.read_committed().pending else b"settled")
            finally:
                os.close(writer)
                os._exit(0)
        os.close(writer)
        try:
            self.assertEqual(b"pending", os.read(reader, 32))
        finally:
            os.close(reader)
            os.waitpid(pid, 0)
        fresh = CrossJournal(restored, anchor_store=store)
        with self.assertRaises(ProtocolRefusal):
            fresh.maintain()
        self.assertEqual(earlier, harness.snapshot(restored))
        store.value, store.outstanding = store.outstanding, None
        fresh.maintain()
        fresh.maintain()
        self.assertEqual([revocation["id"]], [row["id"] for row in fresh.read_committed().records
                                              if row["kind"] == "cross_pair_revoked"])

    def test_current_anchor_refuses_a_restored_pre_revocation_generation(self) -> None:
        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                return True

        store = Store()
        directory = self.base / "anchored"
        journal = CrossJournal(directory, anchor_store=store)
        enable = "a" * 32
        journal.create(enable, enable_generation=7)
        _write(journal, "A", _record("cross_optin", f"cross-optin-{enable}-enable-complete",
                                    phase="enable_complete", enable_id=enable, enable_generation=7, cause=None))
        _write(journal, "A", _opened("original"))
        _write(journal, "A", _activation("p1", "original"))
        for name in ("obsolete-1", "obsolete-2"):
            _write(journal, "A", _opened(name))
            _write(journal, "A", _ceremony_closed(name))
        journal.compact()
        _write(journal, "A", _opened("added"))
        _write(journal, "A", _activation("p2", "added"))
        earlier = harness.snapshot(directory)
        self.assertEqual("committed", _write(journal, "L", _revoked("p2", "local")).status)
        current = store.read()
        self.assertIsNotNone(current)
        restored = self.base / "restored"
        harness.materialize(earlier, restored)
        restored_journal = CrossJournal(restored, anchor_store=store)
        with self.assertRaises(ProtocolRefusal) as caught:
            restored_journal.maintain()
        self.assertEqual("cross_trust_anchor_mismatch", caught.exception.code)
        self.assertEqual(current, store.read())

    def test_successive_ceremonies_isolate_current_panel_and_history(self) -> None:
        import importlib

        panels = importlib.import_module("floati.cross_panel")

        class Surface:
            def __init__(self, *, isolated=True):
                self.isolated = isolated
                self.visible = None

            def clear(self):
                self.visible = None

            def draw(self, display):
                self.visible = display

        surface = Surface()
        lifecycle = panels.PanelLifecycle()
        first = lifecycle.begin(surface, "T")
        first_input = lifecycle.single_entry(first, lambda value: value == "synthetic-a")
        self.assertTrue(first_input("synthetic-a"))
        lifecycle.render_b(first, "synthetic-first-b")
        self.assertEqual("synthetic-first-b", surface.visible)
        lifecycle.end(first)

        second = lifecycle.begin(surface, "T")
        self.assertIsNone(surface.visible)
        self.assertFalse(first_input("synthetic-a"))
        with self.assertRaises(ProtocolRefusal):
            lifecycle.render_b(second, "synthetic-second-b")
        self.assertIsNone(surface.visible)
        lifecycle.end(second)
        with self.assertRaises(ProtocolRefusal):
            lifecycle.begin(Surface(isolated=False), "S")

    def test_gate_entries_keep_each_ceremony_owner_across_replacement(self) -> None:
        import importlib
        import json

        gate = importlib.import_module("floati.cross_gate")
        pair = "6e5adfd61c91fc7e638d59c5355fde1e"
        owner = gate.GateOwner(f"cross-{'a' * 32}-g1", "a" * 32, 7, "b" * 32, pair)
        initial = gate.GateImage.empty(owner.journal_id).provision(owner)
        self.assertEqual(486, len(initial.encoded()))
        self.assertEqual("e46317aec17b47200c0a901cc8ccfe3b400eb6ee073a249e0b4a043b04afbab8",
                         hashlib.sha256(initial.encoded()).hexdigest())
        self.assertEqual(initial.encoded(), gate.GateImage.decode(initial.encoded()).provision(owner).encoded())
        emptied = initial.collect(owner, abandoned=True, activated=False)
        self.assertEqual("9543db3569449264a272fb1ab33f8d79b0e51d2aecebdd3e70d201052376e5ab",
                         hashlib.sha256(emptied.encoded()).hexdigest())
        self.assertEqual(emptied.encoded(), emptied.collect(owner, abandoned=True, activated=False).encoded())
        foreign = gate.GateOwner(owner.journal_id, owner.enable_id, owner.enable_generation, "c" * 32, pair)
        with self.assertRaises(ProtocolRefusal):
            initial.collect(foreign, abandoned=True, activated=False)
        self.assertEqual(initial.encoded(), initial.collect(owner, abandoned=True, activated=True).encoded())
        key_s, key_t = owner.keys()
        for variant, size, sha in (
            ("foreign", 486, "1c65eafd62d8d04f1bf0e47d2823d470e98de61b8194ea0a253c8ea9875a399a"),
            ("partial", 281, "858209cc0e0e83357ef2e3cec0cccbc796aae14ffeca9b249fc91c4ff3793a26"),
            ("foreign-partial", 281, "b75cf0a303479eb36b9d0addc06c3adab2d1a8a22b42d73bef8bd720bbe63652"),
            ("unbound-v5", 244, "22e5bcc37dd87f75415753aea0f4dd85f8e296ee65319f682c03b67ea47f4d8e"),
            ("used", 486, "9f077aa7a36d3bc93aa1bd1039c6ab42c3166a64817fd9fa5dd659923f392e71"),
        ):
            with self.subTest(variant=variant):
                image = json.loads(initial.encoded())
                entries = image["entries"]
                if variant in ("partial", "foreign-partial"):
                    del entries[key_t]
                if variant in ("foreign", "foreign-partial"):
                    for entry in entries.values():
                        entry["ceremony_token"] = "c" * 32
                elif variant == "unbound-v5":
                    image["v"] = 5
                    for entry in entries.values():
                        for key in ("enable_id", "enable_generation", "ceremony_token"):
                            del entry[key]
                elif variant == "used":
                    entries[key_s].update(in_hwm=3, max_session=3)
                raw = encode_frame(image)[:-1]
                self.assertEqual((size, sha), (len(raw), hashlib.sha256(raw).hexdigest()))
                if variant == "unbound-v5":
                    with self.assertRaises(ProtocolRefusal):
                        gate.GateImage.decode(raw)
                    continue
                snapshot = gate.GateImage.decode(raw)
                with self.assertRaises(ProtocolRefusal):
                    snapshot.provision(owner)
                if variant == "partial":
                    self.assertEqual(emptied.encoded(), snapshot.collect(owner, abandoned=True, activated=False).encoded())
                else:
                    with self.assertRaises(ProtocolRefusal):
                        snapshot.collect(owner, abandoned=True, activated=False)
        directory = self.base / "gate-file"
        directory.mkdir()
        storage = Storage(directory)
        storage.create("gate-state", emptied.encoded())
        gate.replace_gate(storage, emptied, initial)
        self.assertEqual(initial.encoded(), storage.read("gate-state"))
        self.assertEqual(emptied, gate.replace_gate(storage, initial, emptied))

    def test_sender_retirement_keeps_the_key_and_shared_wire_floor(self) -> None:
        self._assert_sender_retirement("send")

    def test_retired_ask_resolves_locally_with_an_empty_stream_registry(self) -> None:
        self._assert_sender_retirement("ask")

    def _assert_sender_retirement(self, verb):
        from floati.cross_coordinates import digest, enc, receipt_id

        directory = self.seeded
        pair = "6e5adfd61c91fc7e638d59c5355fde1e"
        direction = "S-to-T"
        def authorize(capability, sender):
            return sender in {"node-a", "node-b"}

        evidence_source = {}
        transport = []
        owner = object()

        def open_check(proof, handle):
            transport.append(("open", proof["stream_id"]))
            return handle is owner
        options = {"authorize_local_send": authorize, "authenticate_open": open_check}
        if verb == "ask":
            options["sender_dependencies"] = lambda outbound, receipt: evidence_source.get(outbound["id"]) == receipt
        journal = CrossJournal(directory, **options)
        _write(journal, "A", _activation(pair, "c3"))
        cap = Capability("L", pair, verb=verb)
        request = {"verb": verb, "envelope_fields": {
            "v": 5, "kind": "ask_question" if verb == "ask" else "message", "message_class": "normal", "pair_id": pair,
            "direction": direction, "sender_node": "node-a", "recipient_node": "node",
            "repo": None, "sha": None, "doc": None, "note": "retire this " * 100, "reply_to": None,
        }}
        stamp = "2026-11-24T12:00:00.000Z"
        allocated = journal.allocate_outbound(cap, "key", request, stamp)
        outbound = next(row for row in journal.read_committed().records if row["id"] == allocated.record_id)
        fields = {key: outbound[key] for key in ("pair_id", "direction", "envelope_id", "envelope_digest")}
        number = outbound["delivery_seq"]
        outcome = {"accepted": {"record_id": "rmsg-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f"}}
        identity = receipt_id(pair, direction, number, outbound["envelope_digest"])
        receipt = dict(fields, seq=number, v=5, receipt_id=identity, result=outcome)
        digest_value = digest(enc(b"floati-remote-receipt-v5"), enc(encode_frame(receipt)[:-1]))
        evidence = _record("remote_peer_receipt", identity, delivery_seq=number, receipt_body=receipt,
                           receipt_digest=digest_value, **fields)
        proof = _record("remote_session_opened", "past-open", schema_version=1, pair_id=pair,
                        direction=direction, stream_id="f" * 32, enable_id=ENABLE_ONE, enable_generation=1,
                        local_side="dialler", peer_der_sha256="e" * 64, gate_counter=7, owner_id="dial")
        live = journal.open_authenticated(proof, owner)
        self.assertEqual("committed", journal.append(live.capability("receipt"), evidence).status)
        frontier = _record("remote_reconcile_frontier", "checked-one", pair_id=pair, direction=direction,
                           frontier=1, checkpoint_digest="f" * 64)
        self.assertEqual("committed", journal.append(live.capability("reconcile"), frontier).status)
        if verb == "ask":
            with self.assertRaises(ProtocolRefusal):
                journal.prune_sender(Capability("L", pair, verb="prune"), direction, 1)
            evidence_source[allocated.record_id] = receipt
        self.assertEqual(1, journal.prune_sender(Capability("L", pair, verb="prune"), direction, 1)["floor"])
        journal.close_authenticated(live)
        transport.clear()
        fresh = CrossJournal(directory, **options)
        self.assertEqual({}, fresh._live_streams)
        self.assertEqual([], fresh.census)
        old = fresh.read_committed()
        retry = fresh.allocate_outbound(cap, "key", request, stamp)
        self.assertEqual(("existing", allocated.record_id, number, outcome),
                         (retry.status, retry.record_id, retry.delivery_seq, retry.outcome))
        self.assertEqual(old, fresh.read_committed())
        self.assertEqual([], fresh.census)
        self.assertEqual([], transport)
        with self.assertRaises(ProtocolRefusal):
            fresh.allocate_outbound(cap, "key", dict(request, envelope_fields=dict(request["envelope_fields"], note="changed")), stamp)
        send = Capability("L", pair, verb="send")
        other = dict(request, verb="send", envelope_fields=dict(request["envelope_fields"], sender_node="node-b", kind="message"))
        allocated_fresh = fresh.allocate_outbound(send, "key", other, stamp)
        self.assertEqual("committed", allocated_fresh.status)
        with self.assertRaises(ProtocolRefusal) as failure:
            fresh.prepare_local_output(send, None, allocated_fresh.record_id, "node-b")
        self.assertEqual("cross_stream_owner_invalid", failure.exception.code)
        self.assertEqual([], transport)
        self.assertEqual([2], [row["delivery_seq"] for row in fresh.read_committed().records
                               if row["kind"] == "remote_outbound"])

    def test_sender_replay_charges_a_source_that_must_remain_in_the_successor(self) -> None:
        from floati.cross_delivery import sender_receipt
        from floati.cross_reserve import DATA_BUDGET, admit, project

        pair = "6e5adfd61c91fc7e638d59c5355fde1e"
        journal = CrossJournal(self.seeded, authorize_local_send=lambda capability, sender: sender == "node-a",
                               retain_sender_source=lambda request: request["envelope_fields"]["note"] == "retain")
        _write(journal, "A", _activation(pair, "c3"))
        fields = {"v": 5, "kind": "message", "message_class": "normal", "pair_id": pair,
                  "direction": "S-to-T", "sender_node": "node-a", "recipient_node": "node-b",
                  "repo": None, "sha": None, "doc": None, "note": "retain", "reply_to": None}
        before = project(journal.read_committed().records)
        result = journal.allocate_outbound(Capability("L", pair, verb="send"), "retained",
                                           {"verb": "send", "envelope_fields": fields}, "2026-11-24T12:00:00.000Z")
        records = journal.read_committed().records
        source = next(row for row in records if row["id"] == result.record_id)
        frame = encode_frame(dict(source, seq=100000))
        priced = len(frame) + source["replay_price"]
        after = project(records)
        self.assertEqual((priced, 2), (after.data_bytes - before.data_bytes, after.data_slots - before.data_slots))
        self.assertEqual((priced, 2), (after.pair_data[pair].bytes, after.pair_data[pair].slots))
        for dimension, cap in (("boundary", Budget(priced, 2)), ("bytes", Budget(priced - 1, 2)),
                               ("slots", Budget(priced, 1))):
            for scope in ("global", "pair", "both"):
                global_cap = cap if scope != "pair" else DATA_BUDGET
                pair_cap = cap if scope != "global" else DATA_BUDGET
                with self.subTest(dimension=dimension, scope=scope), patch("floati.cross_reserve.PAIR_DATA_BUDGET", pair_cap):
                    if dimension == "boundary":
                        self.assertEqual("data", admit(before, source, len(encode_frame(source)), data=global_cap))
                    else:
                        with self.assertRaises(ProtocolRefusal) as caught:
                            admit(before, source, len(encode_frame(source)), data=global_cap)
                        self.assertEqual("cross_storage_cap", caught.exception.code)
        refused = {"refused": {"code": "cross_node_not_allowed"}}
        receipt, digest = sender_receipt(source, refused)
        row = _record("remote_peer_receipt", "receipt-retained", pair_id=pair, direction="S-to-T", delivery_seq=1,
                      envelope_id=source["envelope_id"], receipt_body=receipt, receipt_digest=digest)
        self.assertEqual("committed", _write(journal, "I", row).status)
        frontier = _record("remote_reconcile_frontier", "frontier-retained", pair_id=pair, direction="S-to-T",
                           frontier=1, checkpoint_digest="f" * 64)
        self.assertEqual("committed", journal.append(Capability("I", pair, verb="reconcile"), frontier).status)
        self.assertEqual(1, journal.prune_sender(Capability("L", pair, verb="prune"), "S-to-T", 1)["floor"])
        successor = journal.read_committed(require_settled=True).records
        resident = next(row for row in successor if row["id"] == source["id"])
        replay = next(row for row in successor if row["kind"] == "remote_sender_replay")
        other = project(row for row in successor if row not in (resident, replay))
        total = project(successor)
        actual = len(encode_frame(dict(resident, seq=100000))) + len(encode_frame(replay))
        self.assertEqual((actual, 2), (total.data_bytes - other.data_bytes, total.data_slots - other.data_slots))
        self.assertEqual((actual, 2), (total.pair_data[pair].bytes - other.pair_data[pair].bytes,
                                       total.pair_data[pair].slots - other.pair_data[pair].slots))
        fresh = CrossJournal(self.seeded, authorize_local_send=lambda capability, sender: sender == "node-a")
        retry = fresh.allocate_outbound(Capability("L", pair, verb="send"), "retained",
                                        {"verb": "send", "envelope_fields": fields}, "2026-11-24T12:00:00.000Z")
        self.assertEqual(("existing", result.record_id, 1, refused),
                         (retry.status, retry.record_id, retry.delivery_seq, retry.outcome))
        self.assertEqual([], fresh.census)


    def test_historical_unknown_sender_replay_grants_no_sequence_or_transport_effect(self) -> None:
        from floati.cross_delivery import sender_replay
        from floati.cross_journal import line_digest, parse_checkpoint

        pair = "6e5adfd61c91fc7e638d59c5355fde1e"
        stamp = "2026-11-24T12:00:00.000Z"
        journal = CrossJournal(self.seeded, authorize_local_send=lambda capability, sender: sender == "node-a")
        _write(journal, "A", _activation(pair, "c3"))
        cap = Capability("L", pair, verb="send")
        request = {"verb": "send", "envelope_fields": {
            "v": 5, "kind": "message", "message_class": "normal", "pair_id": pair, "direction": "S-to-T",
            "sender_node": "node-a", "recipient_node": "node-b", "repo": None, "sha": None, "doc": None,
            "note": "historical", "reply_to": None,
        }}
        original = journal.allocate_outbound(cap, "old", request, stamp)
        checkpoint_file = self.seeded / "journal.commit"
        checkpoint = parse_checkpoint(checkpoint_file.read_bytes())
        rows = journal.read_committed(require_settled=True).records
        source = rows[-1]
        self.assertEqual(original.record_id, source["id"])
        retired = dict(sender_replay(source, None), seq=source["seq"], prev=source["prev"])
        final = encode_frame(retired)
        prefix = b"".join(encode_frame(row) for row in rows[:-1]) + final

        # This fixture starts from a previously verified history whose outcome was lost.
        (self.seeded / checkpoint["journal_file"]).write_bytes(prefix)
        checkpoint.update(byte_length=len(prefix), head_sha256=line_digest(final), previous=None)
        frame = encode_frame(checkpoint)
        checkpoint_file.write_bytes(frame)
        (self.seeded / "journal.commit.seal").write_bytes(encode_frame({
            "format": "floati-cross-seal-v1", "checkpoint_sha256": hashlib.sha256(frame).hexdigest(),
        }))
        for _ in range(2):
            reader = CrossJournal(self.seeded, authorize_local_send=lambda capability, sender: sender == "node-a")
            self.assertEqual({}, reader._live_streams)
            before = harness.snapshot(self.seeded)
            retry = reader.allocate_outbound(cap, "old", request, stamp)
            self.assertEqual(("outcome_unknown", original.record_id, 1, None),
                             (retry.status, retry.record_id, retry.delivery_seq, retry.outcome))
            changed = dict(request, envelope_fields=dict(request["envelope_fields"], note="changed"))
            with self.assertRaises(ProtocolRefusal) as collision:
                reader.allocate_outbound(cap, "old", changed, stamp)
            self.assertEqual("cross_record_id_conflict", collision.exception.code)
            with self.assertRaises(ProtocolRefusal) as uncovered:
                reader.allocate_outbound(cap, "new-key", request, stamp)
            self.assertEqual("cross_delivery_history_lost", uncovered.exception.code)
            self.assertEqual([], reader.census)
            self.assertEqual(before, harness.snapshot(self.seeded))

    def test_r9_original_owner_cancels_only_before_controller_publication(self) -> None:
        from floati.cross_anchor import Anchor, Handoff

        class Store:
            value = None

            def __init__(self):
                self.transitions = []
                self.lose_cancel = False

            def read(self):
                return self.value

            def compare_and_set(self, expected, replacement):
                if expected != self.value:
                    return False
                self.transitions.append((expected, replacement))
                if self.lose_cancel and isinstance(expected, Handoff) and replacement == expected.prior:
                    return False
                self.value = replacement
                return True

        for apply_cancel in (False, True):
            with self.subTest(applied=apply_cancel):
                directory = self.base / f"r9v1-cancel-{apply_cancel}"
                store = Store()
                writer = CrossJournal(directory, anchor_store=store)
                writer.create(ENABLE_ONE)
                _write(writer, "A", _optin("enable_complete", None))
                for name in ("obsolete-a", "obsolete-b"):
                    _write(writer, "A", _opened(name))
                    _write(writer, "A", _ceremony_closed(name))
                earlier = harness.snapshot(directory)
                prior = store.value
                store.transitions.clear()
                store.lose_cancel = not apply_cancel
                committed_at = lambda operation, name: (operation == "rename" and name == "journal.commit"
                                                        and isinstance(store.value, Handoff))
                interrupted = CrossJournal(directory, anchor_store=store,
                                           storage=harness.TracingStorage(directory, fail_when=committed_at))
                with self.assertRaises((OSError, ProtocolRefusal)):
                    interrupted.compact()
                held = store.transitions[0][1]
                self.assertEqual([(prior, held), (held, prior)], store.transitions)
                self.assertEqual(prior if apply_cancel else held, store.value)
                harness.materialize(earlier, directory)
                fresh = CrossJournal(directory, anchor_store=store)
                self.assertEqual(not apply_cancel, fresh.read_committed().pending)
                fresh.maintain()
                self.assertIsInstance(store.value, Anchor)
                if apply_cancel:
                    self.assertEqual(prior, store.value)
                else:
                    self.assertNotEqual(prior.opening_sha256, store.value.opening_sha256)
                    self.assertEqual(held, store.transitions[-1][0])
                    harness.materialize(earlier, directory)
                    self.assertTrue(CrossJournal(directory, anchor_store=store).read_committed().pending)

    def test_r9_handoff_rejects_a_self_sealed_sibling_of_the_bound_opening(self) -> None:
        from floati.cross_anchor import Anchor, Handoff
        from floati.cross_journal import parse_checkpoint

        class Store:
            value = None
            lose_install = False

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if expected != self.value:
                    return False
                if self.lose_install and isinstance(expected, Handoff) and isinstance(successor, Anchor):
                    return False
                self.value = successor
                return True

        store = Store()
        directory = self.base / "r9-handoff"
        writer = CrossJournal(directory, anchor_store=store)
        writer.create(ENABLE_ONE)
        _write(writer, "A", _optin("enable_complete", None))
        for name in ("obsolete-a", "obsolete-b"):
            _write(writer, "A", _opened(name))
            _write(writer, "A", _ceremony_closed(name))
        earlier = harness.snapshot(directory)
        store.lose_install = True
        with self.assertRaises(ProtocolRefusal):
            writer.compact()
        self.assertIsInstance(store.value, Handoff)
        held = store.value
        authentic = harness.snapshot(directory)
        candidate = parse_checkpoint(authentic["journal.commit"])
        candidate.update(format="floati-cross-checkpoint-v3", quota={"mode": "charged", "day": "2026-11-25", "count": 1})
        raw = encode_frame(candidate)
        (directory / "journal.commit").write_bytes(raw)
        (directory / "journal.commit.seal").write_bytes(encode_frame({
            "format": "floati-cross-seal-v1", "checkpoint_sha256": hashlib.sha256(raw).hexdigest(),
        }))
        store.lose_install = False
        for _ in range(2):
            fresh = CrossJournal(directory, anchor_store=store)
            self.assertTrue(fresh.read_committed().pending)
            with self.assertRaises(ProtocolRefusal):
                fresh.maintain()
            self.assertEqual(held, store.value)
        harness.materialize(authentic, directory)
        CrossJournal(directory, anchor_store=store).maintain()
        self.assertEqual(hashlib.sha256(authentic["journal.commit"]).hexdigest(), store.value.opening_sha256)
        harness.materialize(earlier, directory)
        self.assertTrue(CrossJournal(directory, anchor_store=store).read_committed().pending)

    def _r9_attended_fixture(self, suffix, *, version=1, journal_type=CrossJournal, listen=()):
        from floati.cross_attended import approved_completion, approved_start

        class Store:
            value = None
            refuse_next = False
            refuse_h2 = False

            def __init__(self):
                self.updates = []

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                if self.refuse_next:
                    self.refuse_next = False
                    return False
                if self.refuse_h2 and getattr(successor, "trust", (None, None, 0))[2] == 2:
                    return False
                self.value = successor
                self.updates.append(successor)
                return True

        class Inventory(_OwnedEnableInventory):
            fault = None

            def read(self, item, start):
                if self.fault == item:
                    return ("safe_absent", None) if item == "sentinel" else ("disputed", None)
                return super().read(item, start)

        start = approved_start(ENABLE_ONE, 7, "a" * 32,
                               {"listen": list(listen), "root": "/fixture/harbor", "store": "keychain", "unit": "none"},
                               schema_version=version)
        store, inventory = Store(), Inventory()
        directory = self.base / suffix
        options = dict(anchor_store=store, enable_approval=lambda phase, body: body == start, enable_inventory=inventory)
        journal = journal_type(directory, **options)
        journal.create(ENABLE_ONE, enable_generation=7, attended=start)
        return directory, options, start, approved_completion(start)

    def _pair_v1_opening(self, journal, start, local, token, wire, pair, *, role="T"):
        cap = Capability("A", verb="pair_show" if role == "S" else "pair_join", ceremony_token=token)
        scope = dict(enable_id=start["enable_id"], enable_generation=start["enable_generation"], ceremony_token=token)
        opened = dict(scope, kind="cross_ceremony_opened", schema_version=1,
                      id=f"cross-ceremony-opened-{local}", ceremony_id=local)
        self.assertEqual("committed", journal.append(cap, opened).status)
        leaf = b"\x30\x03\x02\x01\x01"
        activation = dict(scope, schema_version=1, kind="cross_pair_active", id=f"cross-pair-active-{pair}",
                          opening_id=local, ceremony_id=wire, pair_id=pair, role=role, label="peer",
                          local_der_sha256=hashlib.sha256(b"the local handshake leaf").hexdigest(),
                          peer_der=leaf.hex(), peer_sha256=hashlib.sha256(leaf).hexdigest(),
                          peer_descriptor=dict(v=5, enable_id=ENABLE_OTHER, salt="a" * 32,
                                               label_hint="peer", lan=[], ssh_launcher=""),
                          policy=19, allowlist=[], wake_list=[], transcript_receipt_digest="b" * 64,
                          human_act=dict(channel="controlling_terminal", rendered_digest="c" * 64))
        return cap, activation

    def _ir_production_pair(self, suffix, *, listen=(), **adapters):
        directory, options, start, complete = self._r9_attended_fixture(
            suffix, version=2, journal_type=ProductionJournal, listen=listen)
        options.update({"prove_pair_material": lambda record, **_: True}, **adapters)
        writer = ProductionJournal(directory, **options)
        self.assertEqual("committed", writer.append(Capability("A", verb="enable"), complete).status)
        cap, activation = self._pair_v1_opening(writer, start, "1" * 32, "4" * 32, "2" * 32, "3" * 32)
        self.assertEqual("committed", writer.activate_pair(cap, activation, prepare=lambda _: None).status)
        return writer, options, activation

    def _lan_journal_pair(self, suffix, **adapters):
        from floati.cross_gate import GateImage, GateOwner

        writer, options, pair = self._ir_production_pair(suffix, **adapters)
        owner = GateOwner(writer.read_committed().journal_id, pair["enable_id"], pair["enable_generation"],
                          pair["ceremony_token"], pair["pair_id"])
        image = GateImage.empty(owner.journal_id).provision(owner)
        writer.storage.create("gate-state", image.encoded())
        return writer, options, pair, owner, image

    def _lan_open_proof(self, pair, counter, *, side="listener", owner="endpoint"):
        return _record("remote_session_opened", f"lan-open-{counter}", schema_version=1, pair_id=pair["pair_id"],
                       direction="S-to-T" if side == "listener" else "T-to-S", stream_id=f"{counter:032x}",
                       enable_id=pair["enable_id"], enable_generation=pair["enable_generation"],
                       local_side=side, peer_der_sha256=pair["peer_sha256"], gate_counter=counter, owner_id=owner)

    def _lan_allocate(self, writer, pair):
        cap = Capability("L", pair["pair_id"], verb="send")
        request = {"verb": "send", "envelope_fields": {
            "v": 5, "kind": "message", "message_class": "normal", "pair_id": pair["pair_id"],
            "direction": "T-to-S", "sender_node": "sender", "recipient_node": "peer", "reply_to": None,
            "repo": None, "sha": None, "doc": None, "note": "LAN local request",
        }}
        return cap, writer.allocate_outbound(cap, "lan-send", request, "2026-11-26T00:00:00.000Z")

    def test_lan_gate_checks_ownership_and_carries_directional_floors(self):
        import fcntl
        from floati.cross_gate import GateImage, GateOwner, replace_gate
        from floati.framing import decode_frames

        qualified = True
        writer, options, pair, owner, initial = self._lan_journal_pair(
            "lan-gate", prove_pair_material=lambda *_a, **_kw: qualified, authenticate_open=lambda *_: True,
            authorize_local_send=lambda *_: True, dial_prerequisites=lambda *_: True)
        cap, source = self._lan_allocate(writer, pair)
        dial = writer.begin_local_dial(cap, source.record_id, "sender")
        proof, = decode_frames(dial.attempt_frame)
        with self.assertRaises(ProtocolRefusal):
            writer.current_gate()
        with writer.gate_transaction() as gate:
            self.assertEqual(initial.encoded(), gate.image.encoded())
            self.assertEqual(owner, writer.current_gate().pairs[0].owner)
            with (writer.directory / "journal.lock").open("rb") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = harness.snapshot(writer.directory)
            with self.assertRaises(ProtocolRefusal):
                replace_gate(gate.storage, gate.image, GateImage.empty(owner.journal_id))
            self.assertEqual(before, harness.snapshot(writer.directory))
            used = initial.advance(owner, "S-to-T", "in_hwm", 9).advance(owner, "T-to-S", "out_next", 14)
            with self.assertRaises(ProtocolRefusal):
                replace_gate(writer.current_gate().storage, gate.image, used)
            replace_gate(gate.storage, gate.image, used)
        with self.assertRaises(ProtocolRefusal):
            gate.storage.read("gate-state")
        writer.open_authenticated(self._lan_open_proof(pair, 9), object())
        writer.open_authenticated(self._lan_open_proof(pair, 13, side="dialler", owner=proof["id"]), object(), dial=dial)
        addr = Capability("L", pair["pair_id"], verb="peer_address")
        for index in range(4):
            writer.append(addr, _record("cross_peer_address", f"lan-address-{index}", pair_id=pair["pair_id"],
                                       addresses=[f"192.0.2.{index + 1}:1"]))
        writer.compact(Capability("L", verb="compact"))
        writer = ProductionJournal(writer.directory, **options)
        with writer.gate_transaction() as gate:
            inbound, outbound = (gate.pairs[0].counters[direction] for direction in ("S-to-T", "T-to-S"))
            self.assertEqual((9, 9, 14, 14), (inbound.retained_max_session, inbound.carried_max_session,
                                            outbound.retained_out_next, outbound.carried_out_next))
            self.assertEqual(["192.0.2.4:1"], gate.pairs[0].peer_address["addresses"])
        foreign = GateImage.empty(owner.journal_id).provision(
            GateOwner(owner.journal_id, owner.enable_id, owner.enable_generation, "5" * 32, owner.pair_id))
        wrong_journal = writer.read_committed().journal_id
        crossed = GateImage.empty(wrong_journal).provision(
            GateOwner(wrong_journal, owner.enable_id, owner.enable_generation, owner.ceremony_token, owner.pair_id))
        for image in (initial, foreign, crossed):
            (writer.directory / "gate-state").write_bytes(image.encoded())
            before = harness.snapshot(writer.directory)
            with self.assertRaises(ProtocolRefusal), writer.gate_transaction():
                self.fail("unproved gate granted access")
            self.assertEqual(before, harness.snapshot(writer.directory))
        (writer.directory / "gate-state").write_bytes(used.encoded())
        qualified = False
        with self.assertRaises(ProtocolRefusal), writer.gate_transaction():
            self.fail("unproved pair material granted access")

    def test_lan_listener_registration_binds_the_attended_address_and_os_owner(self):
        witnesses = {}
        disposed = []
        writer, options, _, _, _ = self._lan_journal_pair(
            "lan-endpoint", listen=("127.0.0.1:0", "any:4382"),
            verify_endpoint=lambda record, owner: witnesses.get(owner, (None, None))[1]
            if witnesses.get(owner, (None, None))[0] == record else None)
        lifecycle = Capability("I", verb="endpoint_start")
        token = "5" * 32
        owner = object()
        binding = {"approved": {"host": "any", "port": 4382}, "bound": {"host": "::", "port": 4382},
                   "dual_stack": True, "pid": 50, "start_token": "launch-50", "socket_token": "6" * 32}
        body = dict(binding, id=f"cross-endpoint-{token}", kind="remote_endpoint_registered",
                    schema_version=1, enable_id=ENABLE_ONE, enable_generation=7)
        witnesses[owner] = body, "bound"
        before = harness.snapshot(writer.directory)
        self.assertTrue(writer.bind_allowed(binding["approved"]))
        self.assertFalse(writer.bind_allowed({"host": "any", "port": 4400}))
        self.assertFalse(writer.bind_allowed({"host": "0.0.0.0", "port": 4382}))
        for wrong in (dict(binding, dual_stack=False), dict(binding, bound={"host": "::", "port": 4383}),
                      dict(binding, approved={"host": "any", "port": 4400})):
            with self.assertRaises(ProtocolRefusal):
                writer.register_listener(lifecycle, token, wrong, owner=owner)
            self.assertEqual(before, harness.snapshot(writer.directory))
        with self.assertRaises(ProtocolRefusal):
            writer.append(lifecycle, body)
        self.assertEqual("committed", writer.register_listener(lifecycle, token, binding, owner=owner).status)
        self.assertTrue(writer.listener_current(token, binding, owner=owner))
        self.assertFalse(writer.listener_current(token, binding, owner=object()))
        self.assertFalse(writer.listener_current(token, binding, owner=owner, listening=True))
        witnesses[owner] = body, "listening"
        self.assertTrue(writer.listener_current(token, binding, owner=owner, listening=True))
        with self.assertRaises(ProtocolRefusal):
            writer.dispose_listener(lifecycle, token, binding, owner=owner, dispose=lambda: self.fail("live disposal"))
        witnesses[owner] = body, "bound"
        writer = ProductionJournal(writer.directory, **options)
        self.assertEqual("bound", writer.resolve_listener(lifecycle, token, binding, owner=owner))
        def dispose():
            disposed.append(owner)
            witnesses[owner] = body, "inert"
        self.assertEqual("inert", writer.dispose_listener(lifecycle, token, binding, owner=owner, dispose=dispose))
        self.assertEqual("inert", writer.dispose_listener(lifecycle, token, binding, owner=owner, dispose=dispose))
        self.assertEqual([owner], disposed)
        self.assertFalse(writer.listener_current(token, binding, owner=owner))
        self.assertEqual(body["id"], writer.read_committed().records[-1]["id"])
        lost_token, lost_owner = "7" * 32, object()
        concrete = dict(binding, approved={"host": "127.0.0.1", "port": 0},
                        bound={"host": "127.0.0.1", "port": 4383}, dual_stack=False, socket_token="8" * 32)
        lost_body = dict(body, **concrete)
        lost_body["id"] = f"cross-endpoint-{lost_token}"
        witnesses[lost_owner] = lost_body, "bound"
        fault = harness.TracingStorage(writer.directory, stop_after=lambda label: label == "rename:journal.commit")
        interrupted = ProductionJournal(writer.directory, storage=fault, **options)
        with self.assertRaises(harness.SimulatedCrash):
            interrupted.register_listener(lifecycle, lost_token, concrete, owner=lost_owner)
        writer = ProductionJournal(writer.directory, **options)
        shadow = harness.snapshot(writer.directory)
        self.assertEqual("pending", writer.resolve_listener(lifecycle, lost_token, concrete, owner=lost_owner))
        self.assertFalse(writer.listener_current(lost_token, concrete, owner=lost_owner))
        self.assertEqual(shadow, harness.snapshot(writer.directory))
        writer.maintain(Capability("M", verb="maintain"))
        self.assertEqual("bound", writer.resolve_listener(lifecycle, lost_token, concrete, owner=lost_owner))
        def dispose_lost():
            witnesses[lost_owner] = lost_body, "inert"
        self.assertEqual("inert", writer.dispose_listener(lifecycle, lost_token, concrete,
                                                          owner=lost_owner, dispose=dispose_lost))

    def test_lan_attempt_retirement_leaves_the_published_attempt_and_burn(self):
        from floati.cross_gate import replace_gate

        writer, options, pair, owner, image = self._lan_journal_pair(
            "lan-attempt", authorize_local_send=lambda *_: True, dial_prerequisites=lambda *_: True)
        cap, source = self._lan_allocate(writer, pair)
        dial = writer.begin_local_dial(cap, source.record_id, "sender")
        with self.assertRaises(ProtocolRefusal):
            writer.dial_current(dial)
        with writer.gate_transaction() as gate:
            self.assertTrue(writer.dial_current(dial))
            replace_gate(gate.storage, image, image.advance(owner, "T-to-S", "out_next", 2))
        before = harness.snapshot(writer.directory)
        writer.discard_local_dial(dial)
        writer.discard_local_dial(dial)
        with writer.gate_transaction():
            self.assertFalse(writer.dial_current(dial))
        restarted = ProductionJournal(writer.directory, **options)
        with restarted.gate_transaction():
            self.assertFalse(restarted.dial_current(dial))
        self.assertEqual(before, harness.snapshot(writer.directory))
        self.assertEqual("remote_transport_attempt", restarted.read_committed().records[-1]["kind"])

    def test_lan_existing_open_can_finish_only_with_settled_precut_evidence(self):
        from floati.cross_gate import replace_gate
        from floati.cross_anchor import OutstandingWrite

        known = set()
        def authenticate(proof, stream):
            return any(entry.owner.pair_id == proof["pair_id"] for entry in
                       writer.current_gate(existing_open=stream in known).pairs)
        writer, options, pair, owner, image = self._lan_journal_pair(
            "lan-open", authenticate_open=authenticate, authorize_local_send=lambda *_: True)
        with writer.gate_transaction() as gate:
            replace_gate(gate.storage, image, image.advance(owner, "S-to-T", "in_hwm", 5))
        stream = _OutputStream("T-to-S")
        opened = writer.open_authenticated(self._lan_open_proof(pair, 4), stream)
        known.add(stream)
        cap, source = self._lan_allocate(writer, pair)
        writer.prepare_local_output(cap, opened, source.record_id, "sender")
        row = next(row for row in writer.read_committed().records if row["id"] == source.record_id)
        receipt = _record("remote_peer_receipt", "lan-pre-stop-receipt", pair_id=pair["pair_id"],
                          direction=row["direction"], wire_seq=row["delivery_seq"], envelope_id=row["envelope_id"],
                          envelope_digest=row["envelope_digest"])
        stop = dict(_optin("disable_started", "local"), schema_version=1, enable_generation=pair["enable_generation"])
        self.assertEqual("committed", writer.append(Capability("L", verb="disable"), stop).status)
        store = options["anchor_store"]
        settled = store.value
        before = harness.snapshot(writer.directory)
        with self.assertRaises(ProtocolRefusal):
            writer.open_authenticated(self._lan_open_proof(pair, 5), _OutputStream("T-to-S"))
        with self.assertRaises(ProtocolRefusal), writer.gate_transaction():
            self.fail("a settled stop granted a gate transaction")
        for absent in (None, OutstandingWrite()):
            store.value = absent
            with self.assertRaises(ProtocolRefusal):
                writer.append(opened.capability("receipt"), receipt)
            self.assertEqual(before, harness.snapshot(writer.directory))
        store.value = settled
        for wrong in (dict(receipt, wire_seq=receipt["wire_seq"] + 1), dict(receipt, envelope_digest="a" * 64)):
            with self.assertRaises(ProtocolRefusal):
                writer.append(opened.capability("receipt"), wrong)
            self.assertEqual(before, harness.snapshot(writer.directory))
        self.assertEqual("committed", writer.append(opened.capability("receipt"), receipt).status)
        revoked = dict(_revoked(pair["pair_id"], "disable"), schema_version=1, idempotency_key=None,
                       enable_id=pair["enable_id"], enable_generation=pair["enable_generation"])
        self.assertEqual("committed", writer.append(Capability("L", verb="disable"), revoked).status)
        with self.assertRaises(ProtocolRefusal):
            writer.append(opened.capability("receipt"), receipt)

    def test_ir1_missing_providers_cannot_read_or_write_with_admitting_authority(self):
        writer, options, pair = self._ir_production_pair("ir1-provider-loss")
        self.assertEqual("published", writer.read_committed(require_settled=True).state)
        before = harness.snapshot(writer.directory)
        for missing in ("anchor_store", "enable_approval"):
            with self.subTest(missing=missing):
                isolated = ProductionJournal(writer.directory, **{**options, missing: None})
                self.assertTrue(isolated.read_committed().pending)
                with self.assertRaises(ProtocolRefusal):
                    isolated.read_committed(require_settled=True)
                self.assertEqual("disk_only", isolated.inspect_disk().state)
                self.assertIn(pair["id"], {row["id"] for row in isolated.inspect_disk().records})
        fresh = self.base / "ir1-no-providers"
        with self.assertRaises(ProtocolRefusal):
            ProductionJournal(fresh).create(ENABLE_ONE)
        self.assertFalse(fresh.exists())
        self.assertEqual(before, harness.snapshot(writer.directory))
        stop = dict(_optin("disable_started", "local"), schema_version=1, enable_generation=pair["enable_generation"])
        disable = Capability("L", verb="disable")
        self.assertEqual("committed", writer.append(disable, stop).status)
        self.assertEqual("published", writer.read_committed(require_settled=True).state)
        options["anchor_store"].value = None
        stopped = harness.snapshot(writer.directory)
        self.assertTrue(writer.read_committed().pending)
        self.assertEqual("pending", writer.resolve(disable, stop))
        self.assertEqual(stopped, harness.snapshot(writer.directory))

    def test_ir2_generic_sender_rows_require_the_scoped_local_entry_points(self):
        writer, _, pair = self._ir_production_pair("ir2-generic-sender", authorize_local_send=lambda *_: False)
        principal = Capability("L", pair["pair_id"], verb="send")
        before = harness.snapshot(writer.directory)
        for kind in ("remote_outbound", "remote_attempt", "remote_transport_attempt"):
            body = _record(kind, f"ir2-{kind}", pair_id=pair["pair_id"], outbound_id="missing")
            with self.subTest(kind=kind), self.assertRaises(ProtocolRefusal):
                writer.append(principal, body)
            with self.assertRaises(ProtocolRefusal):
                writer.resolve(principal, body)
            self.assertEqual(before, harness.snapshot(writer.directory))

    def test_ir3_dispatch_deposits_only_into_the_original_live_stream_queue(self):
        writer, _, pair = self._ir_production_pair("ir3-two-queues", authenticate_open=lambda proof, stream: True,
                                                   authorize_local_send=lambda cap, sender: sender == "sender")
        cap = Capability("L", pair["pair_id"], verb="send")
        request = {"verb": "send", "envelope_fields": {
            "v": 5, "kind": "message", "message_class": "normal", "pair_id": pair["pair_id"],
            "direction": "T-to-S", "sender_node": "sender", "recipient_node": "peer",
            "reply_to": None, "repo": None, "sha": None, "doc": None, "note": "owned queue",
        }}
        source = writer.allocate_outbound(cap, "ir3", request, "2026-11-25T00:00:00.000Z")
        self.assertNotIn("enqueue", inspect.signature(writer.deposit_local_output).parameters)
        owners = [_OutputStream("T-to-S"), _OutputStream("S-to-T")]
        live = []
        for i, owner in enumerate(owners, 1):
            proof = _record("remote_session_opened", f"ir3-open-{i}", schema_version=1, pair_id=pair["pair_id"],
                            direction="S-to-T", stream_id=str(i) * 32, enable_id=pair["enable_id"],
                            enable_generation=pair["enable_generation"], local_side="listener",
                            peer_der_sha256=pair["peer_sha256"], gate_counter=i, owner_id=f"endpoint-{i}")
            live.append(writer.open_authenticated(proof, owner))
        before = harness.snapshot(writer.directory)
        for principal, opened, record_id, sender in (
            (Capability("L", pair["pair_id"], verb="ask"), live[0], source.record_id, "sender"),
            (cap, live[0], "missing-source", "sender"),
            (cap, live[1], source.record_id, "sender"),
            (cap, live[0], source.record_id, "different-sender"),
        ):
            with self.subTest(principal=principal, record_id=record_id, sender=sender), self.assertRaises(ProtocolRefusal):
                writer.prepare_local_output(principal, opened, record_id, sender)
            self.assertEqual(before, harness.snapshot(writer.directory))
        dispatch = writer.prepare_local_output(cap, live[0], source.record_id, "sender")
        with self.assertRaises(TypeError):
            writer.deposit_local_output(dispatch, owners[1].enqueue_local_output)
        self.assertTrue(writer.deposit_local_output(dispatch))
        self.assertFalse(writer.deposit_local_output(dispatch))
        self.assertEqual([1, 0], [owner.queue.qsize() for owner in owners])

    def test_ir6_public_auxiliary_writers_require_the_exact_verb(self):
        writer, _, pair = self._ir_production_pair("ir6-auxiliary")
        before = harness.snapshot(writer.directory)
        for call in (lambda: writer.maintain(Capability("M", verb="hold_accounting")),
                     lambda: writer.compact(Capability("L", pair["pair_id"], verb="unpair")),
                     lambda: writer.provision_reserve_file(64)):
            with self.assertRaises(ProtocolRefusal) as caught:
                call()
            self.assertEqual("cross_capability_refused", caught.exception.code)
            self.assertEqual(before, harness.snapshot(writer.directory))

    def test_ir6_crash_closure_requires_the_exact_abandonment_lease(self):
        from contextlib import contextmanager

        writer, options, pair = self._ir_production_pair("ir6-lease")
        start = next(row for row in writer.read_committed().records if row.get("phase") == "enable_started")
        _, candidate = self._pair_v1_opening(writer, start, "5" * 32, "6" * 32, "7" * 32, "8" * 32)
        opening = candidate["opening_id"]
        scope = (candidate["enable_id"], candidate["enable_generation"], opening, candidate["ceremony_token"])
        proof = None
        held = []

        @contextmanager
        def exclusion(identifier):
            self.assertIn(identifier, {opening, pair["opening_id"]})
            held.append(identifier)
            try:
                yield proof
            finally:
                self.assertEqual(identifier, held.pop())

        class MeasuredStorage(Storage):
            def append(self, name, data):
                self.assert_exclusion()
                return super().append(name, data)

        storage = MeasuredStorage(writer.directory)
        storage.assert_exclusion = lambda: self.assertEqual([opening], held)
        maintenance = Capability("M", verb="maintain")
        crash = dict(zip(("enable_id", "enable_generation", "ceremony_id", "ceremony_token"), scope),
                     kind="cross_ceremony_closed", schema_version=2, id=f"cross-ceremony-closed-{opening}", cause="crash")
        before = harness.snapshot(writer.directory)
        with self.assertRaises(ProtocolRefusal):
            writer.append(maintenance, crash)
        writer = ProductionJournal(writer.directory, storage=storage, abandoned_ceremony=exclusion, **options)
        for proof in (None, (*scope[:3], "0" * 32)):
            with self.assertRaises(ProtocolRefusal):
                writer.close_abandoned_ceremony(maintenance, opening)
            self.assertEqual(before, harness.snapshot(writer.directory))
        proof = (pair["enable_id"], pair["enable_generation"], pair["opening_id"], pair["ceremony_token"])
        with self.assertRaises(ProtocolRefusal):
            writer.close_abandoned_ceremony(maintenance, pair["opening_id"])
        self.assertEqual(before, harness.snapshot(writer.directory))
        proof = scope
        self.assertEqual("committed", writer.close_abandoned_ceremony(maintenance, opening).status)
        self.assertEqual("crash", writer.read_committed().records[-1]["cause"])

    def test_ir7_reconstructed_root_denial_requires_confirmed_anchor_absence(self):
        from floati.cross_anchor import OutstandingWrite

        writer, options, pair = self._ir_production_pair("ir7-absent-anchor")
        store = options["anchor_store"]
        original = store.value
        before = harness.snapshot(writer.directory)
        maintenance = Capability("M", verb="maintain")
        candidate = dict(_optin("disable_started", "anchor_absent_reconstructed"), schema_version=1,
                         enable_generation=pair["enable_generation"])
        with self.assertRaises(ProtocolRefusal):
            writer.append(maintenance, candidate)
        for value in (original, OutstandingWrite()):
            store.value = value
            with self.assertRaises(ProtocolRefusal):
                writer.reconstruct_disable(maintenance)
            self.assertEqual(value, store.value)
            self.assertEqual(before, harness.snapshot(writer.directory))
        store.value = None
        self.assertEqual("committed", writer.reconstruct_disable(maintenance).status)
        self.assertIsNone(store.value)
        self.assertTrue(writer.read_committed().pending)
        self.assertEqual("anchor_absent_reconstructed", writer.inspect_disk().records[-1]["cause"])

    def test_ir9_w0_preserves_oversized_and_multiappend_suffixes(self):
        for index, tail in enumerate((b"x" * 65537, b"{}\n{}\n")):
            with self.subTest(index=index):
                writer, _, _ = self._ir_production_pair(f"ir9-suffix-{index}")
                checkpoint, _ = writer._load()
                with (writer.directory / checkpoint["journal_file"]).open("ab") as file:
                    file.write(tail)
                before = harness.snapshot(writer.directory)
                with self.assertRaises(FloatiError):
                    writer.maintain(Capability("M", verb="maintain"))
                self.assertEqual(before, harness.snapshot(writer.directory))

    def test_ir10_new_enable_frames_carry_the_assigned_trust_fields(self):
        from floati.cross_coordinates import semantic
        from floati.cross_journal import verify_prefix
        from floati.cross_records import priced_frame
        from floati.errors import IntegrityFailure

        writer, _, _ = self._ir_production_pair("ir10-assigned-trust")
        view = writer.read_committed()
        steps = [row for row in view.records
                 if row.get("phase") in ("enable_started", "enable_complete") or row["kind"] == "cross_pair_active"]
        self.assertEqual([1, 2, 3], [row.get("trust_count") for row in steps])
        self.assertEqual([row["trust_head"] for row in steps[:-1]], [row["prev_trust_digest"] for row in steps[1:]])
        activation = steps[-1]
        crossed = priced_frame(semantic(activation), activation["seq"], activation["prev"], trust=("0" * 64, 3))
        with self.assertRaises(IntegrityFailure):
            verify_prefix(b"".join(map(encode_frame, view.records[:-1])) + crossed, view.journal_id)

    def test_ir14_fresh_root_needs_an_explicit_migration_slot_observation(self):
        class EmptySlot:
            def read(self):
                return None

        directory = self.base / "ir14-no-migration-adapter"
        root = dict(anchor_store=EmptySlot(), fresh_root=lambda: True, missing_exclusion=nullcontext)
        self.assertTrue(ProductionJournal(directory, **root).read_committed().pending)
        self.assertEqual("absent", ProductionJournal(directory, quota_store=EmptySlot(), **root).read_committed().state)
        self.assertFalse(directory.exists())

    def test_ir19_activation_keeps_trust_counts_for_the_full_shutdown(self):
        self._ir19_counter_boundary(3, "blocked-widening")

    def test_ir19_exhausted_history_can_still_install_independent_denial(self):
        self._ir19_counter_boundary(0, "independent-denial")

    def _ir19_counter_boundary(self, remaining, outcome):
        from floati.cross_coordinates import MAX_INTEGER
        from floati.cross_journal import line_digest
        from floati.cross_anchor import Narrowing

        directory, options, start, complete = self._r9_attended_fixture(
            f"ir19-{outcome}", version=2, journal_type=ProductionJournal)
        writer = ProductionJournal(directory, prove_pair_material=lambda *_a, **_kw: True, **options)
        self.assertEqual("committed", writer.append(Capability("A", verb="enable"), complete).status)
        cap, activation = self._pair_v1_opening(writer, start, "1" * 32, "4" * 32, "2" * 32, "3" * 32)
        checkpoint, raw = writer._load()
        row = dict(id="ir19-near-counter-limit", kind="cross_trust_checkpoint", schema_version=1,
                   seq=checkpoint["through_seq"] + 1, prev=checkpoint["head_sha256"],
                   enable_id=activation["enable_id"], enable_generation=activation["enable_generation"],
                   trust_count=MAX_INTEGER - remaining, trust_head="d" * 64)
        line = encode_frame(row)
        writer.storage.append(checkpoint["journal_file"], line)
        next_checkpoint = dict(writer._carry_opening(checkpoint, raw), through_seq=row["seq"],
                               head_sha256=line_digest(line), byte_length=checkpoint["byte_length"] + len(line),
                               previous=writer._previous(checkpoint, raw))
        sealed = writer._commit_checkpoint(next_checkpoint)
        records, _ = writer._verified(next_checkpoint)
        options["anchor_store"].value = writer._anchor_coordinate(next_checkpoint, sealed, records)
        before = harness.snapshot(directory)
        if outcome == "independent-denial":
            stop = dict(_optin("disable_started", "local"), schema_version=1,
                        enable_generation=activation["enable_generation"])
            result = writer.append(Capability("L", verb="disable"), stop)
            self.assertEqual(("deferred", "cross_trust_counter_exhausted", "confirmed"),
                             (result.status, result.code, result.independent_denial))
            denial = options["anchor_store"].value
            self.assertIsInstance(denial, Narrowing)
            self.assertEqual(stop, denial.body())
            self.assertTrue(ProductionJournal(directory, **options).read_committed().pending)
            self.assertEqual(before[next_checkpoint["journal_file"]],
                             (directory / next_checkpoint["journal_file"]).read_bytes())
            return
        with self.assertRaises(ProtocolRefusal) as caught:
            writer.activate_pair(cap, activation, prepare=lambda _: self.fail("prepared without shutdown count reserve"))
        self.assertEqual("cross_trust_counter_exhausted", caught.exception.code)
        self.assertEqual(before, harness.snapshot(directory))

    def test_ir8_ir24_priced_narrowing_survives_two_torn_appends(self):
        from floati.cross_anchor import Narrowing
        from floati.cross_coordinates import semantic

        for phase in ("revoke", "disable"):
            with self.subTest(phase=phase):
                writer, options, pair = self._ir_production_pair(f"ir24-{phase}")
                if phase == "disable":
                    record = dict(_optin("disable_started", "local"), schema_version=1,
                                  enable_generation=pair["enable_generation"])
                    capability = Capability("L", verb="disable")
                else:
                    record = dict(_revoked(pair["pair_id"], "local"), schema_version=1, idempotency_key="ir24-key",
                                  enable_id=pair["enable_id"], enable_generation=pair["enable_generation"])
                    capability = Capability("L", pair["pair_id"], verb="unpair")
                attempted = []

                class LostAppend(Storage):
                    def append(self, name, data):
                        from floati.framing import decode_frames
                        row, = decode_frames(data)
                        if semantic(row) == record:
                            tail = data if attempted else data[:-1]
                            super().append(name, tail)
                            attempted.append(row)
                            raise OSError(errno.EIO, "interrupted after appending the priced row")
                        return super().append(name, data)

                interrupted = ProductionJournal(writer.directory, storage=LostAppend(writer.directory), **options)
                self.assertNotEqual("committed", interrupted.append(capability, record).status)
                self.assertIsInstance(options["anchor_store"].value, Narrowing)
                self.assertEqual(record, options["anchor_store"].value.body())
                with self.assertRaises(ProtocolRefusal):
                    interrupted.maintain(Capability("M", verb="maintain"))
                restarted = ProductionJournal(writer.directory, **options)
                restarted.maintain(Capability("M", verb="maintain"))
                view = restarted.read_committed(require_settled=True)
                self.assertEqual(2, len(attempted))
                self.assertEqual(attempted[0]["trust_head"], attempted[1]["trust_head"])
                self.assertEqual(2, sum(row["kind"] == "cross_journal_tail_repaired" for row in view.records))
                self.assertEqual(record, semantic(view.records[-1]))
                checkpoint, raw = restarted._load()
                self.assertEqual(restarted._anchor_coordinate(checkpoint, raw, view.records), options["anchor_store"].value)

    def test_pair_v1_consumes_local_opening_and_serializes_live_handoff(self) -> None:
        import fcntl

        directory, options, start, complete = self._r9_attended_fixture("pair-v1-local-and-wire")
        effects = []

        def locked():
            with (directory / "journal.lock").open("rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def material(record, *, phase):
            locked()
            effects.append((phase, record["id"]))
            return record == activation

        self.assertTrue(hasattr(CrossJournal, "activate_pair"))
        writer = CrossJournal(directory, prove_pair_material=material, **options)
        self.assertEqual("committed", writer.append(Capability("A", verb="enable"), complete).status)
        local, wire, pair = "1" * 32, "2" * 32, "3" * 32
        tight = CrossJournal(directory, prove_pair_material=material, budget=Budget(100000, 100), **options)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._pair_v1_opening(tight, start, local, "4" * 32, wire, pair)
        self.assertEqual("cross_review_capacity", caught.exception.code)
        cap, activation = self._pair_v1_opening(writer, start, local, "4" * 32, wire, pair)
        sibling, _ = self._pair_v1_opening(writer, start, wire, "5" * 32, "6" * 32, "7" * 32)
        with self.assertRaises(ProtocolRefusal):
            writer.append(cap, activation)
        writer.preflight_pair_activation(cap, activation)
        self.assertEqual([], effects)
        with self.assertRaises(ProtocolRefusal):
            writer.activate_pair(cap, dict(activation, opening_id=wire), prepare=lambda _: self.fail("prepared wrong opening"))
        with self.assertRaises(ProtocolRefusal):
            writer.activate_pair(cap, dict(activation, extra=True), prepare=lambda _: self.fail("prepared open shape"))
        self.assertEqual("committed", writer.activate_pair(cap, activation, prepare=lambda _: locked()).status)
        self.assertEqual([("publish", activation["id"]), ("anchor", activation["id"])], effects)
        from floati.cross_reserve import project
        self.assertEqual({wire}, project(writer.read_committed().records).open_ceremonies)
        self.assertEqual("queued", writer.with_pair_activation(cap, activation, enqueue=lambda: (locked(), "queued")[1]))
        with self.assertRaises(ProtocolRefusal):
            writer.with_pair_activation(sibling, activation, enqueue=lambda: self.fail("queued wrong token"))

    def test_pair_legacy_history_remains_readable_without_live_authority(self) -> None:
        directory, options, start, complete = self._r9_attended_fixture("pair-legacy-published-fence")
        legacy = CrossJournal(directory, **options)
        self.assertEqual("committed", legacy.append(Capability("A", verb="enable"), complete).status)
        pair, ceremony = "3" * 32, "4" * 32
        self.assertEqual("committed", _write(legacy, "A", _opened(ceremony)).status)
        self.assertEqual("committed", _write(legacy, "A", _activation(pair, ceremony)).status)
        live = CrossJournal(directory, prove_pair_material=lambda *args, **kwargs: True,
                            authenticate_open=lambda body, stream: self.fail("authenticated a legacy pair"), **options)
        self.assertEqual("legacy_fenced", live.lookup_pair(pair)["state"])
        proof = dict(id="remote-session-legacy", kind="remote_session_opened", schema_version=1, pair_id=pair,
                     direction="S-to-T", stream_id="6" * 32, enable_id=start["enable_id"],
                     enable_generation=start["enable_generation"], local_side="listener", peer_der_sha256="7" * 64,
                     gate_counter=1, owner_id="owner")
        before = harness.snapshot(directory)
        with self.assertRaises(ProtocolRefusal):
            live.open_authenticated(proof, object())
        self.assertEqual(before, harness.snapshot(directory))

    def test_pair_v1_requires_the_local_handshake_leaf_before_preparing(self) -> None:
        directory, options, start, complete = self._r9_attended_fixture("pair-local-leaf-proof")
        prepared = []
        local_hash = hashlib.sha256(b"the local handshake leaf").hexdigest()
        writer = CrossJournal(directory, prove_pair_material=lambda row, **_: row["local_der_sha256"] == local_hash,
                              **options)
        self.assertEqual("committed", writer.append(Capability("A", verb="enable"), complete).status)
        cap, activation = self._pair_v1_opening(writer, start, "1" * 32, "4" * 32, "2" * 32, "3" * 32)
        unbound = {key: value for key, value in activation.items() if key != "local_der_sha256"}
        for candidate in (unbound, dict(activation, local_der_sha256=activation["peer_sha256"])):
            with self.assertRaises(ProtocolRefusal):
                writer.activate_pair(cap, candidate, prepare=lambda _: self.fail("prepared without a distinct local leaf"))
        self.assertEqual("committed", writer.activate_pair(cap, dict(activation, local_der_sha256=local_hash),
                                                           prepare=prepared.append).status)
        self.assertEqual([local_hash], [row["local_der_sha256"] for row in prepared])

    def test_pair_v1_recovers_once_and_retains_closed_identity_across_compaction(self) -> None:
        directory, options, start, complete = self._r9_attended_fixture("pair-v1-recovery-and-retirement")
        allowed = True
        phases = []

        def material(record, *, phase):
            phases.append(phase)
            return allowed and record == activation

        self.assertTrue(hasattr(CrossJournal, "lookup_pair"))
        store = options["anchor_store"]
        writer = CrossJournal(directory, prove_pair_material=material, **options)
        self.assertEqual("committed", writer.append(Capability("A", verb="enable"), complete).status)
        cap, activation = self._pair_v1_opening(writer, start, "1" * 32, "4" * 32, "2" * 32, "3" * 32, role="S")
        pair = activation["pair_id"]
        store.refuse_next = True
        self.assertEqual("committed_unpublished", writer.activate_pair(cap, activation, prepare=lambda _: None).status)
        self.assertEqual("pending", writer.lookup_pair(pair)["state"])
        before = list(store.updates)
        restarted = CrossJournal(directory, prove_pair_material=material, **options)
        with self.assertRaises(ProtocolRefusal):
            restarted.activate_pair(cap, activation, prepare=lambda _: self.fail("A tried to finish a sealed activation"))
        self.assertEqual(before, store.updates)
        allowed = False
        with self.assertRaises(ProtocolRefusal):
            restarted.maintain(Capability("M", verb="maintain"))
        self.assertEqual(before, store.updates)
        allowed = True
        restarted.maintain(Capability("M", verb="maintain"))
        restarted.maintain(Capability("M", verb="maintain"))
        self.assertEqual(len(before) + 1, len(store.updates))
        self.assertEqual(["publish", "anchor", "recover", "recover"], phases)
        historical = restarted.lookup_pair(pair)
        self.assertEqual(activation, historical["activation"])
        self.assertIn(historical["review"], restarted.attended_reviews())
        with self.assertRaises(ProtocolRefusal):
            restarted.record_pair_seen(cap, activation, verify_testimony=lambda _: False)
        seen = restarted.record_pair_seen(cap, activation, verify_testimony=lambda _: True)
        self.assertEqual("committed", seen.status)
        old = harness.snapshot(directory)
        revoked = dict(kind="cross_pair_revoked", schema_version=1, id=f"cross-pair-revoked-{pair}",
                       pair_id=pair, cause="local", idempotency_key="fixture-key", enable_id=start["enable_id"],
                       enable_generation=start["enable_generation"])
        before = list(store.updates)
        with self.assertRaises(ProtocolRefusal):
            restarted.append(Capability("L", pair, "unpair"), dict(revoked, enable_generation=1))
        self.assertEqual(before, store.updates)
        self.assertEqual("committed", restarted.append(Capability("L", pair, "unpair"), revoked).status)
        for number in (5, 6, 7):
            token, local = str(number) * 32, f"{number + 15:x}".zfill(32)
            close_cap, _ = self._pair_v1_opening(restarted, start, local, token, "8" * 32, "9" * 32)
            self.assertEqual("committed", restarted.append(close_cap, dict(
                schema_version=2, kind="cross_ceremony_closed", id=f"cross-ceremony-closed-{local}",
                enable_id=start["enable_id"], enable_generation=start["enable_generation"], ceremony_id=local,
                ceremony_token=token, cause="abort")).status)
        retired = restarted.lookup_pair(pair)
        self.assertEqual(revoked, retired["revocation"])
        restarted.compact("L", "explicit")
        image = harness.snapshot(directory)
        harness.materialize(old, directory)
        self.assertEqual("pending", CrossJournal(directory, prove_pair_material=material, **options).lookup_pair(pair)["state"])
        harness.materialize(image, directory)
        restarted = CrossJournal(directory, prove_pair_material=material, **options)
        self.assertEqual(retired, restarted.lookup_pair(pair))
        with self.assertRaises(ProtocolRefusal):
            restarted.with_pair_activation(cap, activation, enqueue=lambda: self.fail("queued retired pair"))
        with self.assertRaises(ProtocolRefusal):
            restarted.record_pair_seen(cap, activation, verify_testimony=lambda _: self.fail("observed retired pair"))
        fresh, conflict = self._pair_v1_opening(restarted, start, "a" * 32, "b" * 32, "c" * 32, pair)
        with self.assertRaises(ProtocolRefusal):
            restarted.activate_pair(fresh, conflict, prepare=lambda _: self.fail("prepared retired pair"))

    def test_optin_ballast_callback_resumes_physical_provisioning_at_each_cut(self) -> None:
        from floati.cross_journal import BALLAST, BALLAST_INVENTORY

        class Inventory(_OwnedEnableInventory):
            def read(self, item, start):
                if item == "ballast":
                    return self.writer.inspect_enable_ballast(start, size=4096)
                return super().read(item, start)

            def create(self, item, start):
                self_test.assertEqual("ballast", item)
                self.writer.provision_enable_ballast(start, size=4096)

        class Cut(Storage):
            def create(self, name, data):
                if ("partial-create", name) == cut:
                    super().create(name, data[:17])
                    raise harness.SimulatedCrash()
                super().create(name, data)

            def boundary(self, operation, name):
                synced_names = (operation, name) == ("sync_directory", ".") and (self.directory / BALLAST_INVENTORY).is_file()
                if (operation, name) == cut or (synced_names and cut == ("sync_directory", BALLAST_INVENTORY)):
                    raise harness.SimulatedCrash()

        self_test = self
        self.assertTrue(hasattr(CrossJournal, "provision_enable_ballast"))
        enable = Capability("A", verb="enable")
        for cut in (("partial-create", BALLAST), ("create", BALLAST), ("sync", BALLAST),
                    ("partial-create", BALLAST_INVENTORY), ("create", BALLAST_INVENTORY),
                    ("sync", BALLAST_INVENTORY), ("sync_directory", BALLAST_INVENTORY)):
            with self.subTest(cut=cut):
                directory, options, start, complete = self._r9_attended_fixture("ballast-" + "-".join(cut))
                inventory = Inventory()
                options["enable_inventory"] = inventory
                writer = CrossJournal(directory, storage=Cut(directory), **options)
                inventory.writer = writer
                with self.assertRaises(ProtocolRefusal):
                    writer.provision_enable_ballast(start, size=4096)
                self.assertFalse((directory / BALLAST).exists())
                with self.assertRaises(harness.SimulatedCrash):
                    writer.append(enable, complete)
                self.assertTrue(writer.read_committed().pending)
                self.assertFalse(any(row.get("phase") == "enable_complete" for row in writer.read_committed().records))
                inode = (directory / BALLAST).stat().st_ino
                writer = inventory.writer = CrossJournal(directory, **options)
                self.assertEqual("committed", writer.append(enable, complete).status)
                self.assertEqual(4096, (directory / BALLAST).stat().st_size)
                self.assertEqual(inode, (directory / BALLAST).stat().st_ino)
                self.assertEqual("owned", writer.inspect_enable_ballast(start, size=4096)[0])
                rows = writer.read_committed(require_settled=True).records
                self.assertEqual(["intent", "created"], [row["phase"] for row in rows if row.get("item") == "ballast"])

        directory, options, start, complete = self._r9_attended_fixture("ballast-sidecar-power-cut")
        inventory = Inventory()
        options["enable_inventory"] = inventory
        storage = harness.TracingStorage(directory, stop_after=lambda label: label == f"create:{BALLAST_INVENTORY}")
        inventory.writer = CrossJournal(directory, storage=storage, **options)
        with self.assertRaises(harness.SimulatedCrash):
            inventory.writer.append(enable, complete)
        # A crash can keep the sidecar name independently of other unsynced
        # changes. Its underlying ballast name must already be durable.
        self.assertIn(BALLAST, storage.model["durable"])
        self.assertNotIn(BALLAST_INVENTORY, storage.model["durable"])
        image = harness.power_loss_images(directory, storage.model)["names=durable,bytes=durable"]
        harness.materialize(image, directory)
        inventory.writer = CrossJournal(directory, **options)
        self.assertEqual("committed", inventory.writer.append(enable, complete).status)

    def test_lock_held_disable_measures_partial_and_complete_inventory_before_receipt(self) -> None:
        import fcntl

        class Inventory(_OwnedEnableInventory):
            def read(self, item, start):
                if item == "ballast" and partial:
                    return "safe_absent", None
                return super().read(item, start)

            def create(self, item, start):
                raise harness.SimulatedCrash()

        class Effects:
            disputed = True

            def inventory(self, start, records):
                return {pair_item: "d" * 64} if not partial else {}

            def read(self, item, start, proof):
                self.assert_locked()
                if item == "configuration" and self.disputed:
                    return "foreign", None
                return ("owned", proof) if item in physical else ("safe_absent", None)

            def remove(self, item, start, proof):
                self.assert_locked()
                self_test.assertEqual("d" * 64 if item == pair_item else inventory.expected(item, start), proof)
                physical.pop(item).unlink()

            def checks(self, start):
                self.assert_locked()
                return {key: True for key in ("unit_absent", "unit_not_loadable", "no_listener", "no_live_endpoint")}

            def assert_locked(self):
                with (directory / "journal.lock").open("rb") as lock:
                    with self_test.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        self_test = self
        self.assertTrue(hasattr(CrossJournal, "teardown_disable"))
        disable = Capability("L", verb="disable")
        pair = "9" * 32
        pair_item = f"pair:{pair}"
        for partial in (True, False):
            with self.subTest(partial=partial):
                directory, options, start, complete = self._r9_attended_fixture(f"measured-disable-{partial}")
                options["enable_inventory"] = inventory = Inventory()
                writer = CrossJournal(directory, **options)
                if partial:
                    with self.assertRaises(harness.SimulatedCrash):
                        writer.append(Capability("A", verb="enable"), complete)
                else:
                    self.assertEqual("committed", writer.append(Capability("A", verb="enable"), complete).status)
                    self.assertEqual("committed", _write(writer, "A", _opened("a" * 32)).status)
                    self.assertEqual("committed", _write(writer, "A", _activation(pair, "a" * 32)).status)
                    self.assertEqual("committed", _write(writer, "A", _opened("b" * 32)).status)
                physical = {item: directory / ("test-" + item) for item in ("configuration", "unit")}
                if not partial:
                    physical[pair_item] = directory / "test-pair"
                for path in physical.values():
                    path.touch()
                effects = Effects()
                with self.assertRaises(ProtocolRefusal):
                    writer.teardown_disable(disable, dict(start, token="b" * 32), effects=effects)
                self.assertEqual("interrupted", writer.teardown_disable(disable, start, effects=effects)["state"])
                self.assertTrue(physical["configuration"].is_file())
                view = writer.read_committed()
                if partial:
                    self.assertTrue(view.pending)
                self.assertFalse(any(row["kind"] == "cross_teardown_receipt" for row in view.records))
                self.assertTrue(any(row.get("item") == "configuration" for row in view.records))
                effects.disputed = False
                restarted = CrossJournal(directory, **options)
                self.assertEqual("complete", restarted.teardown_disable(disable, start, effects=effects)["state"])
                self.assertEqual({}, physical)
                self.assertIsNone(options["anchor_store"].value)
                rows = restarted.read_committed().records
                receipt, = (row for row in rows if row["kind"] == "cross_teardown_receipt")
                self.assertIn({"item": "configuration", "class": "confirmed_absent", "cause": "removed_and_verified"},
                              receipt["items"])
                self.assertIn({"item": "anchor", "class": "confirmed_absent", "cause": "absent_on_recheck"}, receipt["items"])
                if not partial:
                    revoke, = (row for row in rows if row["kind"] == "cross_pair_revoked")
                    close, = (row for row in rows if row["kind"] == "cross_ceremony_closed")
                    self.assertLess(close["seq"], revoke["seq"])
                    self.assertLess(revoke["seq"], receipt["seq"])
                    self.assertEqual([{"pair_id": pair, "outcome": "UNKNOWN"}], receipt["notices"])
                self.assertEqual("complete", restarted.teardown_disable(disable, start, effects=effects)["state"])
                self.assertEqual(rows, restarted.read_committed().records)

    def test_optin_teardown_refuses_unowned_ballast_and_rechecks_retained_extra_after_loss(self) -> None:
        from floati.cross_journal import BALLAST

        class Inventory(_OwnedEnableInventory):
            def read(self, item, start):
                if item == "ballast":
                    return journal.inspect_enable_ballast(start, size=4096)
                return super().read(item, start)

            def create(self, item, start):
                journal.provision_enable_ballast(start, size=4096)

        class Cut(Storage):
            def boundary(self, operation, name):
                if (operation, name) == ("create", BALLAST):
                    raise harness.SimulatedCrash()

        class Effects:
            fail_after_removal = True
            extra_proof = "b" * 64

            def inventory(self, start, records):
                return {"plan-resource": self.extra_proof} if extra.exists() else {}

            def read(self, item, start, proof):
                if item == "plan-resource" and extra.exists():
                    return "owned", self.extra_proof
                return "safe_absent", None

            def remove(self, item, start, proof):
                self_test.assertEqual(("plan-resource", self.extra_proof), (item, proof))
                extra.unlink()
                if self.fail_after_removal:
                    self.fail_after_removal = False
                    raise harness.SimulatedCrash()

            def checks(self, start):
                return {key: True for key in ("unit_absent", "unit_not_loadable", "no_listener", "no_live_endpoint")}

        self_test = self
        directory, options, start, complete = self._r9_attended_fixture("substituted-unsealed-ballast")
        options["enable_inventory"] = Inventory()
        journal = CrossJournal(directory, storage=Cut(directory), **options)
        with self.assertRaises(harness.SimulatedCrash):
            journal.append(Capability("A", verb="enable"), complete)
        ballast = directory / BALLAST
        owned = ballast.read_bytes()
        ballast.unlink()
        ballast.touch()
        extra = directory / "extra-resource"
        extra.touch()
        effects = Effects()
        disable = Capability("L", verb="disable")
        journal = CrossJournal(directory, **options)
        self.assertEqual("interrupted", journal.teardown_disable(disable, start, effects=effects)["state"])
        self.assertEqual(b"", ballast.read_bytes())
        self.assertTrue(extra.exists())
        self.assertTrue(journal.read_committed().pending)
        ballast.write_bytes(owned)
        with self.assertRaises(harness.SimulatedCrash):
            journal.teardown_disable(disable, start, effects=effects)
        self.assertFalse(extra.exists())
        self.assertFalse(ballast.exists())
        journal = CrossJournal(directory, **options)
        self.assertEqual("complete", journal.teardown_disable(disable, start, effects=effects)["state"])
        receipt, = (row for row in journal.read_committed().records if row["kind"] == "cross_teardown_receipt")
        self.assertIn({"item": "plan-resource", "class": "confirmed_absent", "cause": "absent_on_recheck"},
                      receipt["items"])

    def test_optin_teardown_keeps_pair_inventory_when_a_restart_compacts_revocation(self) -> None:
        from floati.cross_anchor import Narrowing

        pair, ceremony = "d" * 32, "e" * 32
        pair_item, proof = f"pair:{pair}", "f" * 64

        class Effects:
            def inventory(self, start, records):
                return {pair_item: proof} if any(row.get("kind") == "cross_pair_active" for row in records) else {}

            def read(self, item, start, expected):
                return ("owned", proof) if item == pair_item and key.exists() else ("safe_absent", None)

            def remove(self, item, start, expected):
                self_test.assertEqual((pair_item, proof), (item, expected))
                key.unlink()

            def checks(self, start):
                return {key: True for key in ("unit_absent", "unit_not_loadable", "no_listener", "no_live_endpoint")}

        self_test = self
        directory, options, start, complete = self._r9_attended_fixture("disable-compacted-revocation")
        journal = CrossJournal(directory, **options)
        self.assertEqual("committed", journal.append(Capability("A", verb="enable"), complete).status)
        self.assertEqual("committed", _write(journal, "A", _opened(ceremony)).status)
        self.assertEqual("committed", _write(journal, "A", _activation(pair, ceremony)).status)
        key = directory / "paired-secret"
        key.touch()
        store = options["anchor_store"]
        replace = store.compare_and_set

        def interrupted_update(expected, successor):
            applied = replace(expected, successor)
            if applied and isinstance(expected, Narrowing) and expected.body()["kind"] == "cross_pair_revoked":
                raise harness.SimulatedCrash()
            return applied

        disable, effects = Capability("L", verb="disable"), Effects()
        with patch.object(store, "compare_and_set", interrupted_update), self.assertRaises(harness.SimulatedCrash):
            journal.teardown_disable(disable, start, effects=effects)
        self.assertTrue(key.exists())
        restarted = CrossJournal(directory, **options)
        restarted.compact("L", "explicit")
        self.assertFalse(any(row["kind"] == "cross_pair_active" for row in restarted.read_committed().records))
        self.assertEqual("complete", restarted.teardown_disable(disable, start, effects=effects)["state"])
        self.assertFalse(key.exists())
        receipt, = (row for row in restarted.read_committed().records if row["kind"] == "cross_teardown_receipt")
        self.assertIn({"item": pair_item, "class": "confirmed_absent", "cause": "removed_and_verified"}, receipt["items"])
        self.assertEqual([{"pair_id": pair, "outcome": "UNKNOWN"}], receipt["notices"])

    def test_r9_enable_resume_never_repairs_a_published_pair_activation(self) -> None:
        directory, options, start, complete = self._r9_attended_fixture("r9v1")
        writer = CrossJournal(directory, **options)
        self.assertEqual("committed", writer.append(Capability("A", verb="enable"), complete).status)
        _write(writer, "A", _opened("pair-cut"))
        store = options["anchor_store"]
        h2 = store.value
        store.refuse_next = True
        self.assertEqual("committed_unpublished", _write(writer, "A", _activation("p1", "pair-cut")).status)
        before = list(store.updates)
        fresh = CrossJournal(directory, **options)
        self.assertTrue(fresh.read_committed().pending)
        with self.assertRaises(ProtocolRefusal):
            fresh.create(ENABLE_ONE, enable_generation=7, attended=start)
        self.assertEqual(before, store.updates)
        self.assertEqual(h2, store.value)
        fresh.maintain(Capability("M", verb="maintain"))
        self.assertEqual(h2.trust[2] + 1, store.value.trust[2])
        self.assertEqual(1, len(store.updates) - len(before))
        self.assertFalse(fresh.read_committed(require_settled=True).pending)

    def test_r9_pairing_open_requires_current_sentinel_and_gate_readback(self) -> None:
        directory, options, _, complete = self._r9_attended_fixture("r9v6")
        writer = CrossJournal(directory, **options)
        self.assertEqual("committed", writer.append(Capability("A", verb="enable"), complete).status)
        before = harness.snapshot(directory)
        for item in ("sentinel", "gate"):
            with self.subTest(item=item):
                options["enable_inventory"].fault = item
                with self.assertRaises(ProtocolRefusal):
                    _write(CrossJournal(directory, **options), "A", _opened("exact-open"))
                self.assertEqual(before, harness.snapshot(directory))
        options["enable_inventory"].fault = None
        self.assertEqual("committed", _write(writer, "A", _opened("exact-open")).status)

    def test_r9_teardown_repair_is_not_enable_continuation_evidence(self) -> None:
        directory, options, _, complete = self._r9_attended_fixture("r9v2")
        writer = CrossJournal(directory, **options)
        original = harness.snapshot(directory)
        checkpoint, _ = writer._load()
        journal_path = directory / checkpoint["journal_file"]
        with journal_path.open("ab") as tail:
            tail.write(encode_frame(dict(_optin("disable_started", "local"), seq=checkpoint["through_seq"] + 1,
                                         prev=checkpoint["head_sha256"])))
        CrossJournal(directory).maintain()
        wrong_image = harness.snapshot(directory)
        for _ in range(2):
            harness.materialize(wrong_image, directory)
            with self.assertRaises(FloatiError):
                CrossJournal(directory, **options).append(Capability("A", verb="enable"), complete)
        harness.materialize(original, directory)
        interrupted = CrossJournal(directory, **options, storage=harness.TracingStorage(
            directory, stop_after=lambda label: label.startswith("append:journal-g")))
        with self.assertRaises(harness.SimulatedCrash):
            interrupted.append(Capability("A", verb="enable"), complete)
        fresh = CrossJournal(directory, **options)
        self.assertEqual("committed", fresh.append(Capability("A", verb="enable"), complete).status)
        self.assertEqual(1, len(fresh.attended_reviews()))

    def test_r9_lagging_h1_refuses_a_teardown_repair_grafted_onto_completion(self) -> None:
        from floati.cross_journal import line_digest

        directory, options, _, complete = self._r9_attended_fixture("r9v2-late")
        writer = CrossJournal(directory, **options)
        original = harness.snapshot(directory)
        start_checkpoint, _ = writer._load()
        journal_path = directory / start_checkpoint["journal_file"]
        with journal_path.open("ab") as tail:
            tail.write(encode_frame(dict(_optin("disable_started", "local"), seq=2, prev=start_checkpoint["head_sha256"])))
        isolated = CrossJournal(directory)
        isolated.maintain()
        repair = isolated.read_committed().records[-1]
        harness.materialize(original, directory)
        store = options["anchor_store"]
        store.refuse_h2 = True
        self.assertEqual("committed_unpublished", writer.append(Capability("A", verb="enable"), complete).status)
        h1 = store.value
        checkpoint, _ = writer._load()
        rows, _ = writer._verified(checkpoint)
        self.assertEqual("enable_complete", rows[-1]["phase"])
        prefix = encode_frame(rows[0]) + encode_frame(repair)
        head = line_digest(encode_frame(repair))
        for index, row in enumerate(rows[1:], 3):
            line = encode_frame(dict(row, seq=index, prev=head))
            prefix += line
            head = line_digest(line)
        journal_path.write_bytes(prefix)
        frame = encode_frame(dict(checkpoint, through_seq=len(rows) + 1, byte_length=len(prefix), head_sha256=head))
        (directory / "journal.commit").write_bytes(frame)
        (directory / "journal.commit.seal").write_bytes(encode_frame({
            "format": "floati-cross-seal-v1", "checkpoint_sha256": hashlib.sha256(frame).hexdigest(),
        }))
        store.refuse_h2 = False
        for _ in range(2):
            crossed = CrossJournal(directory, **options)
            self.assertTrue(crossed.read_committed().pending)
            with self.assertRaises(FloatiError):
                crossed.maintain()
            self.assertEqual(h1, store.value)

    def test_r9_unknown_restore_discriminator_cannot_authorize_a_tail_repair(self) -> None:
        from floati.cross_journal import parse_checkpoint

        writer = CrossJournal(self.seeded)
        raw = (self.seeded / "journal.commit").read_bytes()
        position = parse_checkpoint(raw)
        path = self.seeded / position["journal_file"]
        tail = b"unpublished"
        with path.open("ab") as file:
            file.write(tail)
        pending = dict(position, previous=writer._previous(position, raw), repair_intent={
            "offset": position["byte_length"], "tail_len": len(tail), "tail_sha256": hashlib.sha256(tail).hexdigest(),
            "complete_frames": "unknown_pending_kind",
        })
        (self.seeded / "journal.commit").write_bytes(encode_frame(pending))
        before = harness.snapshot(self.seeded)
        for _ in range(2):
            with self.assertRaises(FloatiError):
                CrossJournal(self.seeded).maintain()
            self.assertEqual(before, harness.snapshot(self.seeded))

    def test_r9_second_narrowing_repair_preserves_the_exact_retry(self) -> None:
        from floati.cross_anchor import Narrowing

        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                return True

        class TornRevoke(Storage):
            def append(self, name, data):
                added = super().append(name, data)
                if b'"kind":"cross_pair_revoked"' in data:
                    raise OSError(errno.EIO, "lost revocation append")
                return added

        for alternate in ("missing", "intervening", "exact"):
            with self.subTest(alternate=alternate):
                store = Store()
                directory = self.base / f"r9-repair-{alternate}"
                writer = CrossJournal(directory, anchor_store=store)
                writer.create(ENABLE_ONE)
                _write(writer, "A", _optin("enable_complete", None))
                _write(writer, "A", _opened("pair-repair"))
                _write(writer, "A", _activation("p1", "pair-repair"))
                revoke = _revoked("p1", "local")
                failed = CrossJournal(directory, storage=TornRevoke(directory), anchor_store=store)
                self.assertNotEqual("committed", _write(failed, "L", revoke).status)
                self.assertIsInstance(store.value, Narrowing)
                with self.assertRaises(ProtocolRefusal):
                    failed.maintain()
                fresh = CrossJournal(directory, anchor_store=store)
                if alternate == "missing":
                    links = list(directory.glob("journal-repair-proof-*.commit"))
                    self.assertEqual(1, len(links))
                    links[0].unlink()
                elif alternate == "intervening":
                    # Replace the second unpublished attempt with a different semantic frame.
                    checkpoint, _ = fresh._load()
                    path = directory / checkpoint["journal_file"]
                    with path.open("r+b") as stream:
                        stream.truncate(checkpoint["byte_length"])
                        stream.seek(0, os.SEEK_END)
                        body = _record("cross_peer_address", "interposed", pair_id="p1", addresses=["192.0.2.1:1"])
                        stream.write(encode_frame(dict(body, seq=checkpoint["through_seq"] + 1, prev=checkpoint["head_sha256"])))
                if alternate != "exact":
                    self.assertTrue(fresh.read_committed().pending)
                    with self.assertRaises(FloatiError):
                        fresh.maintain()
                    self.assertIsInstance(store.value, Narrowing)
                    continue
                fresh.maintain()
                rows = fresh.read_committed(require_settled=True).records
                self.assertEqual(2, sum(row["kind"] == "cross_journal_tail_repaired" for row in rows))
                self.assertEqual(1, sum(row["kind"] == "cross_pair_revoked" for row in rows))

    def test_r9_partial_disable_removes_the_exact_right_without_remint(self) -> None:
        from floati.cross_attended import approved_completion, approved_start

        class Store:
            value = None

            def __init__(self):
                self.issues = 0

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.issues += 1
                self.value = successor
                return True

        start = approved_start(ENABLE_ONE, 7, "a" * 32,
                               {"listen": [], "root": "/fixture/harbor", "store": "keychain", "unit": "none"})
        store = Store()
        directory = self.base / "r9-cancel"
        options = dict(anchor_store=store, enable_approval=lambda phase, body: body == start,
                       enable_inventory=_OwnedEnableInventory())
        journal = CrossJournal(directory, **options)
        journal.create(ENABLE_ONE, enable_generation=7, attended=start)
        with self.assertRaises(ProtocolRefusal):
            journal.cancel_partial_enable(Capability("I", verb="reconcile"), start)
        self.assertTrue(journal.cancel_partial_enable(Capability("L", verb="disable"), start))
        self.assertIsNone(store.value)
        for _ in range(2):
            fresh = CrossJournal(directory, **options)
            self.assertTrue(fresh.read_committed().pending)
            with self.assertRaises(ProtocolRefusal):
                fresh.create(ENABLE_ONE, enable_generation=7, attended=start)
            with self.assertRaises(ProtocolRefusal):
                fresh.append(Capability("A", verb="enable"), approved_completion(start))
        self.assertEqual(2, store.issues)

    def test_r9_attended_resume_reconciles_the_original_inventory_after_restore(self) -> None:
        from floati.cross_attended import approved_completion, approved_start
        from floati.cross_anchor import EnableCreate

        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                return True

        start = approved_start(ENABLE_ONE, 7, "a" * 32,
                               {"listen": [], "root": "/fixture/harbor", "store": "keychain", "unit": "none"})
        complete = approved_completion(start)
        store = Store()
        directory = self.base / "r9-inventory"
        approved = lambda phase, body: body == start
        CrossJournal(directory, anchor_store=store, enable_approval=approved).create(
            ENABLE_ONE, enable_generation=7, attended=start)
        self.assertIsInstance(store.value, EnableCreate)
        right = store.value
        earlier = harness.snapshot(directory)

        class Inventory:
            names = ("configuration", "unit", "ballast", "gate", "sentinel")

            def __init__(self):
                self.actual = {"sentinel": self.expected("sentinel", start)}
                self.created = []

            def expected(self, item, plan):
                return hashlib.sha256(encode_frame({"item": item, "plan": plan})).hexdigest()

            def read(self, item, plan):
                value = self.actual.get(item)
                return ("owned", value) if value is not None else ("safe_absent", None)

            def create(self, item, plan):
                observer = CrossJournal(directory)
                checkpoint, raw = observer._load()
                if observer._seal() != hashlib.sha256(raw).hexdigest():
                    raise AssertionError("inventory intent has not sealed")
                rows, _ = observer._verified(checkpoint)
                if not any(row.get("item") == item and row.get("phase") == "intent" for row in rows):
                    raise AssertionError("inventory effect ran before the intent")
                self.created.append(item)
                self.actual[item] = self.expected(item, plan)

        inventory = Inventory()
        cap = Capability("A", verb="enable")
        for phase in ("foreign", "genuine", "deleted"):
            with self.subTest(phase=phase):
                harness.materialize(earlier, directory)
                store.value = right if phase != "deleted" else None
                inventory.actual["sentinel"] = ("e" * 64 if phase == "foreign" else inventory.expected("sentinel", start))
                reader = CrossJournal(directory, anchor_store=store, enable_approval=approved, enable_inventory=inventory)
                if phase != "genuine":
                    before = list(inventory.created)
                    with self.assertRaises(ProtocolRefusal):
                        reader.append(cap, complete)
                    self.assertEqual(before, inventory.created)
                    continue
                self.assertEqual("committed", reader.append(cap, complete).status)
                self.assertEqual(["configuration", "unit", "ballast", "gate"], inventory.created)
                rows = reader.read_committed(require_settled=True).records
                self.assertEqual(12, sum(row["kind"] == "cross_inventory_item" for row in rows))
                fresh = CrossJournal(directory, anchor_store=store, enable_approval=approved, enable_inventory=inventory)
                self.assertEqual("existing", fresh.append(cap, complete).status)
                self.assertEqual(1, len(fresh.attended_reviews()))

    def test_r9_storage_only_witness_precedes_the_sealed_v4_controller(self) -> None:
        from contextlib import nullcontext

        from floati.cross_journal import parse_checkpoint
        from floati.cross_coordinates import successor_quota_sha256

        _closeout(CrossJournal(self.seeded))
        checkpoint_file = self.seeded / "journal.commit"
        saved = {}
        original = checkpoint_file.read_bytes()
        outer = parse_checkpoint(original)
        v3_bytes = encode_frame(dict(outer, format="floati-cross-checkpoint-v3",
                                     quota={"mode": "charged", "day": "2026-11-25", "count": 1}))
        checkpoint_file.write_bytes(v3_bytes)
        (self.seeded / "journal.commit.seal").write_bytes(encode_frame({
            "format": "floati-cross-seal-v1", "checkpoint_sha256": hashlib.sha256(v3_bytes).hexdigest(),
        }))

        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                saved["issued_at"] = checkpoint_file.read_bytes()
                return True

        store = Store()
        writer = CrossJournal(self.seeded, utc_day=lambda: "2026-11-28",
                              quota_store=store, anchor_store=Store(), quota_inventory=tuple, legacy_exclusion=lambda: nullcontext([]))
        self.assertEqual("2026-11-28", writer.seal_legacy_quota_cutoff(Capability("A", verb="legacy_rollover")))
        self.assertEqual(v3_bytes, saved["issued_at"])
        opening = parse_checkpoint(checkpoint_file.read_bytes())
        self.assertEqual("floati-cross-checkpoint-v4", opening["format"])
        self.assertNotIn("opening_sha256", opening)
        controller_hash = opening["previous"]["checkpoint_sha256"]
        self.assertNotEqual(hashlib.sha256(v3_bytes).hexdigest(), controller_hash)
        controller_raw = (self.seeded / f"journal-proof-{controller_hash}.commit").read_bytes()
        controller = parse_checkpoint(controller_raw)
        self.assertEqual(controller_hash, hashlib.sha256(controller_raw).hexdigest())
        self.assertEqual("floati-cross-checkpoint-v4", controller["format"])
        self.assertEqual(hashlib.sha256(v3_bytes).hexdigest(), controller["previous"]["checkpoint_sha256"])
        self.assertEqual("legacy-quota-migration", controller["compaction_intent"]["reason"])
        self.assertEqual(store.value.quota_sha256, successor_quota_sha256(opening["quota"]))
        self.assertEqual(store.value.quota_sha256, controller["compaction_intent"]["quota_sha256"])

    def test_r9_outer_checkpoint_vectors_and_format_discrimination(self) -> None:
        from floati.cross_coordinates import successor_quota_sha256
        from floati.cross_journal import parse_checkpoint

        enable = "a" * 32
        source = dict(generation=3, journal_id=f"cross-{enable}-g3", through_seq=12, byte_length=6500,
                      head_sha256="3" * 64, checkpoint_sha256="4" * 64)
        prior = dict(source, journal_file="journal-g3.jsonl")
        position = dict(generation=4, journal_file="journal-g4.jsonl", journal_id=f"cross-{enable}-g4",
                        byte_length=4600, through_seq=9, head_sha256="5" * 64)
        base = dict(position, previous=prior, repair_intent=None, compaction_intent=None)
        day = "2026-11-25"
        v3 = "floati-cross-checkpoint-v3"
        v4 = "floati-cross-checkpoint-v4"
        empty = dict(mode="exact", day=None, count=0, source=source)
        charged = dict(mode="charged", day=day, count=1)
        vectors = (
            (v3, dict(mode="legacy_blocked_through", day=day, predecessor=prior), 1018,
             "e472ca9661c901aab046ca2965f1f51931547b73f43660f3266d9f519047d9a4"),
            (v3, charged, 687, "b52d59729f2ede55c9900ad72bed5524a5d94f6fe2a415e9b7077733034aaea7"),
            (v4, dict(mode="legacy_blocked_through", day=day, source=source), 979,
             "91060c0cab540ce0c11922638ea253039fca4057dcb1d98f42938e8face27b9f"),
            (v4, dict(mode="exact", day=day, count=1, source=source), 972,
             "7884993687cf40b8aa92afd039b28ccc2ca9360d99145a5c186e94f6d462d81e"),
            (v4, empty, 964, "835aff0a6412bb0950284ccd2fc0acbaeef678ece454f10e89231cbda4db840b"),
        )
        for version, quota, width, digest in vectors:
            row = dict(base, format=version, quota=quota)
            raw = encode_frame(row)
            self.assertEqual(row, parse_checkpoint(raw))
            self.assertEqual((width, digest), (len(raw), hashlib.sha256(raw).hexdigest()))
            with self.assertRaises(FloatiError):
                parse_checkpoint(encode_frame(dict(row, format=v4 if version == v3 else v3)))
        with self.assertRaises(FloatiError):
            parse_checkpoint(encode_frame(dict(base, format=v4, quota=dict(empty, count=False))))
        empty_bytes = encode_frame(empty)[:-1]
        self.assertEqual((324, "fa977c65e4f7f6f65393711a5a4e11e1a5b71a696a44b8727803a5ece9ecf9e3"),
                         (len(empty_bytes), hashlib.sha256(empty_bytes).hexdigest()))
        self.assertEqual("f1c646f74e43558e30c63a618470b1730f6bfb505baa00d9e0a08ceed608584d",
                         successor_quota_sha256(empty))
        journal = CrossJournal(self.seeded)
        old = dict(base, format=v3, quota=charged)
        old_raw = encode_frame(old)
        cut = dict(position, checkpoint_sha256=hashlib.sha256(old_raw).hexdigest())
        del cut["journal_file"]
        quota = dict(mode="legacy_blocked_through", day="2026-11-28", source=cut)
        intent = dict(successor_file="journal-g5.jsonl", successor_journal_id=f"cross-{enable}-g5",
                      successor_generation=5, successor_length=4100, successor_through_seq=8,
                      successor_head_sha256="7" * 64, successor_sha256="8" * 64, reason="legacy-quota-migration")
        controller = journal._quota_controller(old, old_raw, quota, intent)
        raw = encode_frame(controller)
        opening = encode_frame(journal._successor_checkpoint(controller, raw))
        for frame, width, digest in (
            (raw, 1529, "c1bbab68299e27894c0327707f521b1118a8abdb8d531e0f1edd3c12f0c20183"),
            (opening, 977, "3a5b6d7aff51df55d8c84dafbc68f53a9567d110669228a1f5d9fe23a79f5623"),
        ):
            self.assertEqual((width, digest), (len(frame), hashlib.sha256(frame).hexdigest()))
            self.assertEqual(v4, parse_checkpoint(frame)["format"])

    def test_r9_inventory_limit_is_measured_before_independent_migration_issue(self) -> None:
        from contextlib import nullcontext

        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if expected != self.value:
                    return False
                self.value = successor
                return True

        _closeout(CrossJournal(self.seeded))
        before = harness.snapshot(self.seeded)
        store = Store()
        count = [160]
        journal = CrossJournal(self.seeded, utc_day=lambda: "2026-11-28",
                               quota_store=store, anchor_store=Store(), legacy_exclusion=lambda: nullcontext([]),
                               quota_inventory=lambda: tuple(f"independent-{i}" for i in range(count[0])))
        cap = Capability("A", verb="legacy_rollover")
        with self.assertRaises(ProtocolRefusal):
            journal.seal_legacy_quota_cutoff(cap)
        self.assertIsNone(store.value)
        self.assertEqual(before, harness.snapshot(self.seeded))
        count[0] = 159
        self.assertEqual("2026-11-28", journal.seal_legacy_quota_cutoff(cap))
        self.assertEqual(160, len(journal.quota_migration_inventory()))
        self.assertIn(f"quota-migration:{store.value.token}", journal.quota_migration_inventory())

    def test_r9_teardown_owns_the_migration_token_before_local_publication(self) -> None:
        from floati.cross_anchor import QuotaMigrationPending

        class Store:
            value = None
            lost_install = True
            lost_delete = True
            calls = 0

            def read(self):
                return self.value

            def compare_and_set(self, expected, replacement):
                if self.value != expected:
                    return False
                self.calls += 1
                if replacement is not None:
                    self.value = replacement
                    return not self.lost_install
                if self.lost_delete:
                    self.value = QuotaMigrationPending(expected.token)
                    return False
                self.value = None
                return True

        _closeout(CrossJournal(self.seeded))
        anchor, quota = Store(), Store()
        options = dict(anchor_store=anchor, quota_store=quota, legacy_exclusion=lambda: nullcontext([]),
                       quota_inventory=lambda: tuple(f"other-{i}" for i in range(159)))
        writer = CrossJournal(self.seeded, **options)
        old = (self.seeded / "journal.commit").read_bytes()
        with self.assertRaises(ProtocolRefusal):
            writer.seal_legacy_quota_cutoff(Capability("A", verb="legacy_rollover"))
        token = quota.value.token
        self.assertEqual(old, (self.seeded / "journal.commit").read_bytes())
        cap = Capability("L", verb="disable")
        with self.assertRaises(ProtocolRefusal):
            writer.dispose_quota_migration(cap, "f" * 32)
        self.assertEqual(1, quota.calls)
        with self.assertRaises(ProtocolRefusal):
            writer.dispose_quota_migration(cap, token)
        for _ in range(2):
            fresh = CrossJournal(self.seeded, **options)
            self.assertTrue(fresh.read_committed().pending)
            self.assertEqual(160, len(fresh.quota_migration_inventory()))
            self.assertEqual("unknown", fresh.quota_migration_disposition(token)["class"])
            with self.assertRaises(ProtocolRefusal):
                fresh.dispose_quota_migration(cap, token)
        self.assertEqual(2, quota.calls)
        quota.value = None
        self.assertEqual("confirmed_absent", writer.dispose_quota_migration(cap, token)["class"])
        self.assertEqual(159, len(writer.quota_migration_inventory()))
        self.assertEqual(old, (self.seeded / "journal.commit").read_bytes())

    def test_r9_quota_keeps_an_outstanding_slot_and_recuts_after_last_proof_loss(self) -> None:
        from floati.cross_anchor import QuotaMigrationPending

        class Store:
            value = None
            next_value = None
            outstanding = False

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if expected != self.value:
                    return False
                if self.outstanding:
                    self.next_value = successor
                    self.value = QuotaMigrationPending(successor.token)
                    return False
                self.value = successor
                return True

        _closeout(CrossJournal(self.seeded))
        anchor, quota = Store(), Store()
        quota.outstanding = True
        items = tuple(f"other-{i}" for i in range(159))
        options = dict(anchor_store=anchor, quota_store=quota, legacy_exclusion=lambda: nullcontext([]),
                       quota_inventory=lambda: items)
        writer = CrossJournal(self.seeded, **options, utc_day=lambda: "2026-11-28")
        cap = Capability("A", verb="legacy_rollover")
        old = (self.seeded / "journal.commit").read_bytes()
        with self.assertRaises(ProtocolRefusal):
            writer.seal_legacy_quota_cutoff(cap)
        witness = quota.next_value
        self.assertEqual(old, (self.seeded / "journal.commit").read_bytes())
        for _ in range(2):
            fresh = CrossJournal(self.seeded, **options)
            self.assertTrue(fresh.read_committed().pending)
            self.assertEqual(160, len(fresh.quota_migration_inventory()))
            self.assertEqual("unknown", fresh.quota_migration_disposition(witness.token)["class"])
            with self.assertRaises(ProtocolRefusal):
                fresh.maintain()
        quota.outstanding = False
        quota.value = witness
        fresh = CrossJournal(self.seeded, **options, utc_day=lambda: "2026-11-29")
        self.assertEqual("still_reachable", fresh.quota_migration_disposition(witness.token)["class"])
        fresh.maintain()
        self.assertFalse(fresh.read_committed().pending)

        quota.value = None
        self.assertEqual("confirmed_absent", fresh.quota_migration_disposition(witness.token)["class"])
        self.assertTrue(fresh.read_committed().pending)
        self.assertEqual("2026-11-29", fresh.seal_legacy_quota_cutoff(cap))
        self.assertNotEqual(witness.token, quota.value.token)
        self.assertIsNone(anchor.value)
        self.assertEqual(160, len(fresh.quota_migration_inventory()))

    def test_r10_stopped_v4_can_finish_inert_teardown_after_anchor_removal(self) -> None:
        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if expected != self.value:
                    return False
                self.value = successor
                return True

        directory = self.base / "stopped-v4"
        store, quota = Store(), Store()
        options = dict(anchor_store=store, quota_store=quota, legacy_exclusion=nullcontext,
                       authenticate_quota_history=lambda source, proof: ())
        writer = CrossJournal(directory, **options)
        writer.create(ENABLE_ONE)
        _write(writer, "A", _optin("enable_complete", None))
        writer.seal_authenticated_quota_history(Capability("A", verb="legacy_rollover"), "empty-history")
        disable = Capability("L", verb="disable")
        self.assertEqual("committed", writer.append(disable, _optin("disable_started", "local")).status)
        self.assertTrue(writer.remove_disable_anchor(disable, ENABLE_ONE, 1))
        self.assertIsNone(store.value)
        self.assertTrue(writer.read_committed().pending)
        self.assertEqual("complete", _closeout(CrossJournal(directory, **options))["state"])
        self.assertIsNone(store.value)
        before = harness.snapshot(directory)
        with self.assertRaises(ProtocolRefusal):
            _write(CrossJournal(directory, **options), "A", _opened("blocked"))
        self.assertEqual(before, harness.snapshot(directory))

    def test_r10_quota_transfer_observes_all_install_and_release_outcomes(self) -> None:
        from floati.cross_anchor import Anchor, OutstandingWrite, QuotaMigrationPending

        class Store:
            def __init__(self):
                self.value = None
                self.attempted = []
                self.pending = None
                self.outcome = None

            def read(self):
                return self.pending[0] if self.pending is not None else self.value

            def compare_and_set(self, expected, successor):
                if self.pending is not None or expected != self.value:
                    return False
                self.attempted.append((expected, successor))
                outcome = self.outcome
                self.outcome = None
                if outcome == "lost-unapplied":
                    return False
                if outcome and outcome.startswith("outstanding"):
                    self.pending = (OutstandingWrite() if isinstance(successor, Anchor)
                                    else QuotaMigrationPending(expected.token), successor)
                    return False
                self.value = successor
                return outcome != "lost-applied"

        seed = self.base / "r10-quota-transfer"
        anchor, store = Store(), Store()
        base_options = dict(legacy_exclusion=lambda: nullcontext(()), quota_inventory=lambda: ("anchor",),
                            authorize_quota_transfer=lambda capability, expected, witness: True,
                            utc_day=lambda: "2026-11-25")
        seed_writer = CrossJournal(seed, anchor_store=anchor, quota_store=store, **base_options)
        seed_writer.create(ENABLE_ONE)
        _write(seed_writer, "A", _optin("enable_complete", None))
        _closeout(seed_writer)
        anchor.value = None
        cap = Capability("A", verb="legacy_rollover")
        self.assertEqual("2026-11-25", seed_writer.seal_legacy_quota_cutoff(cap))
        disk, witness = harness.snapshot(seed), store.value
        for phase in ("install", "release"):
            for outcome in ("lost-applied", "lost-unapplied", "outstanding-applied", "outstanding-unapplied"):
                with self.subTest(phase=phase, outcome=outcome):
                    directory = seed.with_name(f"r10-{phase}-{outcome}")
                    harness.materialize(disk, directory)
                    anchor, quota = Store(), Store()
                    quota.value = witness
                    active = anchor if phase == "install" else quota
                    active.outcome = outcome
                    options = dict(base_options, anchor_store=anchor, quota_store=quota)
                    writer = CrossJournal(directory, **options)
                    with self.assertRaises(ProtocolRefusal):
                        writer.transfer_quota_migration(cap, witness.token)
                    if phase == "install":
                        self.assertEqual(witness, quota.value)
                        self.assertEqual([], quota.attempted)
                    else:
                        self.assertIsInstance(anchor.value, Anchor)
                    if active.pending is not None:
                        for _ in range(2):
                            restored = CrossJournal(directory, **options)
                            self.assertTrue(restored.read_committed().pending)
                            self.assertIn(f"quota-migration:{witness.token}", restored.quota_migration_inventory())
                            with self.assertRaises(ProtocolRefusal):
                                restored.transfer_quota_migration(cap, witness.token)
                        self.assertEqual(1, len(active.attempted))
                        if outcome == "outstanding-applied":
                            active.value = active.pending[1]
                        active.pending = None
                    if anchor.value is not None and quota.value is not None:
                        # Both witnesses exist. Only the matching opening can release U.
                        original = anchor.value
                        anchor.value = Anchor(original.trust, original.generation, "f" * 64)
                        with self.assertRaises(ProtocolRefusal):
                            CrossJournal(directory, **options).transfer_quota_migration(cap, witness.token)
                        self.assertEqual(witness, quota.value)
                        anchor.value = original
                    for _ in range(2):
                        harness.materialize(disk, directory)
                        restored = CrossJournal(directory, **options)
                        self.assertTrue(restored.transfer_quota_migration(cap, witness.token))
                        self.assertIsNone(quota.value)
                        self.assertEqual(("anchor",), restored.quota_migration_inventory())
                        self.assertFalse(restored.read_committed(require_settled=True).pending)

    def test_r10_exact_quota_requires_authenticated_complete_history_and_folds_copies(self) -> None:
        from floati.cross_journal import parse_checkpoint
        from floati.cross_reserve import project

        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                return True

        cap = Capability("A", verb="legacy_rollover")
        for count, route in ((0, "storage"), (1, "storage"), (1, "storage-v1"), (1, "anchor")):
            with self.subTest(count=count, route=route):
                directory = self.base / f"r10-quota-{route}-{count}"
                anchor, quota = Store(), Store()
                writer = CrossJournal(directory, anchor_store=anchor, utc_day=lambda: "2026-11-25")
                writer.create(ENABLE_ONE)
                _write(writer, "A", _optin("enable_complete", None))
                if count:
                    _write(writer, "A", _opened("historical"))
                    _write(writer, "A", _ceremony_closed("historical"))
                if route.startswith("storage"):
                    _closeout(writer)
                    anchor.value = None
                if route == "storage-v1":
                    previous, _ = writer._load()
                    previous["format"] = "floati-cross-checkpoint-v1"
                    previous.pop("opening_sha256", None)
                    legacy = encode_frame(previous)
                    (directory / "journal.commit").write_bytes(legacy)
                    (directory / "journal.commit.seal").write_bytes(encode_frame({
                        "format": "floati-cross-seal-v1", "checkpoint_sha256": hashlib.sha256(legacy).hexdigest(),
                    }))
                checkpoint, raw = writer._load()
                rows = writer.read_committed(require_settled=True).records
                openings = tuple((row["id"], row["ceremony_token"], row["utc_day"])
                                 for row in rows if row["kind"] == "cross_ceremony_opened")
                self.assertEqual(count, len(openings))
                digest = hashlib.sha256(raw).hexdigest()
                observations = []
                qualified = [False]
                supplied_history = [openings + openings]

                def authenticate(source, supplied):
                    observations.append(source)
                    if not qualified[0] or supplied != "local-history-proof":
                        return None
                    return supplied_history[0]

                options = dict(anchor_store=anchor, quota_store=quota, legacy_exclusion=lambda: nullcontext([]),
                               quota_inventory=tuple, authenticate_quota_history=authenticate)
                migrated = CrossJournal(directory, **options)
                before = harness.snapshot(directory)
                held = anchor.value
                with self.assertRaises(ProtocolRefusal):
                    migrated.seal_authenticated_quota_history(cap, "local-history-proof")
                self.assertEqual(before, harness.snapshot(directory))
                self.assertEqual(held, anchor.value)
                self.assertIsNone(quota.value)
                qualified[0] = True
                if count:
                    supplied_history[0] = ()
                    with self.assertRaises(ProtocolRefusal):
                        migrated.seal_authenticated_quota_history(cap, "local-history-proof")
                    supplied_history[0] = openings + ((openings[0][0], openings[0][1], "2026-11-26"),)
                    with self.assertRaises(ProtocolRefusal):
                        migrated.seal_authenticated_quota_history(cap, "local-history-proof")
                    self.assertEqual(before, harness.snapshot(directory))
                    self.assertEqual(held, anchor.value)
                    self.assertIsNone(quota.value)
                supplied_history[0] = openings + openings
                projection = migrated.seal_authenticated_quota_history(cap, "local-history-proof")
                self.assertEqual({"mode": "exact", "day": "2026-11-25" if count else None, "count": count,
                                  "source": {key: value for key, value in writer._previous(checkpoint, raw).items()
                                             if key != "journal_file"}}, projection)
                self.assertEqual([digest] * (4 if count else 2),
                                 [source["checkpoint_sha256"] for source in observations])
                for _ in range(2):
                    reader = CrossJournal(directory, **options)
                    view = reader.read_committed(require_settled=True)
                    state = project(view.records)
                    self.assertEqual((projection["day"], count), (state.quota_day, state.quota_count))
                    self.assertEqual(projection, parse_checkpoint((directory / "journal.commit").read_bytes())["quota"])
                if route == "anchor":
                    self.assertEqual(held.trust, anchor.value.trust)
                    self.assertNotEqual(held.opening_sha256, anchor.value.opening_sha256)
                    self.assertIsNone(quota.value)
                else:
                    self.assertIsNone(anchor.value)
                    self.assertIsNotNone(quota.value)

    def _assert_r10_qualified_completion_matrix(self, repaired, sentinel_present):
        from itertools import product

        from floati.cross_anchor import Narrowing

        class CompletionCut(Storage):
            def append(self, name, data):
                made = super().append(name, data)
                if b'"phase":"enable_complete"' in data:
                    raise OSError(errno.EIO, "lost enable completion append")
                return made

        class Material(_OwnedEnableInventory):
            def __init__(self, missing):
                self.missing = set(missing)
                self.created = []

            def read(self, item, start):
                return ("safe_absent", None) if item in self.missing else super().read(item, start)

            def create(self, item, start):
                self.missing.remove(item)
                self.created.append(item)

        directory, options, start, complete = self._r9_attended_fixture(f"r10-b26-{repaired}-{sentinel_present}")
        store = options["anchor_store"]
        enable = Capability("A", verb="enable")
        disable = Capability("L", verb="disable")
        if repaired:
            cut = CrossJournal(directory, storage=CompletionCut(directory), **options)
            self.assertEqual("failed", cut.append(enable, complete).status)
        earlier = harness.snapshot(directory)
        right = store.value
        names = ("configuration", "unit", "ballast", "gate")
        for mask in product((False, True), repeat=len(names)):
            with self.subTest(repaired=repaired, sentinel=sentinel_present, present=mask):
                harness.materialize(earlier, directory)
                store.value = right
                absent = {item for item, present in zip(names, mask) if not present}
                if not sentinel_present:
                    absent.add("sentinel")
                material = Material(absent)
                options["enable_inventory"] = material
                recovered = CrossJournal(directory, **options)
                self.assertEqual("committed", recovered.append(enable, complete).status)
                self.assertEqual(absent, set(material.created))
                reviews = recovered.attended_reviews()
                self.assertEqual(1, len(reviews))
                self.assertEqual(complete["id"], reviews[0]["transition_id"])
                self.assertEqual("published", recovered.read_committed(require_settled=True).state)
                completed = harness.snapshot(directory)
                self.assertEqual("committed", _write(recovered, "A", _opened("next-ceremony")).status)
                harness.materialize(completed, directory)
                stop = dict(_optin("disable_started", "local"), schema_version=1,
                            enable_generation=start["enable_generation"])
                before = len(store.updates)
                resumed = CrossJournal(directory, **options)
                self.assertEqual("committed", resumed.append(disable, stop).status)
                intent = store.updates[before]
                self.assertIsInstance(intent, Narrowing)
                self.assertEqual(hashlib.sha256(completed["journal.commit"]).hexdigest(), intent.predecessor_checkpoint)
                self.assertEqual(stop, intent.body())
                self.assertTrue(resumed.remove_disable_anchor(disable, ENABLE_ONE, start["enable_generation"]))
                self.assertIsNone(store.value)
                harness.materialize(completed, directory)
                self.assertTrue(CrossJournal(directory, **options).read_committed().pending)

    def test_r10_b26_create_with_surviving_sentinel_has_both_next_commands(self) -> None:
        self._assert_r10_qualified_completion_matrix(False, True)

    def test_r10_b26_create_with_safely_absent_sentinel_has_both_next_commands(self) -> None:
        self._assert_r10_qualified_completion_matrix(False, False)

    def test_r10_b26_repaired_h1_with_surviving_sentinel_has_both_next_commands(self) -> None:
        self._assert_r10_qualified_completion_matrix(True, True)

    def test_r10_b26_repaired_h1_with_safely_absent_sentinel_has_both_next_commands(self) -> None:
        self._assert_r10_qualified_completion_matrix(True, False)

    def test_r10_b26_each_inventory_repair_completes_both_next_commands(self) -> None:
        from floati.cross_anchor import Narrowing
        from floati.framing import decode_frames

        class InventoryCut(Storage):
            def __init__(self, directory, item, phase):
                super().__init__(directory)
                self.target = item, phase

            def append(self, name, data):
                made = super().append(name, data)
                row = decode_frames(data)[0]
                if (row.get("item"), row.get("phase")) == self.target:
                    raise OSError(errno.EIO, "lost scoped inventory append")
                return made

        for item in ("configuration", "unit", "ballast", "gate", "sentinel", "anchor"):
            for phase in ("intent", "created"):
                with self.subTest(item=item, phase=phase):
                    directory, options, start, complete = self._r9_attended_fixture(f"r10-cut-{item}-{phase}")
                    interrupted = CrossJournal(directory, storage=InventoryCut(directory, item, phase), **options)
                    enable = Capability("A", verb="enable")
                    disable = Capability("L", verb="disable")
                    with self.assertRaises(ProtocolRefusal):
                        interrupted.append(enable, complete)
                    writer = CrossJournal(directory, **options)
                    self.assertEqual("committed", writer.append(enable, complete).status)
                    active = writer.read_committed(require_settled=True)
                    self.assertEqual(1, sum(row["kind"] == "cross_journal_tail_repaired" for row in active.records))
                    completed = harness.snapshot(directory)
                    self.assertEqual("committed", _write(writer, "A", _opened("post-repair-ceremony")).status)
                    harness.materialize(completed, directory)
                    stop = dict(_optin("disable_started", "local"), schema_version=1,
                                enable_generation=start["enable_generation"])
                    store = options["anchor_store"]
                    before = len(store.updates)
                    self.assertEqual("committed", writer.append(disable, stop).status)
                    held = store.updates[before]
                    self.assertIsInstance(held, Narrowing)
                    self.assertEqual(hashlib.sha256(completed["journal.commit"]).hexdigest(), held.predecessor_checkpoint)
                    self.assertTrue(writer.remove_disable_anchor(disable, ENABLE_ONE, start["enable_generation"]))
                    self.assertIsNone(store.value)

    def test_r10_crossed_stop_histories_do_not_settle_either_independent_intent(self) -> None:
        from floati.cross_anchor import Anchor, Narrowing

        class Store:
            value = None
            lose_stop = False

            def __init__(self):
                self.writes = []

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if expected != self.value:
                    return False
                if self.lose_stop and isinstance(expected, Narrowing) and isinstance(successor, Anchor):
                    return False
                self.value = successor
                self.writes.append(successor)
                return True

        base = self.base / "r10-crossed-base"
        common = Store()
        journal = CrossJournal(base, anchor_store=common)
        journal.create(ENABLE_ONE)
        _write(journal, "A", _optin("enable_complete", None))
        _write(journal, "A", _opened("ordinary"))
        _write(journal, "A", _activation("p1", "ordinary"))
        seed = harness.snapshot(base)
        branches = {}
        for cause in ("local", "key_absent_reconstructed"):
            work = self.base / ("branch-" + cause)
            harness.materialize(seed, work)
            store = Store()
            store.value = common.value
            writer = CrossJournal(work, anchor_store=store, confirm_pair_absent=lambda pair: pair == "p1")
            body = _revoked("p1", cause)
            cap = Capability("L", "p1", verb="unpair") if cause == "local" else Capability("M", "p1", verb="maintain")
            self.assertEqual("committed", writer.append(cap, body).status)
            store.lose_stop = True
            self.assertNotEqual("committed", _write(writer, "L", _optin("disable_started", "local")).status)
            self.assertIsInstance(store.value, Narrowing)
            branches[cause] = store.value, harness.snapshot(work)

        probe = self.base / "crossed-stop"
        for owner, (intent, _) in branches.items():
            for origin, (_, disk) in branches.items():
                with self.subTest(owner=owner, origin=origin):
                    store = Store()
                    store.value = intent
                    for _ in range(2):
                        harness.materialize(disk, probe)
                        fresh = CrossJournal(probe, anchor_store=store)
                        if owner == origin:
                            fresh.maintain()
                            self.assertTrue(any(row.get("phase") == "disable_started"
                                                for row in fresh.read_committed(require_settled=True).records))
                        else:
                            self.assertTrue(fresh.read_committed().pending)
                            with self.assertRaises(FloatiError):
                                fresh.maintain()
                            self.assertEqual([], store.writes)
                            self.assertEqual(intent, store.value)

    def test_r10_clean_and_repaired_completion_offer_fresh_ceremony_and_bound_disable(self) -> None:
        class CompletionCut(Storage):
            def append(self, name, data):
                made = super().append(name, data)
                if b'"phase":"enable_complete"' in data:
                    raise OSError(errno.EIO, "lost enable completion append")
                return made

        for repaired in (False, True):
            with self.subTest(repaired=repaired):
                directory, options, start, complete = self._r9_attended_fixture(f"r10-next-{repaired}")
                writer = CrossJournal(directory, **options)
                enable = Capability("A", verb="enable")
                if repaired:
                    cut = CrossJournal(directory, storage=CompletionCut(directory), **options)
                    self.assertEqual("failed", cut.append(enable, complete).status)
                self.assertEqual("committed", writer.append(enable, complete).status)
                state = writer.read_committed(require_settled=True)
                self.assertEqual(int(repaired), sum(row["kind"] == "cross_journal_tail_repaired" for row in state.records))
                stable = options["anchor_store"].value
                disk = harness.snapshot(directory)
                self.assertEqual("committed", _write(writer, "A", _opened("fresh-pairing")).status)
                harness.materialize(disk, directory)
                self.assertEqual(stable, options["anchor_store"].value)

                stop = dict(_optin("disable_started", "local"), schema_version=1,
                            enable_generation=start["enable_generation"])
                self.assertEqual("committed", _write(writer, "L", stop).status)
                self.assertTrue(writer.remove_disable_anchor(Capability("L", verb="disable"), ENABLE_ONE,
                                                            start["enable_generation"]))
                self.assertIsNone(options["anchor_store"].value)
                harness.materialize(disk, directory)
                self.assertTrue(CrossJournal(directory, **options).read_committed().pending)

    def test_r9_first_bound_opening_charges_once_after_the_inclusive_cutoff(self) -> None:
        from floati.cross_attended import approved_completion, approved_start
        from floati.cross_reserve import project

        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                return True

        _closeout(CrossJournal(self.seeded))
        anchor, quota = Store(), Store()
        now = ["2026-11-28"]
        start = approved_start(ENABLE_TWO, 2, "b" * 32,
                               {"listen": [], "root": "/fixture/harbor", "store": "keychain", "unit": "none"})
        options = dict(anchor_store=anchor, quota_store=quota, legacy_exclusion=lambda: nullcontext([]),
                       quota_inventory=lambda: ("anchor",), utc_day=lambda: now[0],
                       enable_approval=lambda phase, body: body == start, enable_inventory=_OwnedEnableInventory())
        writer = CrossJournal(self.seeded, **options)
        migrate = Capability("A", verb="legacy_rollover")
        self.assertEqual("2026-11-28", writer.seal_legacy_quota_cutoff(migrate, attended=start))
        token = quota.value.token
        self.assertIsNone(anchor.value)
        writer.create(ENABLE_TWO, enable_generation=2, attended=start)
        self.assertEqual("committed", writer.append(Capability("A", verb="enable"), approved_completion(start)).status)
        self.assertIsNotNone(quota.value)
        self.assertTrue(writer.transfer_quota_migration(migrate, token))
        for _ in range(2):
            restored = CrossJournal(self.seeded, **options)
            with self.assertRaises(ProtocolRefusal) as refused:
                _write(restored, "A", _opened("first-after-cut"))
            self.assertEqual("cross_ceremony_quota", refused.exception.code)
        now[0] = "2026-11-29"
        for expected in ("committed", "existing"):
            restored = CrossJournal(self.seeded, **options)
            self.assertEqual(expected, _write(restored, "A", _opened("first-after-cut")).status)
            state = project(restored.read_committed(require_settled=True).records)
            self.assertEqual(("2026-11-29", 1), (state.quota_day, state.quota_count))

    def test_r9_quota_transfer_cannot_use_its_witness_to_fund_the_anchor(self) -> None:
        class Store:
            value = None

            def __init__(self):
                self.changes = []
                self.lose_return = False

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if expected != self.value:
                    return False
                self.value = successor
                self.changes.append(successor)
                if self.lose_return:
                    self.lose_return = False
                    return False
                return True

        _closeout(CrossJournal(self.seeded))
        anchor, quota = Store(), Store()
        inventory = {f"other-{n}" for n in range(159)}
        approvals = []

        def authorize(capability, expected, witness):
            approvals.append((capability, expected, witness))
            return True

        def reserve():
            inventory.add("anchor")
            return True

        options = dict(anchor_store=anchor, quota_store=quota, legacy_exclusion=lambda: nullcontext([]),
                       quota_inventory=lambda: sorted(inventory), authorize_quota_transfer=authorize,
                       reserve_quota_anchor=reserve, utc_day=lambda: "2026-11-28")
        writer = CrossJournal(self.seeded, **options)
        cap = Capability("A", verb="legacy_rollover")
        writer.seal_legacy_quota_cutoff(cap)
        witness = quota.value
        before = harness.snapshot(self.seeded)
        self.assertEqual(160, len(writer.quota_migration_inventory()))
        with self.assertRaises(ProtocolRefusal) as unavailable:
            writer.transfer_quota_migration(cap, witness.token)
        self.assertEqual("cross_quota_inventory_full", unavailable.exception.code)
        self.assertEqual([witness], quota.changes)
        self.assertEqual([], anchor.changes)
        self.assertEqual(before, harness.snapshot(self.seeded))
        self.assertEqual([], approvals)

        inventory.remove("other-158")
        measured = writer.quota_migration_inventory()
        self.assertEqual(159, len(measured))
        self.assertNotIn("other-158", measured)
        self.assertIn(f"quota-migration:{witness.token}", measured)
        anchor.lose_return = True
        with self.assertRaises(ProtocolRefusal):
            writer.transfer_quota_migration(cap, witness.token)
        fresh = CrossJournal(self.seeded, **options)
        self.assertEqual(160, len(fresh.quota_migration_inventory()))
        self.assertIs(witness, quota.value)
        self.assertIsNotNone(anchor.value)
        self.assertTrue(fresh.transfer_quota_migration(cap, witness.token))
        self.assertEqual(159, len(fresh.quota_migration_inventory()))
        self.assertEqual([witness, None], quota.changes)
        self.assertEqual(1, len(anchor.changes))
        self.assertTrue(CrossJournal(self.seeded, **options).transfer_quota_migration(cap, witness.token))

    def test_storage_only_migration_refuses_a_resealed_quota_sibling(self) -> None:
        from contextlib import nullcontext

        from floati.cross_journal import parse_checkpoint

        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                return True

        self.assertEqual("complete", _closeout(CrossJournal(self.seeded))["state"])
        store = Store()
        migration = Capability("A", verb="legacy_rollover")
        journal = CrossJournal(self.seeded, utc_day=lambda: "2026-11-28", quota_store=store, anchor_store=Store(), quota_inventory=tuple,
                               legacy_exclusion=lambda: nullcontext([]))
        self.assertEqual("2026-11-28", journal.seal_legacy_quota_cutoff(migration))
        self.assertIsNotNone(store.read())
        checkpoint, seal = self.seeded / "journal.commit", self.seeded / "journal.commit.seal"
        authentic, seal_bytes = checkpoint.read_bytes(), seal.read_bytes()
        sibling = parse_checkpoint(authentic)
        sibling["quota"]["day"] = "2026-11-25"
        forged = encode_frame(sibling)
        checkpoint.write_bytes(forged)
        seal.write_bytes(encode_frame({"format": "floati-cross-seal-v1",
                                      "checkpoint_sha256": hashlib.sha256(forged).hexdigest()}))
        fresh = CrossJournal(self.seeded, utc_day=lambda: "2026-11-26", quota_store=store)
        for _ in range(2):
            with self.assertRaises(FloatiError):
                fresh.read_committed(require_settled=True)
        checkpoint.write_bytes(authentic)
        seal.write_bytes(seal_bytes)
        self.assertFalse(fresh.read_committed(require_settled=True).pending)

    def test_every_earlier_enable_image_requires_current_authority(self) -> None:
        from floati.cross_attended import approved_completion, approved_start
        from floati import cross_anchor

        class Store:
            value = None
            failed = False
            replacements = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.replacements.append(type(successor).__name__)
                if self.failed:
                    self.failed = False
                    return False
                self.value = successor
                return True

        plan = {"listen": [], "root": "/fixture/harbor", "store": "keychain", "unit": "none"}
        start = approved_start(ENABLE_ONE, 1, "a" * 32, plan)
        complete = approved_completion(start)
        approve = lambda phase, body: body == start
        store = Store()
        store.replacements = []
        source = self.base / "initial-authority"
        writer = CrossJournal(source, anchor_store=store, enable_approval=approve, enable_inventory=_OwnedEnableInventory())
        writer.create(ENABLE_ONE, attended=start)
        self.assertEqual(["EnableCreate"], store.replacements)
        self.assertIsInstance(store.value, cross_anchor.EnableCreate)
        image = harness.snapshot(source)
        resumed = CrossJournal(source, anchor_store=store, enable_approval=approve, enable_inventory=_OwnedEnableInventory())
        cap = Capability("A", verb="enable")
        self.assertEqual("committed", resumed.append(cap, complete).status)
        self.assertEqual(["EnableCreate", "Anchor", "Anchor"], store.replacements)
        retired = self.base / "restored-old-start"
        harness.materialize(image, retired)
        store.value = None
        count = len(store.replacements)
        for _ in range(2):
            reader = CrossJournal(retired, anchor_store=store, enable_approval=approve, enable_inventory=_OwnedEnableInventory())
            with self.assertRaises(ProtocolRefusal):
                reader.create(ENABLE_ONE, attended=start)
            with self.assertRaises(ProtocolRefusal):
                reader.append(cap, complete)
        self.assertEqual(count, len(store.replacements))
        self.assertFalse(any(row.get("phase") == "enable_complete"
                             for row in CrossJournal(retired).read_committed().records))

    def test_successor_binds_quota_before_anchor_or_first_open(self) -> None:
        from floati import cross_coordinates

        cut = {"journal_id": f"cross-{'a' * 32}-g3", "generation": 3, "through_seq": 12,
               "head_sha256": "3" * 64, "byte_length": 6500, "checkpoint_sha256": "4" * 64}
        early = {"mode": "legacy_blocked_through", "day": "2026-11-25", "source": cut}
        late = dict(early, day="2026-11-28")
        commitment = getattr(cross_coordinates, "successor_quota_sha256", lambda value: None)
        self.assertEqual("c01f76d5c7e6f3d0262161e398d8e70339d2837751b76f9eb819257b2b2f76aa", commitment(early))
        self.assertEqual("98b2f2b36f2a3e4b8e843d4ef1f1cc77a5af09a27261295a94d34411e2639185", commitment(late))
        self.assertNotEqual(commitment(late), commitment(dict(late, source=dict(cut, checkpoint_sha256="5" * 64))))

    def test_sender_replay_price_and_scope_survive_retirement(self) -> None:
        from floati import cross_delivery

        scope = {"enable_id": "a" * 32, "enable_generation": 7, "pair_id": "6e5adfd61c91fc7e638d59c5355fde1e",
                 "direction": "S-to-T", "sender_node": "architect-a", "idempotency_key": "a"}
        identity = getattr(cross_delivery, "sender_replay_id", lambda key: None)
        self.assertEqual("cross-replay-6db2a309d57d761f197dbac43bdabb2b8f1d85a5f29e8a629c708d7a471a034c",
                         identity(scope))
        self.assertNotEqual(identity(scope), identity(dict(scope, sender_node="another-sender")))

    def test_r9_three_replay_obligations_charge_coexistence_in_both_dimensions(self) -> None:
        from floati.cross_reserve import DATA_BUDGET, admit, apply, project

        pair = "a" * 32
        state = project(())
        charges = ((900, 821, False), (700, 948, True), (1100, 1032, False))
        for seq, (size, price, keep) in enumerate(charges, 1):
            row = dict(kind="remote_outbound", schema_version=2, replay_price=price, replay_retained=keep,
                       seq=100000, enable_id="b" * 32, enable_generation=7, pair_id=pair, direction="S-to-T",
                       delivery_seq=seq)
            if seq == len(charges):
                for limit in (Budget(3648, 4), Budget(3647, 4), Budget(3648, 3)):
                    for scope in ("global", "pair", "both"):
                        local = limit if scope != "global" else DATA_BUDGET
                        total = limit if scope != "pair" else DATA_BUDGET
                        with self.subTest(limit=limit, scope=scope), patch("floati.cross_reserve.PAIR_DATA_BUDGET", local):
                            if limit == Budget(3648, 4):
                                self.assertEqual("data", admit(state, row, size, data=total))
                            else:
                                with self.assertRaises(ProtocolRefusal):
                                    admit(state, row, size, data=total)
            state = apply(state, row, size)
        self.assertEqual(Budget(3648, 4), state.pair_data[pair])
        self.assertEqual((3648, 4), (state.data_bytes, state.data_slots))
        successor = apply(project(()), dict(row, delivery_seq=2, replay_price=948, replay_retained=True), 700)
        for seq, (_, price, _) in enumerate(charges, 1):
            replay = dict(row, kind="remote_sender_replay", schema_version=1, delivery_seq=seq)
            successor = apply(successor, replay, price)
        self.assertEqual(Budget(3501, 4), successor.pair_data[pair])
        self.assertEqual((3501, 4), (successor.data_bytes, successor.data_slots))

    def test_v8_sender_replay_frames_match_all_frozen_lengths_and_digests(self) -> None:
        import json

        from floati.cross_coordinates import digest, enc, envelope_id
        from floati.cross_delivery import request_commitment, sender_replay, sender_replay_price

        pair = "6e5adfd61c91fc7e638d59c5355fde1e"
        accepted = {"accepted": {"record_id": "rmsg-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f"}}
        refused = {"refused": {"code": "cross_node_not_allowed"}}
        vectors = (
            ("short", accepted, 821, "4d3e457d6803f2ee8f6e189b43a21cc298edf2d4f923efb86cadd0b709c91131"),
            ("short", refused, 800, "217f1abafcb21973d16a223f8307254568a4fc47ea0611c7ea5156f84553840a"),
            ("short", None, 697, "65e478a39dde150a97985b2e38e8ea32c058544ebec48e840adef63c24ac5cc1"),
            ("key max", accepted, 948, "1f2026149d1cbf81e8e52d7b7d21c26e5dc73012345d3aa170a9cb5904f4392d"),
            ("all max", accepted, 1032, "b13e06e7df39b7ec11c7bd10b7e099aedf02d993127b5a227b3bd1748aa7c4bd"),
            ("all max", refused, 1011, "8c737f87b6bd6d1dbed8196a50c9cf1d927e3618745f74d21846042cd4ddc7a8"),
            ("all max", None, 908, "e995c2d78b4c994b0c5b818757d963d41fcde9f5d6d0a035e1fd8bc4af06123f"),
        )
        for variant, result, length, sha in vectors:
            with self.subTest(width=variant, result=result):
                max_width = variant == "all max"
                number = 9007199254740991 if max_width else 1
                fields = {"direction": "S-to-T", "doc": None, "kind": "message" if max_width else "ask_question",
                          "message_class": "normal", "note": "Which test covers the gate counter?", "pair_id": pair,
                          "recipient_node": "builder-a", "reply_to": None, "repo": None,
                          "sender_node": "z" * 64 if max_width else "architect-a", "sha": None, "v": 5}
                verb = "send" if max_width else "ask"
                eid = envelope_id(pair, fields["direction"], number)
                envelope = dict(fields, sender_timestamp="2026-11-24T12:00:00.000Z", seq=number, envelope_id=eid)
                outbound = {"enable_id": "a" * 32, "enable_generation": number if max_width else 7,
                            "pair_id": pair, "direction": fields["direction"], "verb": verb,
                            "idempotency_key": "a" if variant == "short" else "z" * 128,
                            "request_sha256": request_commitment({"verb": verb, "envelope_fields": fields}),
                            "delivery_seq": number, "envelope_id": eid, "envelope": envelope,
                            "envelope_digest": digest(enc(b"floati-remote-envelope-v5"), enc(encode_frame(envelope)[:-1]))}
                row = sender_replay(outbound, result)
                frame = json.dumps(dict(row, seq=100000, prev="f" * 64), sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
                self.assertEqual((length, sha), (len(frame), hashlib.sha256(frame).hexdigest()))
                if result == accepted:
                    self.assertEqual(length, sender_replay_price(outbound))

    def test_attended_review_survives_resume_and_rollover(self) -> None:
        import importlib

        attended = importlib.import_module("floati.cross_attended")
        start = attended.approved_start("a" * 32, 7, "b" * 32, {
            "listen": ["127.0.0.1:4321"], "root": "/fixture/harbor", "store": "keychain", "unit": "launchd",
        })
        self.assertEqual("faa74788f8d428cf8a4dd30d4f5d3bc0e6fcc7fad6a83265c0deef25b2abe446",
                         start["human_act"]["rendered_digest"])
        complete = attended.approved_completion(start)
        self.assertEqual("a1279682d53d6642106bac66bb422a683229e2022215bbd410571a6861062d32",
                         complete["approval_sha256"])
        self.assertEqual("cross-review-480e2c9c8b57dc7a4d24c2ed4f65101f0a712ec41a4b2ce532ac32111b4f17df",
                         attended.review_item(start, complete)["id"])
        with self.assertRaises(ProtocolRefusal):
            attended.review_item(start, dict(complete, token="c" * 32))

        directory = self.base / "attended"
        approve = lambda phase, source: source == start

        class Store:
            value = None

            def read(self):
                return self.value

            def compare_and_set(self, expected, successor):
                if self.value != expected:
                    return False
                self.value = successor
                return True

        store = Store()
        first = CrossJournal(directory, enable_approval=approve, anchor_store=store, enable_inventory=_OwnedEnableInventory())
        with self.assertRaises(ProtocolRefusal):
            first.create(start["enable_id"], enable_generation=7)
        first.create(start["enable_id"], enable_generation=7, attended=start)
        self.assertFalse(any(row.get("phase") == "enable_complete" for row in first.read_committed().records))
        resumed = CrossJournal(directory, enable_approval=approve, anchor_store=store, enable_inventory=_OwnedEnableInventory())
        cap = Capability("A", verb="enable")
        with self.assertRaises(ProtocolRefusal):
            resumed.append(cap, dict(complete, token="c" * 32))
        self.assertEqual("committed", resumed.append(cap, complete).status)
        self.assertEqual((attended.review_item(start, complete),), resumed.attended_reviews())

    def test_delivery_sequence_survives_retry_prune_and_restart(self) -> None:
        import importlib

        delivery = importlib.import_module("floati.cross_delivery")
        pair = "6e5adfd61c91fc7e638d59c5355fde1e"
        request = {"verb": "ask", "envelope_fields": {
            "direction": "S-to-T", "doc": None, "kind": "ask_question", "message_class": "normal",
            "note": "Which test covers the gate counter?", "pair_id": pair, "recipient_node": "builder-a",
            "reply_to": None, "repo": None, "sender_node": "architect-a", "sha": None, "v": 5,
        }}
        self.assertEqual("a406c45f5515080dc6e10b17296c82bb7eeb31d9063c20afad47853c07632642",
                         delivery.request_commitment(request))
        self.assertEqual(3, delivery.next_sequence(2, []))
        with self.assertRaises(ProtocolRefusal):
            delivery.next_sequence(2, [4])

        journal = CrossJournal(self.seeded, authorize_local_send=lambda capability, sender: sender == "architect-a")
        _write(journal, "A", _activation(pair, "c3"))
        cap = Capability("L", pair, verb="ask")
        timestamp = "2026-11-25T00:00:00.000Z"
        first = journal.allocate_outbound(cap, "a", request, timestamp)
        self.assertEqual("committed", first.status)
        fresh = CrossJournal(self.seeded, authorize_local_send=lambda capability, sender: sender == "architect-a")
        self.assertEqual("existing", fresh.allocate_outbound(cap, "a", request, timestamp).status)
        opposite = dict(request, envelope_fields=dict(request["envelope_fields"], direction="T-to-S"))
        self.assertEqual("committed", fresh.allocate_outbound(cap, "b", request, timestamp).status)
        self.assertEqual("committed", fresh.allocate_outbound(cap, "a", opposite, timestamp).status)
        self.assertEqual([("S-to-T", 1), ("S-to-T", 2), ("T-to-S", 1)],
                         [(row["direction"], row["delivery_seq"]) for row in fresh.read_committed().records
                          if row["kind"] == "remote_outbound"])

    def test_directional_prune_and_delivery_ids_match_frozen_vectors(self) -> None:
        from floati import cross_coordinates

        pair = "6e5adfd61c91fc7e638d59c5355fde1e"
        envelope_digest = "768329c6cb80fc7cb67bf2fe74d722109cfb4d4aae9483106435745d4edf5033"
        envelope_id = cross_coordinates.envelope_id
        receipt_id = cross_coordinates.receipt_id
        prune_digest = cross_coordinates.prune_digest
        self.assertEqual("renv-e61301a8f05990f0368f8fe68381aea4", envelope_id(pair, "S-to-T", 1))
        self.assertEqual("rcpt-5d78cc5624e93a960b359071c214d047", receipt_id(pair, "S-to-T", 1, envelope_digest))
        self.assertEqual("46a64cd51fbf1303461da1321ce4f0eb0bfb7f9a92252ec387bd3627aac648ee",
                         prune_digest(pair, "S-to-T", 1, b"synthetic archive fixture\n", []))

    def test_a_partial_shutdown_resumes_to_one_result(self) -> None:
        reference_directory = self._copy_of_seed("reference")
        _closeout(CrossJournal(reference_directory, storage=harness.TracingStorage(reference_directory)))
        reference = _plan_records(CrossJournal(reference_directory).read_committed().records)
        kinds = [row["kind"] for row in CrossJournal(reference_directory).read_committed().records]
        self.assertEqual(1, kinds.count("cross_teardown_receipt"))
        points, labels = harness.count_points(self._copy_of_seed("probe"), _closeout)
        violations: List[str] = []
        for point in range(points):
            work = self._copy_of_seed("work")
            storage = harness.TracingStorage(work, stop_at=point)
            try:
                _closeout(CrossJournal(work, storage=storage))
            except harness.SimulatedCrash:
                pass
            for image_label, files in harness.power_loss_images(work, storage.model).items():
                found = self.base / "image" / "cross"
                harness.materialize(files, found)
                try:
                    _closeout(CrossJournal(found, storage=harness.TracingStorage(found)))
                    records = CrossJournal(found).read_committed().records
                except FloatiError as exc:
                    violations.append(f"after {labels[point - 1] if point else 'start'}, {image_label}: {exc.code}")
                    continue
                repairs = [row for row in records if row["kind"] == "cross_journal_tail_repaired"]
                if _plan_records(records) != reference or len(repairs) > 1:
                    violations.append(
                        f"after {labels[point - 1] if point else 'start'}, {image_label}: "
                        f"resumed to {_plan_records(records)[-6:]} with {len(repairs)} repairs")
        if violations:
            self.fail(f"{len(violations)} violations; first 12:\n" + "\n".join(violations[:12]))

    def _fork(self, work: Callable[[int], None], children: int) -> int:
        pids = []
        for child in range(children):
            pid = os.fork()
            if pid == 0:  # pragma: no cover - runs in the child
                try:
                    work(child)
                except BaseException:
                    pass
                finally:
                    os._exit(0)
            pids.append(pid)
        killed = 0
        for pid in pids:
            _, status = os.waitpid(pid, 0)
            killed += int(os.WIFSIGNALED(status))
        return killed

    def test_racing_writers_compactors_and_shutdowns_converge(self) -> None:
        from floati.cross_authority import Capability

        directory = self._copy_of_seed("race")
        logs = self.base / "logs"
        logs.mkdir()

        expected_refusals = {"cross_maintenance_due", "cross_compaction_no_gain"}

        def note(child: int, line: str) -> None:
            with (logs / f"{child}.notes").open("a") as notes:
                notes.write(line + "\n")

        def writer(child: int) -> None:
            rng = random.Random(child)
            ingress = Capability("I", pair_id="p1")
            for number in range(10):
                # Keep competing compactions, but require each designated
                # writer to reach an actual append cut. A no-gain compaction
                # or ingress deferral can return before the armed boundary.
                dies = child < 4 and number >= 1 + child and number % 4 != 3
                storage = harness.TracingStorage(
                    directory, stop_at=2 + rng.randint(0, 5), stop="kill") if dies else None
                journal = CrossJournal(directory, storage=storage or harness.TracingStorage(directory))
                record = _pair_record("remote_inbound", "p1", 100 * child + number)
                try:
                    if number % 4 == 3:
                        journal.compact(Capability("L"), "explicit")
                        continue
                    try:
                        result = _write(journal, ingress, record)
                    except ProtocolRefusal as exc:
                        if exc.code != "cross_maintenance_due":
                            raise
                        journal.maintain(Capability("M"))
                        result = _write(journal, ingress, record)
                except ProtocolRefusal as exc:
                    if exc.code not in expected_refusals:
                        note(child, f"unexpected {exc.code}")
                    continue
                except BaseException as exc:
                    note(child, f"unexpected {type(exc).__name__}: {exc}")
                    return
                if result.status == "committed":
                    with (logs / f"{child}.log").open("a") as log:
                        log.write(record["id"] + "\n")

        killed = self._fork(writer, 6)
        journal = CrossJournal(directory)
        _write(journal, "L", _optin("disable_started", "local"))
        settled = journal.read_committed(require_settled=True)

        def after_stop(child: int) -> None:
            try:
                _write(CrossJournal(directory), "I", _pair_record("remote_inbound", "p1", 900 + child))
            except ProtocolRefusal as exc:
                note(200 + child, exc.code)

        self._fork(after_stop, 2)
        self.assertEqual(["cross_disable_in_progress"] * 2,
                         [(logs / f"{200 + child}.notes").read_text().strip() for child in range(2)])
        self.assertEqual(settled, journal.read_committed(require_settled=True))
        for child in range(2):
            (logs / f"{200 + child}.notes").unlink()

        def shutdown(child: int) -> None:
            rng = random.Random(100 + child)
            storage = harness.TracingStorage(directory, stop_at=rng.randint(0, 60), stop="kill")
            try:
                CrossJournal(directory, storage=storage).closeout(Capability("L", verb="disable"), RECEIPT_ITEMS)
            except BaseException as exc:
                note(100 + child, f"unexpected {type(exc).__name__}: {exc}")

        killed += self._fork(shutdown, 3)
        CrossJournal(directory, storage=harness.TracingStorage(directory)).closeout(
            Capability("L", verb="disable"), RECEIPT_ITEMS)
        records = CrossJournal(directory).read_committed().records
        present = [str(row["id"]) for row in records]
        logged = [line for path in sorted(logs.glob("*.log")) for line in path.read_text().split()]
        notes = [line for path in sorted(logs.glob("*.notes")) for line in path.read_text().splitlines()]
        self.assertEqual([], notes)
        self.assertGreaterEqual(killed, 4, "the race killed too few processes mid-flight")
        self.assertGreater(len({path.name for path in logs.glob("*.log")}), 1, "fewer than two writers committed")
        self.assertEqual([], [record_id for record_id in logged if record_id not in present])
        self.assertEqual(len(present), len(set(present)))
        kinds = [str(row["kind"]) for row in records]
        phases = [row.get("phase") for row in records if row["kind"] == "cross_optin"]
        self.assertEqual(1, phases.count("disable_started"))
        self.assertEqual("disable_complete", phases[-1])
        self.assertEqual(1, phases.count("disable_complete"))
        self.assertEqual(1, kinds.count("cross_teardown_receipt"))
        revoked = sorted(str(row["pair_id"]) for row in records if row["kind"] == "cross_pair_revoked")
        self.assertEqual(["p1", "p2"], revoked)
        self.assertEqual(1, len(list(directory.glob("journal-g*.jsonl"))))


F_FULLFSYNC = 51  # <sys/fcntl.h> on Darwin


class _RecordingStorage(Storage):
    """Real syncs, with each platform call and each synced name recorded."""

    full_sync = True

    def __init__(self, directory: Path, *, refuse: bool = False) -> None:
        self.barrier_observations = []
        super().__init__(directory, barrier_observer=self.barrier_observations.append)
        self.calls: List[object] = []
        self.synced: List[str] = []
        self.refuse = refuse
        self.fsync_call = self._fsync
        self.fcntl_call = self._full

    def _fsync(self, descriptor: int) -> None:
        self.calls.append("fsync")
        os.fsync(descriptor)

    def _full(self, descriptor: int, operation: int) -> None:
        self.calls.append(("full", operation))
        if self.refuse:
            raise OSError(errno.ENOTSUP, "full sync refused")

    def boundary(self, operation: str, name: str) -> None:
        if operation in {"sync", "sync_directory"}:
            self.synced.append(name)


class CrossFullSyncTests(unittest.TestCase):
    """R5.1 on Darwin: each sync is fsync then F_FULLFSYNC, and a refusal is reported."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve() / "cross"

    def _exercise(self, storage: Storage) -> Dict[str, object]:
        """Journal, checkpoint, seal, directory, successor and repair syncs."""

        journal = CrossJournal(self.directory, storage=storage)
        journal.create(ENABLE_ONE)
        for ceremony in ("c1", "c2"):
            _write(journal, "A", _opened(ceremony))
            _write(journal, "A", _ceremony_closed(ceremony))
        journal.compact("L", "explicit")
        storage.append("journal-g2.jsonl", b'{"torn":')
        return journal.maintain("M")

    def test_every_sync_is_followed_by_a_full_sync(self) -> None:
        storage = _RecordingStorage(self.directory)
        report = self._exercise(storage)
        self.assertEqual("full", report["durability"])
        self.assertGreater(len(storage.calls), 20)
        self.assertEqual(["fsync", ("full", F_FULLFSYNC)] * (len(storage.calls) // 2), storage.calls)
        for name in ("journal-g1.jsonl", "journal-g2.jsonl", ".journal.commit.tmp",
                     ".journal.commit.seal.tmp", "."):
            self.assertIn(name, storage.synced)

    def test_a_refused_full_sync_reports_fsync_only(self) -> None:
        report = self._exercise(_RecordingStorage(self.directory, refuse=True))
        self.assertEqual("fsync_only", report["durability"])
        fresh = CrossJournal(self.directory, storage=_RecordingStorage(self.directory))
        self.assertEqual("unknown", fresh.maintain()["durability"])
        fresh.storage.sync("journal-g2.jsonl")
        self.assertEqual("unknown", fresh.read_committed().historical_durability)
        self.assertEqual("unknown", fresh.maintain()["durability"])

    def test_earlier_failed_barrier_is_visible_after_clean_repair(self) -> None:
        class OneFailure(_RecordingStorage):
            def _full(self, descriptor, operation):
                if not self.calls or sum(isinstance(call, tuple) for call in self.calls) == 0:
                    self.calls.append(("full", operation))
                    raise OSError(errno.ENOTSUP, "the first full sync failed")
                super()._full(descriptor, operation)

        storage = OneFailure(self.directory)
        final = self._exercise(storage)
        self.assertEqual("full", final["durability"])
        observations = getattr(storage, "barrier_observations", ())
        self.assertEqual([("make_directory", ".", "fsync_only")],
                         [(event.operation, event.object_name, event.grade)
                          for event in observations if event.grade == "fsync_only"])
        self.assertTrue(any(event.object_name == "journal-g2.jsonl" and event.grade == "full"
                            for event in observations))

    @unittest.skipUnless(sys.platform == "darwin", "F_FULLFSYNC exists only on Darwin")
    def test_this_drive_accepts_the_full_sync(self) -> None:
        observations = []
        storage = Storage(self.directory, barrier_observer=observations.append)
        self.assertEqual("full", self._exercise(storage)["durability"])
        self.assertEqual({"full"}, {event.grade for event in observations})
        reached = {(event.operation, event.object_name) for event in observations}
        self.assertTrue({("make_directory", "."), ("sync_directory", "."), ("sync", "journal-g1.jsonl"),
                         ("sync", "journal-g2.jsonl"), ("sync", ".journal.commit.tmp"),
                         ("sync", ".journal.commit.seal.tmp")} <= reached)


def _copy(model: dict) -> dict:
    return {
        "current": dict(model["current"]),
        "durable": dict(model["durable"]),
        "data": dict(model["data"]),
        "next": model["next"],
    }


if __name__ == "__main__":
    unittest.main()
