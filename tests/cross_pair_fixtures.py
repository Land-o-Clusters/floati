"""In-process endpoints, isolated surfaces and independent durability fixtures."""

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from time import monotonic
from types import SimpleNamespace

from floati.cross_anchor import Anchor
from floati.cross_attended import approved_completion, approved_start
from floati.cross_authority import Capability
from floati.cross_coordinates import semantic
from floati.cross_gate import GateImage, GateOwner
from floati.cross_journal import CrossJournal, Storage
from floati.cross_reserve import Budget, closeout_cost, project
from floati.errors import ProtocolRefusal
from tests.cross_journal_harness import TracingStorage


class FixtureChannel:
    def __init__(self, incoming, outgoing, peer_leaf, case):
        self.incoming, self.outgoing = incoming, outgoing
        self.peer_leaf, self.case = peer_leaf, case
        self.sent = []
        self.final_deadline = None

    def authenticate(self, *, role, local_der, peer_der, deadline):
        return b"another leaf" if self.case == "wrong_leaf" else self.peer_leaf

    def send(self, payload, deadline):
        kind = json.loads(payload)["t"]
        self.sent.append(kind)
        if (self.case, kind) in {("lost_s", "final_S"), ("lost_t", "final_T")}:
            self.outgoing.put(None)
        else:
            self.outgoing.put(payload)
        if self.case == "duplicate_commit" and kind == "commit":
            self.outgoing.put(payload)

    def receive(self, deadline):
        if self.sent and self.sent[-1] == "final_S":
            self.final_deadline = deadline
        try:
            value = self.incoming.get(timeout=max(0, deadline - monotonic()))
        except Empty:
            raise TimeoutError from None
        if value is None:
            raise EOFError
        return value

    def close(self):
        self.outgoing.put(None)


class FixtureSurface:
    isolated = True

    def __init__(self, incoming, outgoing, case, role):
        self.visible = None
        self.captured = []
        self.incoming, self.outgoing = incoming, outgoing
        self.case, self.role = case, role
        self.reads = 0

    def clear(self):
        self.visible = None

    def draw(self, display):
        self.visible = display
        self.captured.append(display)
        for line in display.splitlines():
            if line.startswith(("Compare code ", "Return code ")):
                self.outgoing.put(line.split()[-1])

    def read_code(self, deadline):
        self.reads += 1
        if (self.case, self.role) in {("bad_a", "T"), ("bad_b", "S")}:
            return "!"
        try:
            value = self.incoming.get(timeout=max(0, deadline - monotonic()))
        except Empty:
            raise TimeoutError from None
        if value is None:
            raise EOFError
        return value


class FixtureAnchor:
    value = None
    before_advance = None

    def read(self):
        return self.value

    def compare_and_set(self, expected, successor):
        if self.value != expected:
            return False
        if (self.before_advance is not None and isinstance(expected, Anchor)
            and isinstance(successor, Anchor) and successor.trust[2] == expected.trust[2] + 1):
            self.before_advance()
        self.value = successor
        return True


class FixturePairBackend:
    """Actual journal and anchor paths, with independent fixture adapters."""

    def __init__(self, role, cut, directory, case="success"):
        from floati.cross_pair_storage import JournalOpenings
        from tests.test_cross_journal import _OwnedEnableInventory

        self.role, self.cut, self.case = role, cut, case
        self.lock = Lock()
        self.order = []
        self.leaf = b"fixture-" + role.encode()
        self.directory = directory
        self.anchor = FixtureAnchor()
        self.start = approved_start(
            "a3" * 16 if role == "S" else "b3" * 16, 7, "c3" * 16,
            {"listen": [], "root": "/fixture/cross", "store": "keychain", "unit": "none"}, schema_version=2,
        )
        self.inventory = _OwnedEnableInventory()
        self.owner = self.binding = self.final = self.effects = None
        self.journal = self.fresh_journal()
        self.journal.create(self.start["enable_id"], enable_generation=7, attended=self.start)
        assert self.journal.append(Capability("A", verb="enable"), approved_completion(self.start)).status == "committed"
        self.charges = JournalOpenings(self.journal)
        self.material = FixtureMaterial(self)
        self.storage = self.journal.storage
        self.opening = None

    def fresh_journal(self):
        return CrossJournal(self.directory, storage=TracingStorage(self.directory), anchor_store=self.anchor,
                            enable_inventory=self.inventory, enable_approval=lambda phase, body: body == self.start,
                            prove_pair_material=self.prove_pair_material,
                            utc_day=lambda: "2026-01-01")

    def prove_pair_material(self, record, *, phase):
        return self.effects.require_record(self.owner.journal_id, record) == self.binding

    @contextmanager
    def exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise ProtocolRefusal("cross_pairing_busy", "busy")
        try:
            yield
        finally:
            self.lock.release()

    def open(self, role):
        self.opening = self.charges.open(role)
        return self.opening

    def stage(self, opening):
        self.material.staged = opening.token
        return self.leaf

    def check_candidate(self, opening, pair_id, label):
        self.charges.check_candidate(opening, pair_id, label)

    def boundary(self, step):
        if self.cut == (self.role, step):
            raise OSError("synthetic fixture failure")

    def activate(self, opening, activation, gate_root, stream):
        from floati.cross_pair_storage import JournalFinal, PairBinding, PairEffects, PairGate

        self.owner = GateOwner(f"cross-{opening.enable_id}-g1", opening.enable_id,
                               opening.enable_generation, opening.token, activation.pair_id)
        self.storage.create("gate-state", GateImage.empty(self.owner.journal_id).encoded())
        self.effects = PairEffects(PairGate(self.storage), self.material)
        record = dict(opening.scope(), kind="cross_pair_active", schema_version=1,
                      id="cross-pair-active-" + activation.pair_id, pair_id=activation.pair_id,
                      opening_id=activation.opening_id, ceremony_id=activation.wire_id,
                      local_der_sha256=activation.local_der_sha256, peer_der=activation.peer_der.hex(),
                      peer_sha256=activation.peer_sha256, peer_descriptor=json.loads(activation.peer_descriptor),
                      role=activation.role, label=activation.label, allowlist=list(activation.allowlist),
                      wake_list=list(activation.wake_list), policy=activation.policy,
                      transcript_receipt_digest=activation.transcript_receipt_digest,
                      human_act={"channel": "controlling_terminal", "rendered_digest": activation.rendered_digest})
        if self.role == "S" and self.case == "changed_transcript":
            record["transcript_receipt_digest"] = "0" * 64
        self.binding = PairBinding.from_record(self.owner, record)
        self.final = JournalFinal(self.journal, opening, activation, stream, self.effects, self.binding)

        def at_anchor():
            fault = self.cut[1] if self.cut is not None and self.cut[0] == self.role else None
            if fault == "item_missing":
                self.material.item = None
            elif fault == "item_other_scope":
                foreign = replace(self.binding, owner=replace(self.owner, ceremony_token="ab" * 16))
                self.material.item = replace(self.material.item, binding=foreign)
            elif fault == "files_changed":
                self.material.key.write_bytes(b"another key")
            elif fault == "gate_changed":
                (self.directory / "gate-state").write_bytes(GateImage.empty(self.owner.journal_id).encoded())
            checkpoint, _ = self.journal._load()
            rows, _ = self.journal._verified(checkpoint)
            published = next(row for row in rows if row["id"] == record["id"])
            self.effects.require_record(self.owner.journal_id, published, gate_root)
            self.boundary("published")
            self.boundary("anchor")

        def prepare(_record):
            self.effects.activate(self.binding, self.leaf, gate_root,
                                  publish=lambda: self.boundary("gate"), advance_anchor=lambda: None)

        self.anchor.before_advance = at_anchor
        try:
            result = self.journal.activate_pair(opening.capability(), record, prepare=prepare)
        finally:
            self.anchor.before_advance = None
        if result.status != "committed":
            raise ProtocolRefusal("cross_pairing_publication_unknown", "activation unsettled")

    def stop(self):
        enable = self.start["enable_id"]
        result = self.journal.append(Capability("L", verb="disable"), dict(
            schema_version=1, kind="cross_optin", phase="disable_started", cause="local",
            id=f"cross-optin-{enable}-disable-started", enable_id=enable, enable_generation=7,
        ))
        assert result.status == "committed", result
        self.order.append("stop")

    def deposit_final(self, opening, output):
        if self.role == "S" and self.case == "stop_first":
            self.stop()
        self.final.deposit(opening, output)
        if output.queued:
            self.order.append("final")
        if self.role == "S" and self.case == "final_first":
            self.stop()

    def observe(self, opening, activation):
        original = self.active
        if original is not None:
            if original["pair_id"] == activation.pair_id and not any(
                row.get("phase") == "disable_started" for row in self.journal.read_committed().records
            ):
                self.journal.record_pair_seen(
                    opening.capability(), semantic(original),
                    verify_testimony=lambda record: record["opening_id"] == activation.opening_id
                    and record["ceremony_id"] == activation.wire_id
                    and record["transcript_receipt_digest"] == activation.transcript_receipt_digest,
                )

    @property
    def active(self):
        return next((row for row in self.fresh_journal().read_committed().records
                     if row["kind"] == "cross_pair_active"), None)

    def _status(self, journal):
        from floati.cross_pair import outcome

        view = journal.read_committed()
        active = self.active
        if view.pending:
            return outcome("unknown", active["pair_id"] if active else None)
        if active is None:
            return outcome("absent")
        if "disable_started" in project(view.records).phases:
            return outcome("unknown", active["pair_id"])
        self.effects.require(self.binding)
        seen = any(row["kind"] == "cross_pair_seen" and row.get("pair_id") == active["pair_id"]
                   for row in view.records)
        return outcome("reported" if self.role == "T" else "seen" if seen else "unconfirmed", active["pair_id"])

    def finish(self, opening):
        if self.active is None and not self.journal.read_committed().pending:
            if self.owner is not None:
                self.effects.collect(self.owner, abandoned=True, publication="absent")
            else:
                self.material.staged = None
            self.charges.close(opening)
        return self._status(self.journal)

    def recover(self):
        from floati.cross_pair import outcome
        fresh = self.fresh_journal()
        persisted = self.active
        if persisted is not None:
            try:
                fresh.maintain(Capability("M", verb="maintain"))
            except (ProtocolRefusal, OSError):
                return outcome("unknown", persisted["pair_id"])
        return self._status(fresh)


class FixtureMaterial:
    def __init__(self, backend):
        self.backend = backend
        self.staged = self.item = self.promoted = None
        self.leaf = backend.directory / "fixture-leaf"
        self.key = backend.directory / "fixture-key"

    def sync_staging(self, owner, local_der):
        self.backend.boundary("stage_sync")
        assert self.staged == owner.ceremony_token and local_der == self.backend.leaf

    def create_item(self, item):
        self.backend.boundary("store")
        self.item = item

    def promote(self, owner):
        self.backend.boundary("rename")
        self.promoted, self.staged = owner, None
        self.leaf.write_bytes(self.backend.leaf)
        self.key.write_bytes(self.backend.leaf)

    def read_item(self, owner):
        return self.item

    def read_files(self, owner):
        from floati.cross_pair_storage import PairFiles

        if self.promoted is None:
            return None
        return PairFiles(self.promoted, sha256(self.leaf.read_bytes()).hexdigest(), sha256(self.key.read_bytes()).hexdigest())

    def discard(self, owner):
        assert self.staged in (None, owner.ceremony_token)
        assert self.promoted in (None, owner)
        assert self.item is None or self.item.binding.owner == owner
        self.leaf.unlink(missing_ok=True)
        self.key.unlink(missing_ok=True)
        self.staged = self.item = self.promoted = None
        return "confirmed_absent"


def run_pair(case, cut=None, policies=((), ())):
    with TemporaryDirectory() as directory:
        return _run_pair(Path(directory), case, cut, policies)


def _run_pair(directory, case, cut, policies):
    from floati.cross_pair import Pairing

    channels, terminals = (Queue(), Queue()), (Queue(), Queue())
    endpoints = []
    now = monotonic()
    for index, role in enumerate(("S", "T")):
        backend = FixturePairBackend(role, cut, directory / role, case)
        surface = FixtureSurface(terminals[index], terminals[1 - index], case, role)
        channel = FixtureChannel(channels[index], channels[1 - index],
                                 b"fixture-" + (b"T" if role == "S" else b"S"), case)
        endpoints.append(SimpleNamespace(role=role, backend=backend, surface=surface, channel=channel))
    def operate(end):
        try:
            end.result = Pairing(end.backend, clock=lambda: now).run(
                end.role, end.surface, lambda deadline: end.channel, label="peer",
                address="fixture", label_hint="fixture\x1b[2J\u202e", allowlist=policies[0], wake_list=policies[1],
            )
        except BaseException as exc:
            end.error = exc
        finally:
            terminals[1 - endpoints.index(end)].put(None)
    threads = [Thread(target=operate, args=(end,), daemon=True) for end in endpoints]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for end in endpoints:
        if hasattr(end, "error"):
            raise end.error
        end.recovered = (end.backend.recover(), end.backend.recover())
        end.order = tuple(end.backend.order)
        end.final_wait = None if end.channel.final_deadline is None else end.channel.final_deadline - now
        end.restart_record = end.backend.active
        end.opened_record = next(row for row in end.backend.fresh_journal().read_committed().records
                                 if row["kind"] == "cross_ceremony_opened")
        end.active = end.backend.active
    return tuple(endpoints)


def seed_activation(journal, opening, pair_id, label, *, local_der=b"local leaf", peer_der=b"peer leaf"):
    """Publish an attended pair whose local material adapter is supplied by the test."""
    from secrets import token_hex

    record = dict(
        opening.scope(), schema_version=1, kind="cross_pair_active", id=f"cross-pair-active-{pair_id}",
        opening_id=opening.opening_id, ceremony_id=token_hex(16), pair_id=pair_id, role=opening.role,
        label=label, local_der_sha256=sha256(local_der).hexdigest(), peer_der=peer_der.hex(),
        peer_sha256=sha256(peer_der).hexdigest(),
        peer_descriptor=dict(v=5, enable_id="bb" * 16, salt="a" * 32, label_hint=label, lan=[], ssh_launcher=""),
        policy=19, allowlist=[], wake_list=[], transcript_receipt_digest="b" * 64,
        human_act=dict(channel="controlling_terminal", rendered_digest="c" * 64),
    )
    result = journal.activate_pair(opening.capability(), record, prepare=lambda _: None)
    assert result.status == "committed", result


class JournalFixture:
    def __enter__(self):
        from tests.test_cross_journal import _OwnedEnableInventory

        self.temp = TemporaryDirectory()
        self.directory = Path(self.temp.name) / "cross"
        self.day = "2026-01-01"
        self.enable = "aa" * 16
        self.anchor = FixtureAnchor()
        self.inventory = _OwnedEnableInventory()
        self.start = approved_start(
            self.enable, 7, "cc" * 16,
            {"listen": [], "root": "/fixture/cross", "store": "keychain", "unit": "none"}, schema_version=2,
        )
        self.restart()
        self.journal.create(self.enable, enable_generation=7, attended=self.start)
        result = self.journal.append(Capability("A", verb="enable"), approved_completion(self.start))
        assert result.status == "committed", result
        return self

    def __exit__(self, *error):
        self.temp.cleanup()

    def restart(self):
        from floati.cross_pair_storage import JournalOpenings

        self.journal = CrossJournal(
            self.directory, storage=TracingStorage(self.directory), utc_day=lambda: self.day,
            anchor_store=self.anchor, enable_inventory=self.inventory,
            enable_approval=lambda phase, body: body == self.start,
            prove_pair_material=lambda record, *, phase: True,
        )
        self.charges = JournalOpenings(self.journal)

    def seed_pair(self, pair_id, label):
        seed_activation(self.journal, self.charges.open("S"), pair_id, label)

    def fill_ordinary_control(self):
        state = project(self.journal.read_committed(require_settled=True).records)
        reserve = closeout_cost(state, headroom=3)
        self.journal.budget = Budget(state.control_bytes + reserve.bytes, state.control_slots + reserve.slots)
        try:
            self.charges.open("S")
        except ProtocolRefusal as exc:
            assert exc.code == "cross_review_capacity", exc
        else:
            raise AssertionError("ordinary capacity still available")

    def is_active(self, pair_id):
        return pair_id in project(self.journal.read_committed(require_settled=True).records).active_pairs
