"""The cross-machine journal's storage layer (dark: no verb imports it).

Publication is a two-phase decision. A writer replaces the checkpoint, syncs
the directory, and only then writes a seal naming the checkpoint's digest. A
reader publishes a checkpoint only when the seal names it; otherwise it
publishes the sealed state the checkpoint records as `previous`. A visible
seal proves the directory sync returned, because the writer creates it only
afterwards. A rename a power loss can still undo is therefore never published.
The next process to hold the exclusive lock completes a barrier that a dead
writer left open.

Successors never replace the journal by rename. Each generation is its own
file, `journal-g<k>.jsonl`, and the checkpoint names it. The checkpoint
rename is the only namespace change a reader depends on. Tail repair
truncates in place under a recorded intent.

Every durable operation goes through one `Storage` object, so a test can name
each durable boundary, kill the writer there and rebuild the directory a power
loss would leave (`tests/cross_journal_harness.py`).
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import stat
import os
import re
import secrets
import sys
import threading
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, ContextManager, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

from .cross_anchor import (
    Anchor, AnchorStore, EnableCreate, Handoff, Narrowing, NarrowingHandoff, OutstandingWrite,
    QuotaMigration, QuotaMigrationPending, sealed_descriptor, stable_descriptor,
)
from .cross_attended import (
    activation_review_item, approved_completion, approved_start, endpoint_bind_spec, endpoint_refusal,
    review_item, validate_endpoint_registration,
)
from .cross_authority import Capability, as_capability, check, check_committed, require
from .cross_authority import row as authority_row
from .cross_coordinates import (
    MAX_INTEGER, POSITION_FIELDS, digest, enc, envelope_id, fold_quota_history, legacy_quota_cutoff, prune_digest, semantic, trust_coordinate,
    trust_genesis,
    successor_quota_sha256,
)
from .cross_delivery import (
    next_sequence, request_commitment, retirable_through, sender_receipt, sender_replay, sender_replay_price,
    validate_outbound, validate_sender_replay,
)
from .cross_gate import DIRECTIONS, GateFloors, GateImage, GateOwner, gate_history, validate_gate_link
from .cross_records import pair_seen_record, priced_frame, validate_pair_activation, validate_pair_revocation
from .cross_reserve import (
    obligations,
    CONTROL_BUDGET,
    CONTROL_KINDS,
    CONTROLLER_KINDS,
    COMPLETION_KINDS,
    COMPLETION_PRICE,
    DATA_BUDGET,
    DATA_KINDS,
    check_trust_reserve,
    Budget,
    admit,
    admit_compaction,
    check_data_scope,
    completion_id,
    delivery_key,
    inbound_result,
    limiting_control,
    project,
    retires,
    retained,
    trust_head,
)
from .errors import IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .jsonl import _durability_failure, _locked_path
from .root import FloatiRoot


CROSS_DIRECTORY = "cross"
CHECKPOINT = "journal.commit"
SEAL = "journal.commit.seal"
LOCK = "journal.lock"
LEGACY_CHECKPOINT_FORMAT = "floati-cross-checkpoint-v1"
CHECKPOINT_FORMAT = "floati-cross-checkpoint-v2"
QUOTA_CHECKPOINT_FORMAT = "floati-cross-checkpoint-v3"
BOUND_QUOTA_FORMAT = "floati-cross-checkpoint-v4"
_MODERN_FORMATS = {CHECKPOINT_FORMAT, QUOTA_CHECKPOINT_FORMAT, BOUND_QUOTA_FORMAT}
_MIGRATION = "legacy-quota-migration"
_ENABLE_ITEMS = ("configuration", "unit", "ballast", "gate", "sentinel")
_ADOPT = "adopt-enable:"
_SENDER_PRUNE = re.compile(r"prune-sender:([0-9a-f]{32}):(S-to-T|T-to-S):([1-9][0-9]{0,15})\Z")
_PRUNE = re.compile(r"prune-receiver:([0-9a-f]{32}):(S-to-T|T-to-S):([1-9][0-9]{0,15})\Z")
SEAL_FORMAT = "floati-cross-seal-v1"
BALLAST = "closeout.ballast"
BALLAST_INVENTORY = "closeout.ballast.inventory"
BALLAST_BYTES = 1024 * 1024
_BALLAST_HEADER = b"floati-cross-ballast-v2\0"
_TEARDOWN_PLAN = "closeout.inventory"
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TEARDOWN_CHECKS = {"unit_absent", "unit_not_loadable", "no_listener", "no_live_endpoint"}
_SENDER_KINDS = {"remote_outbound", "remote_attempt", "remote_transport_attempt"}
MAX_RECORD_BYTES = 65536
MAX_SEQUENCE = 2**53

_JOURNAL_FILE = re.compile(r"journal-g([1-9][0-9]{0,8})\.jsonl\Z")
_TEMPORARY = re.compile(r"\.journal\.commit(\.seal)?\.tmp\Z")
_ENABLE_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_POSITION = ("journal_file", "journal_id", "generation", "through_seq", "head_sha256", "byte_length")
_CHECKPOINT_FIELDS = frozenset(_POSITION) | {"format", "previous", "repair_intent", "compaction_intent"}
_PREVIOUS_FIELDS = frozenset(_POSITION) | {"checkpoint_sha256"}
_REPAIR_FIELDS = frozenset({"offset", "tail_len", "tail_sha256", "complete_frames"})
_COMPACTION_FIELDS = frozenset({
    "successor_file", "successor_journal_id", "successor_generation", "successor_sha256",
    "successor_length", "successor_through_seq", "successor_head_sha256", "reason",
})

KNOWN_KINDS = CONTROL_KINDS | DATA_KINDS | CONTROLLER_KINDS


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def genesis_head(journal_id: str) -> str:
    """The house chain rule: genesis is SHA-256 of the journal id."""

    return _sha256(journal_id.encode("utf-8"))


def line_digest(line: bytes) -> str:
    """The house chain rule: `prev` hashes the exact previous line without LF."""

    return _sha256(line[:-1] if line.endswith(b"\n") else line)


def journal_file(generation: int) -> str:
    return f"journal-g{generation}.jsonl"


@dataclass(frozen=True)
class BarrierObservation:
    operation: str
    object_name: str
    grade: str


class Storage:
    """Every durable operation of the cross journal, in one directory.

    Production does nothing at a boundary. A test subclass records the
    filesystem state each operation leaves and can stop the process there.
    On Darwin a sync is `fsync` followed by `F_FULLFSYNC`; if the second
    fails, `durability` becomes `fsync_only` and power-loss durability on
    this machine is unknown.
    """

    def __init__(self, directory: Union[Path, str], *, barrier_observer=None) -> None:
        self.directory = Path(directory)
        self._barrier_observer = barrier_observer
        self._syncs = 0
        self._full_sync_failures = 0

    def before(self, operation: str, name: str) -> None:
        """A durable operation is about to run."""

    def boundary(self, operation: str, name: str) -> None:
        """A durable operation has just returned."""

    def _path(self, name: str) -> Path:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("a cross journal name is one entry of its directory")
        return self.directory / name

    def read(self, name: str) -> Optional[bytes]:
        try:
            return self._path(name).read_bytes()
        except FileNotFoundError:
            return None

    def names(self) -> List[str]:
        try:
            return sorted(os.listdir(self.directory))
        except FileNotFoundError:
            return []

    def make_directory(self) -> None:
        self.before("mkdir", ".")
        self.directory.mkdir(exist_ok=True)
        descriptor = os.open(self.directory.parent, os.O_RDONLY)
        try:
            self._sync_observed(descriptor, "make_directory", ".")
        finally:
            os.close(descriptor)
        self.boundary("mkdir", ".")

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]

    def append(self, name: str, data: bytes) -> bool:
        self.before("append", name)
        path = self._path(name)
        created = not path.exists()
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            self._write_all(descriptor, data)
        finally:
            os.close(descriptor)
        self.boundary("append", name)
        return created

    def create(self, name: str, data: bytes) -> None:
        self.before("create", name)
        descriptor = os.open(self._path(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            self._write_all(descriptor, data)
        finally:
            os.close(descriptor)
        self.boundary("create", name)

    def truncate(self, name: str, length: int) -> None:
        self.before("truncate", name)
        os.truncate(self._path(name), length)
        self.boundary("truncate", name)

    def sync(self, name: str) -> None:
        self.before("sync", name)
        descriptor = os.open(self._path(name), os.O_RDONLY)
        try:
            self._sync_observed(descriptor, "sync", name)
        finally:
            os.close(descriptor)
        self.boundary("sync", name)

    def sync_directory(self) -> None:
        self.before("sync_directory", ".")
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            self._sync_observed(descriptor, "sync_directory", ".")
        finally:
            os.close(descriptor)
        self.boundary("sync_directory", ".")

    def rename(self, source: str, target: str) -> None:
        self.before("rename", target)
        os.replace(self._path(source), self._path(target))
        self.boundary("rename", target)

    def unlink(self, name: str) -> None:
        self.before("unlink", name)
        os.unlink(self._path(name))
        self.boundary("unlink", name)

    # The platform calls, as attributes so a test can observe them on any host.
    full_sync = sys.platform == "darwin"
    fsync_call = staticmethod(os.fsync)
    fcntl_call = staticmethod(fcntl.fcntl)

    def sync_position(self) -> Tuple[int, int]:
        return self._syncs, self._full_sync_failures

    def sync_grade(self, since: Tuple[int, int]) -> str:
        if not self.full_sync or self._syncs == since[0]:
            return "unknown"
        return "fsync_only" if self._full_sync_failures != since[1] else "full"

    def _sync_observed(self, descriptor: int, operation: str, name: str) -> None:
        if self._barrier_observer is None:
            self._sync_descriptor(descriptor)
            return
        before = self.sync_position()
        try:
            self._sync_descriptor(descriptor)
        except OSError:
            self._barrier_observer(BarrierObservation(operation, name, "failed"))
            raise
        self._barrier_observer(BarrierObservation(operation, name, self.sync_grade(before)))

    def _sync_descriptor(self, descriptor: int) -> None:
        self.fsync_call(descriptor)
        self._syncs += 1
        if self.full_sync:
            try:
                self.fcntl_call(descriptor, getattr(fcntl, "F_FULLFSYNC", 51))
            except OSError:
                self._full_sync_failures += 1


@dataclass(frozen=True)
class CommittedView:
    """What a reader may publish: the sealed prefix and nothing beyond it."""

    state: str
    journal_id: Optional[str]
    generation: int
    through_seq: int
    head_sha256: Optional[str]
    records: Tuple[Dict[str, object], ...]
    pending: bool = False
    historical_durability: str = "unknown"


class RootJournalProvenance(Protocol):
    """Root history and stable exclusion, both outside the journal restore set."""

    def exclude(self, root: FloatiRoot) -> ContextManager[None]: ...

    def read(self, root: FloatiRoot) -> Optional[str]: ...

    def mark_established(self, root: FloatiRoot) -> bool: ...


class TeardownEffects(Protocol):
    """Qualified observations and token-scoped effects under the journal lock."""

    def inventory(self, start: Mapping[str, object], records: Sequence[Mapping[str, object]]) -> Mapping[str, str]: ...

    def read(self, item: str, start: Mapping[str, object], proof: str) -> Tuple[str, Optional[str]]: ...

    def remove(self, item: str, start: Mapping[str, object], proof: str) -> None: ...

    def checks(self, start: Mapping[str, object]) -> Mapping[str, bool]: ...


@dataclass
class TeardownProgress:
    """Observations from this invocation under teardown exclusion."""

    inventory: Optional[Dict[str, dict]] = None
    checks: Dict[str, Optional[bool]] = field(default_factory=lambda: dict.fromkeys(sorted(_TEARDOWN_CHECKS)))

    def evidence(self):
        return {"inventory": None if self.inventory is None else list(self.inventory.values()),
                "checks": dict(self.checks)}


@dataclass(frozen=True)
class AppendResult:
    """`committed` is published. `failed` can never become committed.

    `indeterminate` means the checkpoint may be visible but not yet durable;
    the next maintenance pass decides it, and `resolve` reports which way.
    `committed_unpublished` means the checkpoint is durable and the seal
    failed. Maintenance publishes it before a caller can act on the record.
    `deferred` names the delivery waiting for ordinary repair room.
    """

    status: str
    record_id: str
    seq: Optional[int] = None
    code: Optional[str] = None
    delivery_id: Optional[str] = None
    operation: Optional[str] = None
    limiting_resource: Optional[str] = None
    barrier_durability: str = "unknown"
    journal_publication: Optional[str] = None
    independent_denial: str = "unknown"
    delivery_seq: Optional[int] = None
    outcome: Optional[dict] = None


@dataclass(frozen=True, eq=False)
class AuthenticatedOpen:
    record_frame: bytes
    stream: object
    pair_id: str
    permit_output: Optional[Callable[[str], bool]] = None
    enqueue_output: Optional[Callable[[bytes], bool]] = None

    def capability(self, verb: str) -> Capability:
        return Capability("I", self.pair_id, verb=verb, session=self)


@dataclass(frozen=True, eq=False)
class LocalDial:
    capability: Capability
    sender: str
    outbound_frame: bytes
    attempt_frame: bytes


@dataclass(frozen=True)
class GatePairProjection:
    owner: GateOwner
    activation: Mapping[str, object]
    counters: Mapping[str, GateFloors]
    peer_address: Optional[Mapping[str, object]]


@dataclass(frozen=True)
class GateProjection:
    storage: object
    image: GateImage
    pairs: Tuple[GatePairProjection, ...]


class _GateStorage:
    """Limit the gate adapter to the same lock-held file transaction."""

    def __init__(self, journal, lease, writable, active_keys):
        self._journal, self._lease, self._writable = journal, lease, writable
        self._active_keys = active_keys

    def _check(self, *names, write=False):
        if (self._journal._exclusive_token() is not self._lease or (write and not self._writable)
            or any(name not in ("gate-state", ".gate-state.tmp") for name in names)):
            raise self._journal._gate_refusal()
        return self._journal.storage

    def read(self, name):
        return self._check(name).read(name)

    def create(self, name, payload):
        if name != ".gate-state.tmp":
            raise self._journal._gate_refusal()
        storage = self._check(name, write=True)
        current = storage.read("gate-state")
        if current is None:
            raise self._journal._gate_refusal()
        GateImage.decode(current).check_counter_update(GateImage.decode(payload), self._active_keys)
        storage.create(name, payload)

    def sync(self, name):
        self._check(name, write=True).sync(name)

    def rename(self, source, target):
        if (source, target) != (".gate-state.tmp", "gate-state"):
            raise self._journal._gate_refusal()
        storage = self._check(source, target, write=True)
        current, staged = storage.read(target), storage.read(source)
        if current is None or staged is None:
            raise self._journal._gate_refusal()
        GateImage.decode(current).check_counter_update(GateImage.decode(staged), self._active_keys)
        storage.rename(source, target)

    def sync_directory(self):
        self._check(write=True).sync_directory()


@dataclass(frozen=True, eq=False)
class LocalOutput:
    opened: AuthenticatedOpen
    capability: Capability
    sender: str
    outbound_frame: bytes
    attempt_frame: bytes


_ABSENT = CommittedView("absent", None, 0, 0, None, ())


def _corrupt(detail: str) -> IntegrityFailure:
    return IntegrityFailure("cross_journal_corrupt", detail)


def _frame(record: Mapping[str, object]) -> bytes:
    try:
        line = encode_frame(record)
    except FrameError as exc:
        raise ProtocolRefusal(exc.code, exc.detail) from exc
    if len(line) > MAX_RECORD_BYTES:
        raise ProtocolRefusal("record_too_large", f"record is {len(line)} bytes")
    return line


def verify_prefix(data: bytes, journal_id: str) -> Tuple[List[Dict[str, object]], str]:
    """Decode one exact chained prefix; return its records and head.

    Pure: it reads nothing and writes nothing, so every reader can use it.
    """

    if data and not data.endswith(b"\n"):
        raise _corrupt("the committed prefix does not end at a frame boundary")
    try:
        decoded = decode_frames(data)
    except FrameError as exc:
        raise _corrupt(exc.detail) from exc
    head = genesis_head(journal_id)
    records: List[Dict[str, object]] = []
    seen = set()
    activations = {}
    for index, (record, line) in enumerate(zip(decoded, data.split(b"\n")), start=1):
        if not isinstance(record, dict) or encode_frame(record) != line + b"\n":
            raise _corrupt(f"frame {index} is not one canonical object")
        if type(record.get("seq")) is not int or record["seq"] != index or record.get("prev") != head:
            raise _corrupt(f"frame {index} does not chain")
        if record.get("kind") not in KNOWN_KINDS or record.get("id") in seen:
            raise _corrupt(f"frame {index} has an unknown kind or a repeated id")
        try:
            if record["kind"] == "cross_journal_link":
                if not _integer(record.get("schema_version"), 0, 2):
                    raise _corrupt("the journal link version is unsupported")
                if record["schema_version"] == 2:
                    if index != 1:
                        raise _corrupt("the gate maxima are outside the successor link")
                    validate_gate_link(semantic(record))
            if record["kind"] == "remote_endpoint_registered" and record.get("schema_version") != 0:
                validate_endpoint_registration(semantic(record))
            if record["kind"] == "remote_sender_replay":
                if index > 999999:
                    raise _corrupt("the replay sequence exceeds its priced width")
                validate_sender_replay(semantic(record))
            if record["kind"] == "remote_outbound" and record.get("schema_version") == 2:
                validate_outbound(semantic(record))
        except (KeyError, TypeError, ProtocolRefusal) as exc:
            raise _corrupt("a closed cross-journal record is malformed") from exc
        if ((record["kind"] == "cross_pair_active" and record.get("schema_version") != 0)
            or (record["kind"] == "cross_optin" and record.get("schema_version") == 2)):
            try:
                body = semantic(record)
                frame = priced_frame(body, index, head, trust=(record["prev_trust_digest"], record["trust_count"]))
                if frame != line + b"\n":
                    raise _corrupt("the attended activation does not match its closed frame")
                if body["kind"] == "cross_pair_active":
                    activations[body["id"]] = body
            except (KeyError, TypeError, ProtocolRefusal) as exc:
                raise _corrupt("an attended trust step is malformed") from exc
        if record["kind"] == "cross_pair_seen" and record.get("schema_version") != 0:
            activation = activations.get(str(record.get("activation_id")))
            if activation is None or _frame(semantic(record)) != _frame(pair_seen_record(activation)):
                raise _corrupt("the pair observation does not match its activation")
        if record["kind"] == "cross_journal_tail_repaired":
            try:
                priced_frame(semantic(record), index, head)
            except ProtocolRefusal as exc:
                raise _corrupt("the repair row is outside the single-append domain") from exc
        if record["kind"] == "cross_pair_revoked" and (activation := activations.get(f"cross-pair-active-{record.get('pair_id')}")):
            try:
                body = semantic(record)
                validate_pair_revocation(activation, body)
                frame = priced_frame(body, index, head, trust=(record["prev_trust_digest"], record["trust_count"]))
                if frame != line + b"\n":
                    raise _corrupt("the attended pair revocation does not match its closed frame")
            except (KeyError, TypeError, ProtocolRefusal) as exc:
                raise _corrupt("the attended pair revocation is malformed") from exc
        seen.add(record.get("id"))
        records.append(record)
        head = line_digest(line)
    trust_coordinate(records)
    return records, head


def _integer(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _position_valid(value: Mapping[str, object]) -> bool:
    match = _JOURNAL_FILE.fullmatch(str(value.get("journal_file")))
    return (
        match is not None
        and _integer(value.get("generation"), 1, 10**9)
        and int(match.group(1)) == value.get("generation")
        and isinstance(value.get("journal_id"), str)
        and str(value["journal_id"]).endswith(f"-g{value['generation']}")
        and _integer(value.get("through_seq"), 0, MAX_SEQUENCE)
        and _integer(value.get("byte_length"), 0, 64 * 1024 * 1024)
        and isinstance(value.get("head_sha256"), str)
        and _SHA256.fullmatch(str(value["head_sha256"])) is not None
    )


def _bound_quota_valid(quota) -> bool:
    if not isinstance(quota, dict):
        return False
    source = quota.get("source")
    if not isinstance(source, dict) or set(source) != _PREVIOUS_FIELDS - {"journal_file"}:
        return False
    position = dict(source, journal_file=f"journal-g{source['generation']}.jsonl")
    if not _position_valid(position) or not _SHA256.fullmatch(str(source["checkpoint_sha256"])):
        return False
    if quota.get("mode") == "legacy_blocked_through":
        if set(quota) != {"mode", "day", "source"}:
            return False
    elif quota.get("mode") == "exact":
        if set(quota) != {"mode", "day", "count", "source"}:
            return False
        if quota["day"] is None:
            return type(quota["count"]) is int and quota["count"] == 0
        if not _integer(quota["count"], 1, 32):
            return False
    else:
        return False
    try:
        return date.fromisoformat(quota["day"]).isoformat() == quota["day"]
    except (TypeError, ValueError):
        return False


def parse_checkpoint(data: bytes) -> Dict[str, object]:
    try:
        decoded = decode_frames(data)
    except FrameError as exc:
        raise _corrupt(exc.detail) from exc
    if len(decoded) != 1 or not isinstance(decoded[0], dict):
        raise _corrupt("the checkpoint is not one object")
    checkpoint = decoded[0]
    if encode_frame(checkpoint) != data or frozenset(checkpoint) - {"completion_repair", "opening_sha256", "quota"} != _CHECKPOINT_FIELDS:
        raise _corrupt("the checkpoint is not one canonical object")
    if checkpoint["format"] not in _MODERN_FORMATS | {LEGACY_CHECKPOINT_FORMAT} or not _position_valid(checkpoint):
        raise _corrupt("the checkpoint position is malformed")
    if "opening_sha256" in checkpoint and (
        checkpoint["format"] not in _MODERN_FORMATS
        or _SHA256.fullmatch(str(checkpoint["opening_sha256"])) is None
    ):
        raise _corrupt("the generation-opening reference is malformed")
    quota = checkpoint.get("quota")
    if checkpoint["format"] == QUOTA_CHECKPOINT_FORMAT:
        if not isinstance(quota, dict):
            raise _corrupt("the quota checkpoint is missing its projection")
        mode = quota.get("mode")
        if mode == "legacy_blocked_through":
            cut = quota.get("predecessor")
            if (set(quota) != {"mode", "day", "predecessor"} or not isinstance(cut, dict)
                or set(cut) != _PREVIOUS_FIELDS or not _position_valid(cut)
                or not isinstance(cut["checkpoint_sha256"], str) or not _SHA256.fullmatch(cut["checkpoint_sha256"])):
                raise _corrupt("the last possible legacy opening has no exact predecessor")
        elif mode == "charged":
            if set(quota) != {"mode", "day", "count"} or not _integer(quota["count"], 1, 32):
                raise _corrupt("the charged bucket is malformed")
        else:
            raise _corrupt("the quota projection has an unknown mode")
        try:
            if date.fromisoformat(quota["day"]).isoformat() != quota["day"]:
                raise ValueError("noncanonical civil date")
        except (TypeError, ValueError) as exc:
            raise _corrupt("the quota projection has an invalid UTC date") from exc
    elif checkpoint["format"] == BOUND_QUOTA_FORMAT:
        if not _bound_quota_valid(quota):
            raise _corrupt("the successor quota projection is malformed")
    elif "quota" in checkpoint:
        raise _corrupt("a legacy checkpoint cannot carry a newer quota projection")
    previous = checkpoint["previous"]
    if previous is not None and (
        not isinstance(previous, dict)
        or frozenset(previous) != _PREVIOUS_FIELDS
        or not _position_valid(previous)
        or _SHA256.fullmatch(str(previous["checkpoint_sha256"])) is None
    ):
        raise _corrupt("the checkpoint's previous state is malformed")
    repair = checkpoint["repair_intent"]
    if repair is not None and (
        not isinstance(repair, dict) or frozenset(repair) != _REPAIR_FIELDS
        or not _integer(repair["offset"], 0, checkpoint["byte_length"])
        or repair["offset"] != checkpoint["byte_length"]
        or not _integer(repair["tail_len"], 1, 65536)
        or not _integer(repair["complete_frames"], 0, 1)
        or not isinstance(repair["tail_sha256"], str) or not _SHA256.fullmatch(repair["tail_sha256"])
    ):
        raise _corrupt("the repair intent is malformed")
    compaction = checkpoint["compaction_intent"]
    if compaction is not None and (not isinstance(compaction, dict) or (
        frozenset(compaction) != (_COMPACTION_FIELDS | {"quota_sha256"} if checkpoint["format"] == BOUND_QUOTA_FORMAT
                                  else _COMPACTION_FIELDS)
        or ("quota_sha256" in compaction and not _SHA256.fullmatch(str(compaction["quota_sha256"])))
    )):
        raise _corrupt("the compaction intent is malformed")
    if repair is not None and compaction is not None:
        raise _corrupt("a checkpoint carries two intents")
    if "completion_repair" in checkpoint:
        key = checkpoint["completion_repair"]
        if compaction is not None or not isinstance(key, str) or _SHA256.fullmatch(key) is None:
            raise _corrupt("the completion repair marker is malformed")
    return checkpoint


def _position(value: Mapping[str, object]) -> Dict[str, object]:
    return {field: value[field] for field in _POSITION}


class CrossJournal:
    """One root's cross journal: writer, reader, recovery and compaction."""

    _fixture_mode = False

    def __init__(
        self,
        directory: Union[Path, str],
        *,
        storage: Optional[Storage] = None,
        lock_relative: str = CROSS_DIRECTORY + "/" + LOCK,
        budget: Budget = CONTROL_BUDGET,
        data_budget: Budget = DATA_BUDGET,
        utc_day=None,
        anchor_store: Optional[AnchorStore] = None,
        hold_rejection=None,
        hold_source=None,
        enable_approval=None,
        legacy_exclusion=None,
        authenticate_open=None,
        authorize_local_send=None,
        fresh_root=None,
        quota_store=None,
        sender_dependencies=None,
        retain_sender_source=None,
        missing_exclusion=None,
        quota_inventory=None,
        enable_inventory=None,
        dial_prerequisites=None,
        authorize_quota_transfer=None,
        reserve_quota_anchor=None,
        authenticate_quota_history=None,
        confirm_pair_absent=None,
        prove_pair_material=None,
        abandoned_ceremony=None,
        verify_endpoint=None,
    ) -> None:
        self.directory = Path(directory)
        self.storage = storage if storage is not None else Storage(self.directory)
        self.lock_relative = lock_relative
        self.budget = budget
        self.data_budget = data_budget
        self._anchor_store = anchor_store
        self._hold_rejection = hold_rejection
        self._hold_source = hold_source
        self._enable_approval = enable_approval
        self._legacy_exclusion = legacy_exclusion
        self._authenticate_open = authenticate_open
        self._authorize_local_send = authorize_local_send
        self._fresh_root = fresh_root
        self._quota_store = quota_store
        self._sender_dependencies = sender_dependencies
        self._retain_sender_source = retain_sender_source
        self._missing_exclusion = missing_exclusion
        self._quota_inventory = quota_inventory
        self._enable_inventory = enable_inventory
        self._dial_prerequisites = dial_prerequisites
        self._authorize_quota_transfer = authorize_quota_transfer
        self._reserve_quota_anchor = reserve_quota_anchor
        self._authenticate_quota_history = authenticate_quota_history
        self._confirm_pair_absent = confirm_pair_absent
        self._prove_pair_material = prove_pair_material
        self._abandoned_ceremony = abandoned_ceremony
        self._verify_endpoint = verify_endpoint
        self._lock_context = threading.local()
        self._root_provenance = None
        self._enable_ballast_scope = None
        self._synced_enable_ballast = None
        self._pending_dials = set()
        self._live_streams = {}
        self._pending_outputs = set()
        self._utc_day = utc_day if utc_day is not None else lambda: datetime.now(timezone.utc).date().isoformat()
        # (originating writer, writer whose authority the effect used, kind or action)
        self.census: List[Tuple[str, str, str]] = []
        self._origin = "M"

    @classmethod
    def for_root(cls, root: FloatiRoot, *, provenance: Optional[RootJournalProvenance] = None,
                 **options) -> "CrossJournal":
        """Bind qualified root evidence to an already validated tenant home.

        The provider's exclude(root) uses an established exclusion outside the
        journal restore set. Under it, read(root) returns never_established,
        established, or uncertainty. mark_established(root) durably records
        prior issuance before the first possible journal creation.
        """
        if not isinstance(root, FloatiRoot):
            raise TypeError("cross journal requires a validated root")
        directory = root.tenant_home / CROSS_DIRECTORY
        if root.resolve_relative(CROSS_DIRECTORY) != directory:
            raise cls._provenance_refusal()
        journal = cls(directory, **options)
        if provenance is not None:
            journal._root_provenance = (root, provenance)
        return journal

    @staticmethod
    def _provenance_refusal():
        return ProtocolRefusal("cross_publication_pending", "the root's independent journal provenance is unproved")

    def _check_root_binding(self, root):
        if (root.resolve_relative(CROSS_DIRECTORY) != self.directory or self.directory.is_symlink()
            or root.tenant_home.is_symlink()):
            raise self._provenance_refusal()

    @contextmanager
    def _lock(self, *, exclusive: bool, _open_handle=None):
        with _locked_path(self.directory / LOCK, exclusive=exclusive, relative=self.lock_relative,
                          _open_handle=_open_handle):
            self._lock_context.token = object() if exclusive else None
            try:
                yield
            finally:
                self._lock_context.token = None

    def _exclusive_token(self):
        return getattr(self._lock_context, "token", None)

    def _root_providers_present(self) -> bool:
        return self._anchor_store is not None and callable(self._enable_approval)

    def _require_root_providers(self):
        if not self._fixture_mode and not self._root_providers_present():
            raise ProtocolRefusal("cross_publication_pending", "the root needs independent store and approval providers")

    # -- reading ------------------------------------------------------------

    def _load(self) -> Optional[Tuple[Dict[str, object], bytes]]:
        data = self.storage.read(CHECKPOINT)
        if data is None:
            return None
        return parse_checkpoint(data), data

    def _seal(self) -> Optional[str]:
        """The checkpoint digest the seal names, or None for any other content."""

        data = self.storage.read(SEAL)
        if data is None:
            return None
        try:
            decoded = decode_frames(data)
        except FrameError:
            return None
        if (
            len(decoded) == 1
            and isinstance(decoded[0], dict)
            and frozenset(decoded[0]) == {"format", "checkpoint_sha256"}
            and decoded[0]["format"] == SEAL_FORMAT
            and _SHA256.fullmatch(str(decoded[0]["checkpoint_sha256"])) is not None
        ):
            return str(decoded[0]["checkpoint_sha256"])
        return None

    def _established(self) -> bool:
        return self.storage.read(SEAL) is not None or any(
            _JOURNAL_FILE.fullmatch(name) for name in self.storage.names()
        )

    def _verified(self, position: Mapping[str, object]) -> Tuple[List[Dict[str, object]], bytes]:
        data = self.storage.read(str(position["journal_file"])) or b""
        length = int(position["byte_length"])
        if len(data) < length:
            raise _corrupt("the journal is shorter than its checkpoint")
        records, head = verify_prefix(data[:length], str(position["journal_id"]))
        if head != position["head_sha256"] or len(records) != position["through_seq"]:
            raise _corrupt("the checkpoint is not an exact chained prefix of its journal")
        return records, data

    def _missing_view(self, require_settled: bool, *, uncertain: bool = False,
                      root_excluded: bool = False) -> CommittedView:
        try:
            absent = not uncertain and self._independently_new(root_excluded=root_excluded)
        except (OSError, ProtocolRefusal):
            absent = False
        if absent:
            return _ABSENT
        if require_settled:
            raise ProtocolRefusal("cross_publication_pending", "missing journal evidence does not establish absence")
        return CommittedView("pending", None, 0, 0, None, (), True)

    def _independently_new(self, *, root_excluded: bool = False) -> bool:
        if self._anchor_store is None or self.storage.read(CHECKPOINT) is not None or self._established():
            return False
        if self._root_provenance is None:
            if not self._fixture_mode and self._quota_store is None:
                return False
            fresh = self._fresh_root is not None and self._fresh_root() is True
            return fresh and self._observed_store() is None and self._observed_quota() is None
        if not root_excluded or self._quota_store is None:
            return False
        root, source = self._root_provenance
        anchor, migration = self._observed_store(), self._observed_quota()
        return anchor is None and migration is None and source.read(root) == "never_established"

    @contextmanager
    def _read_lock(self, *, exclusive=False):
        """Observe the established inode without creating a lock as a read effect."""
        path = self.directory / LOCK
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            yield "absent"
            return
        except OSError:
            yield "uncertain"
            return
        with handle, self._lock(exclusive=exclusive, _open_handle=lambda: handle):
            try:
                coherent = os.path.samestat(os.fstat(handle.fileno()), path.stat())
            except OSError:
                coherent = False
            yield "locked" if coherent else "uncertain"

    def _missing_with_exclusion(self, lock, require_settled):
        if lock != "absent" or self._missing_exclusion is None:
            return self._missing_view(require_settled, uncertain=True)
        try:
            with self._missing_exclusion():
                return self._missing_view(require_settled)
        except OSError:
            return self._missing_view(require_settled, uncertain=True)

    def read_committed(self, *, require_settled: bool = False) -> CommittedView:
        """Expose the published prefix only. Never syncs, never repairs."""

        with ExitStack() as exclusion:
            if self._root_provenance is not None:
                root, source = self._root_provenance
                try:
                    exclusion.enter_context(source.exclude(root))
                    self._check_root_binding(root)
                except (OSError, ProtocolRefusal):
                    return self._missing_view(require_settled, uncertain=True)
            return self._read_committed(require_settled)

    def inspect_disk(self) -> CommittedView:
        """Inspect sealed local history without granting settled admission evidence."""
        return replace(self._read_committed(False, disk_only=True), state="disk_only", pending=True)

    def _read_committed(self, require_settled: bool, *, disk_only=False) -> CommittedView:
        with self._read_lock() as lock:
            if lock != "locked":
                if disk_only:
                    return _ABSENT
                if self._root_provenance is not None:
                    return self._missing_view(require_settled, uncertain=lock != "absent", root_excluded=True)
                return self._missing_with_exclusion(lock, require_settled)
            loaded = self._load()
            if loaded is None:
                if disk_only:
                    return _ABSENT
                return self._missing_view(require_settled, root_excluded=self._root_provenance is not None)
            checkpoint, raw = loaded
            seal = self._seal()
            previous = checkpoint["previous"]
            def pending_refusal():
                return ProtocolRefusal(
                    "cross_publication_pending",
                    "the visible checkpoint awaits its durability barrier; run maintenance",
                )

            if seal == _sha256(raw):
                position, pending = _position(checkpoint), False
            elif previous is not None and seal == previous["checkpoint_sha256"]:
                position, pending = _position(previous), True
            elif previous is None and seal is None:
                if require_settled:
                    raise pending_refusal()
                return CommittedView(
                    "pending", str(checkpoint["journal_id"]), int(checkpoint["generation"]), 0, None, (), True
                )
            else:
                raise pending_refusal()
            records, data = self._verified(position)
            pending = (
                pending
                or (not disk_only and (self._anchor_pending(checkpoint, raw, records) or self._quota_pending(checkpoint, data)))
                or checkpoint["repair_intent"] is not None
                or checkpoint["compaction_intent"] is not None
                or "completion_repair" in checkpoint
                or len(data) > position["byte_length"]
                or self._partial_disposal_pending(records)
            )
            if pending and require_settled:
                raise pending_refusal()
            return CommittedView(
                "pending" if pending else "published",
                str(position["journal_id"]),
                int(position["generation"]),
                int(position["through_seq"]),
                str(position["head_sha256"]),
                tuple(records),
                pending,
            )

    @staticmethod
    def _partial_disposal_pending(records) -> bool:
        start = next((index for index in range(len(records) - 1, -1, -1)
                      if records[index].get("phase") == "enable_started"), None)
        if start is None or records[start].get("schema_version") not in (1, 2):
            return False
        phases = {row.get("phase") for row in records[start + 1:]}
        return "disable_started" in phases and not phases.intersection({"enable_complete", "disable_complete"})

    # -- the checkpoint barrier ---------------------------------------------

    def _write_seal(self, digest: str) -> None:
        temporary = "." + SEAL + ".tmp"
        if self.storage.read(temporary) is not None:
            self.storage.unlink(temporary)
        self.storage.create(temporary, encode_frame({"format": SEAL_FORMAT, "checkpoint_sha256": digest}))
        self.storage.sync(temporary)
        self.storage.rename(temporary, SEAL)

    def _commit_checkpoint(self, checkpoint: Mapping[str, object], stage: Optional[dict] = None) -> bytes:
        """Replace, sync the directory, then seal, tracking failure outcomes."""

        payload = encode_frame(dict(checkpoint))
        parse_checkpoint(payload)
        temporary = "." + CHECKPOINT + ".tmp"
        if self.storage.read(temporary) is not None:
            self.storage.unlink(temporary)
        self.storage.create(temporary, payload)
        self.storage.sync(temporary)
        if stage is not None:
            stage["failure_status"] = "indeterminate"
        self.storage.rename(temporary, CHECKPOINT)
        self.storage.sync_directory()
        if stage is not None:
            stage["failure_status"] = "committed_unpublished"
        self._write_seal(_sha256(payload))
        return payload

    @staticmethod
    def _previous(checkpoint: Mapping[str, object], raw: bytes) -> Dict[str, object]:
        return dict(_position(checkpoint), checkpoint_sha256=_sha256(raw))

    @staticmethod
    def _carry_opening(checkpoint: Mapping[str, object], raw: bytes) -> dict:
        carried = dict(checkpoint)
        if carried["format"] in _MODERN_FORMATS:
            carried.setdefault("opening_sha256", _sha256(raw))
        return carried

    @staticmethod
    def _anchor_coordinate(checkpoint, raw, records) -> Optional[Anchor]:
        trust = trust_coordinate(records) if checkpoint["format"] in _MODERN_FORMATS else None
        if trust is None:
            return None
        return Anchor(trust, checkpoint["generation"], checkpoint.get("opening_sha256", _sha256(raw)))

    def _observed_store(self):
        if self._anchor_store is None:
            return None
        try:
            value = self._anchor_store.read()
        except OSError as exc:
            raise ProtocolRefusal("cross_trust_store_pending", "the independent store outcome is unavailable") from exc
        if isinstance(value, OutstandingWrite):
            raise ProtocolRefusal("cross_trust_store_pending", "an independent store transition is still outstanding")
        return value

    @staticmethod
    def _quota_refusal():
        return ProtocolRefusal("cross_quota_cutoff_unproved", "the sealed quota has no matching independent witness")

    def _quota_slot(self):
        if self._quota_store is None:
            return None
        try:
            return self._quota_store.read()
        except OSError as exc:
            raise self._quota_refusal() from exc

    def _observed_quota(self):
        value = self._quota_slot()
        if value is None or isinstance(value, QuotaMigration):
            return value
        raise self._quota_refusal()

    def _quota_reference_matches(self, witness, checkpoint, data):
        position = self._position_key(checkpoint)
        opening_length = witness.successor[-1]
        controller_raw = self.storage.read(f"journal-quota-{witness.token}.commit")
        if controller_raw is None:
            return False
        controller = parse_checkpoint(controller_raw)
        intent = controller["compaction_intent"]
        if (
            controller["format"] != BOUND_QUOTA_FORMAT or intent is None or intent["reason"] != _MIGRATION
            or controller["previous"] is None
            or self._position_key(controller) != witness.predecessor
            or self._position_key(controller["previous"]) != witness.predecessor
            or controller["previous"]["checkpoint_sha256"] != witness.predecessor_checkpoint
            or controller["quota"] != checkpoint["quota"]
            or intent["quota_sha256"] != witness.quota_sha256
            or successor_quota_sha256(checkpoint["quota"]) != witness.quota_sha256
            or self._successor_position(intent) != witness.successor
            or intent["successor_sha256"] != witness.successor_sha256
            or position[:3] != witness.successor[:3] or checkpoint["byte_length"] < opening_length
            or _sha256(data[:opening_length]) != witness.successor_sha256
        ):
            return False
        digest = _sha256(controller_raw)
        if self.storage.read(f"journal-proof-{digest}.seal") != encode_frame({
            "format": SEAL_FORMAT, "checkpoint_sha256": digest,
        }):
            return False
        opening = self._successor_checkpoint(controller, controller_raw)
        opening_digest = _sha256(encode_frame(opening))
        if "opening_sha256" in checkpoint:
            return checkpoint["opening_sha256"] == opening_digest
        return checkpoint == opening

    def _quota_pending(self, checkpoint, data) -> bool:
        try:
            witness = self._observed_quota()
            if checkpoint["format"] != BOUND_QUOTA_FORMAT:
                return witness is not None
            if witness is not None:
                current = self._observed_store()
                return (
                    not self._quota_reference_matches(witness, checkpoint, data)
                    or (current is not None and not self._anchored_quota_matches(checkpoint, data, anchor=current))
                )
            return not self._anchored_quota_matches(checkpoint, data)
        except (OSError, ProtocolRefusal, IntegrityFailure):
            return True

    def _anchored_quota_matches(self, checkpoint, data, *, anchor=None) -> bool:
        if anchor is None:
            anchor = self._observed_store()
        if isinstance(anchor, EnableCreate):
            anchor = anchor.initial
        elif isinstance(anchor, Narrowing):
            anchor = anchor.prior
        if (
            not isinstance(anchor, Anchor) or anchor.generation != checkpoint["generation"]
            or anchor.opening_sha256 != checkpoint.get("opening_sha256", _sha256(encode_frame(checkpoint)))
        ):
            return False
        raw = self.storage.read(f"journal-proof-{anchor.opening_sha256}.commit")
        seal = self.storage.read(f"journal-proof-{anchor.opening_sha256}.seal")
        if raw is None or _sha256(raw) != anchor.opening_sha256 or seal != encode_frame({
            "format": SEAL_FORMAT, "checkpoint_sha256": anchor.opening_sha256,
        }):
            return False
        opening = parse_checkpoint(raw)
        if (opening["format"] != BOUND_QUOTA_FORMAT or opening["quota"] != checkpoint["quota"]
            or opening["journal_id"] != checkpoint["journal_id"] or opening["journal_file"] != checkpoint["journal_file"]
            or opening["byte_length"] > checkpoint["byte_length"]):
            return False
        self._verified(opening)
        return True

    @staticmethod
    def _successor_position(intent) -> tuple:
        return (intent["successor_file"], intent["successor_journal_id"], intent["successor_generation"],
                intent["successor_through_seq"], intent["successor_head_sha256"], intent["successor_length"])

    def _quota_controller(self, checkpoint, raw, quota, intent):
        return dict(_position(checkpoint), format=BOUND_QUOTA_FORMAT, previous=self._previous(checkpoint, raw),
                    opening_sha256=checkpoint.get("opening_sha256", _sha256(raw)), quota=quota,
                    repair_intent=None, compaction_intent=dict(intent, quota_sha256=successor_quota_sha256(quota)))

    def _finish_quota_migration(self, witness, checkpoint, raw):
        controller_raw = self.storage.read(f"journal-quota-{witness.token}.commit")
        predecessor_raw = self.storage.read(f"journal-proof-{witness.predecessor_checkpoint}.commit")
        predecessor_seal = self.storage.read(f"journal-proof-{witness.predecessor_checkpoint}.seal")
        if (self._observed_store() is not None or controller_raw is None or predecessor_raw is None
            or _sha256(predecessor_raw) != witness.predecessor_checkpoint or predecessor_seal != encode_frame({
                "format": SEAL_FORMAT, "checkpoint_sha256": witness.predecessor_checkpoint,
            })):
            raise self._quota_refusal()
        predecessor = parse_checkpoint(predecessor_raw)
        controller = parse_checkpoint(controller_raw)
        if (
            self._position_key(predecessor) != witness.predecessor
            or controller["format"] != BOUND_QUOTA_FORMAT
            or controller["quota"]["source"] != sealed_descriptor(witness.predecessor, witness.predecessor_checkpoint)
            or (raw != predecessor_raw and raw != controller_raw)
        ):
            raise self._quota_refusal()
        rows, predecessor_data = self._verified(predecessor)
        if len(predecessor_data) != predecessor["byte_length"]:
            raise self._quota_refusal()
        target = self._quota_target(witness.successor[1], witness.successor[2])
        intent, _ = self._successor(predecessor, rows, _MIGRATION, target_enable=target,
                                    quota_projection=controller["quota"], expected_digest=witness.successor_sha256)
        if (self._successor_position(intent) != witness.successor or intent["successor_sha256"] != witness.successor_sha256
            or successor_quota_sha256(controller["quota"]) != witness.quota_sha256
            or controller != self._quota_controller(predecessor, predecessor_raw, controller["quota"], intent)):
            raise self._quota_refusal()
        if raw != controller_raw or self._seal() != _sha256(controller_raw):
            if self._seal() != witness.predecessor_checkpoint:
                raise self._quota_refusal()
            self._commit_checkpoint(controller)
        self._keep_store_predecessor(controller_raw)
        opening, sealed = self._finish_compaction(controller, controller_raw)
        self._keep_store_predecessor(sealed)
        return opening, sealed

    def _anchor_pending(self, checkpoint, raw, records, *, inert=False) -> bool:
        if not self._fixture_mode and not self._root_providers_present():
            return True
        if self._anchor_store is None:
            return False
        try:
            current = self._observed_store()
        except (OSError, ProtocolRefusal):
            return True
        if current is None and (inert or self._fixture_mode) and "disable_started" in project(records).phases:
            return False
        if not self._fixture_mode and not inert:
            start = next((row for row in reversed(records) if row.get("phase") == "enable_started"), None)
            if start is None or start.get("schema_version") not in (1, 2):
                return True
        return current != self._anchor_coordinate(checkpoint, raw, records)

    @staticmethod
    def _anchor_refusal():
        return ProtocolRefusal("cross_trust_anchor_mismatch", "the store and the sealed trust coordinate do not agree")

    def _replace_anchor(self, expected, successor) -> None:
        try:
            if self._anchor_store.compare_and_set(expected, successor) and self._observed_store() == successor:
                return
        except OSError as exc:
            raise self._anchor_refusal() from exc
        raise self._anchor_refusal()

    @staticmethod
    def _position_key(position) -> tuple:
        return tuple(position[field] for field in _POSITION)

    @staticmethod
    def _narrowing(record: Mapping[str, object]) -> bool:
        return record["kind"] == "cross_pair_revoked" or (
            record["kind"] == "cross_optin" and record.get("phase") in {"disable_started", "disable_complete"}
        )

    def _keep_store_predecessor(self, raw: bytes) -> None:
        digest = _sha256(raw)
        if self._seal() != digest:
            raise self._anchor_refusal()
        for suffix, value in (("commit", raw), ("seal", self.storage.read(SEAL))):
            name = f"journal-proof-{digest}.{suffix}"
            existing = self.storage.read(name)
            if existing is None:
                self.storage.create(name, value)
            elif existing != value:
                raise _corrupt("a retained store predecessor has conflicting bytes")
            self.storage.sync(name)
        self.storage.sync_directory()

    def _prove_narrowing_predecessor(self, intent: Narrowing, records) -> int:
        digest = intent.predecessor_checkpoint
        raw = self.storage.read(f"journal-proof-{digest}.commit")
        seal = self.storage.read(f"journal-proof-{digest}.seal")
        if raw is None:
            live = self.storage.read(CHECKPOINT)
            if live is not None and _sha256(live) == digest and self._seal() == digest:
                self._keep_store_predecessor(live)
                raw, seal = live, self.storage.read(SEAL)
        if raw is None or _sha256(raw) != digest or seal != encode_frame({
            "format": SEAL_FORMAT, "checkpoint_sha256": digest,
        }):
            raise self._anchor_refusal()
        predecessor = parse_checkpoint(raw)
        if self._position_key(predecessor) != intent.predecessor:
            raise self._anchor_refusal()
        prefix, _ = self._verified(predecessor)
        if records[:len(prefix)] != prefix or self._anchor_coordinate(predecessor, raw, prefix) != intent.prior:
            raise self._anchor_refusal()
        return len(prefix)

    def _narrowing_semantic_suffix(self, intent, checkpoint, records):
        start = self._prove_narrowing_predecessor(intent, records)
        body = intent.body()
        suffix = records[start:]
        intervening = []
        for row in suffix:
            if row["kind"] == "cross_journal_tail_repaired":
                self._check_scoped_repair(row, body, checkpoint["journal_id"])
            else:
                intervening.append(row)
        return intervening

    def _settle_narrowing(self, intent: Narrowing, checkpoint, records, observed) -> None:
        body = intent.body()
        intervening = self._narrowing_semantic_suffix(intent, checkpoint, records)
        if not intervening:
            if observed != intent.prior:
                raise self._anchor_refusal()
            state = project(records)
            if retires(state, body) is None:
                raise self._anchor_refusal()
            if limiting_control(state, self.budget, headroom=1) is not None:
                try:
                    self._compact_locked("M", "closeout-replenishment", narrowing=intent)
                except ProtocolRefusal as exc:
                    if exc.code not in {"cross_compaction_no_gain", "cross_reserve_protected", "cross_storage_cap"}:
                        raise
                    raise ProtocolRefusal("cross_closeout_record_pending", "the fenced narrowing needs recovery space") from exc
                return
            caller = Capability("M", body.get("pair_id"), verb="maintain")
            result, _ = self._append_locked(caller, body, authorized_intent=intent)
            if result.status != "committed":
                raise ProtocolRefusal(result.code or "cross_closeout_record_pending",
                                      "the fenced narrowing still needs recovery")
            return
        if (
            len(intervening) != 1 or semantic(intervening[0]) != body or observed is None
            or observed.generation != intent.prior.generation
            or observed.opening_sha256 != intent.prior.opening_sha256
            or observed.trust[:2] != intent.prior.trust[:2]
            or observed.trust[2] != intent.prior.trust[2] + 1
        ):
            raise self._anchor_refusal()
        self._replace_anchor(intent, observed)

    def _enable_initial(self, right: EnableCreate, checkpoint, raw, records) -> Anchor:
        proof = Narrowing(right.initial, right.predecessor, right.predecessor_checkpoint, b"{}\n")
        prefix_length = self._prove_narrowing_predecessor(proof, records)
        if not prefix_length:
            raise self._anchor_refusal()
        start = records[prefix_length - 1]
        complete = approved_completion(start)
        if (
            start.get("token") != right.token or complete["approval_sha256"] != right.approval_sha256
            or self._anchor_coordinate(checkpoint, raw, records) != right.initial
            or any(row["kind"] not in {"cross_inventory_item", "cross_journal_tail_repaired"}
                   or row["kind"] == "cross_inventory_item" and (
                       row.get("enable_id"), row.get("enable_generation"), row.get("token")
                   ) != (start["enable_id"], start["enable_generation"], start["token"])
                   for row in records[prefix_length:])
        ):
            raise self._anchor_refusal()
        self._check_enable_repair_history(checkpoint, records, semantic(start), right.initial)
        return right.initial

    def _install_enable_create(self, started, checkpoint, raw, records) -> None:
        initial = self._anchor_coordinate(checkpoint, raw, records)
        if self._observed_store() is not None or initial is None or initial.trust[2] != 1:
            raise self._anchor_refusal()
        witness = self._observed_quota()
        if witness is not None:
            _, data = self._verified(checkpoint)
            if not self._quota_reference_matches(witness, checkpoint, data):
                raise self._quota_refusal()
            self._fund_quota_anchor(self._quota_inventory_items())
        self._keep_store_predecessor(raw)
        complete = approved_completion(started)
        right = EnableCreate(initial, self._position_key(checkpoint), _sha256(raw), started["token"],
                             complete["approval_sha256"])
        self._replace_anchor(None, right)

    @staticmethod
    def _enable_inventory_refusal():
        return ProtocolRefusal("cross_enable_inventory_unproved", "the original enable inventory is unproved")

    def _inventory_rows(self, start, h1):
        if self._enable_inventory is None:
            raise self._enable_inventory_refusal()
        proofs = {item: self._enable_inventory.expected(item, start) for item in _ENABLE_ITEMS}
        proofs["anchor"] = _sha256(encode_frame(stable_descriptor(h1))[:-1])
        for item, proof in proofs.items():
            if not isinstance(proof, str) or not _SHA256.fullmatch(proof):
                raise self._enable_inventory_refusal()
            for phase in ("intent", "created"):
                yield {
                    "schema_version": 1, "kind": "cross_inventory_item",
                    "id": f"cross-inventory-{start['enable_id']}-{start['enable_generation']}-{item}-{phase}",
                    "enable_id": start["enable_id"], "enable_generation": start["enable_generation"],
                    "token": start["token"], "item": item, "phase": phase, "proof_sha256": proof,
                }

    def _inventory_read(self, start, rows, *, complete):
        for intent in rows[::2]:
            item = intent["item"]
            if item == "anchor":
                continue
            observed = self._enable_inventory.read(item, start)
            if observed != ("owned", intent["proof_sha256"]) and (
                complete or observed != ("safe_absent", None)
            ):
                raise self._enable_inventory_refusal()

    def _inventory_prefix(self, records, start, required):
        scope = start["enable_id"], start["enable_generation"]
        existing = [semantic(row) for row in records if row["kind"] == "cross_inventory_item"
                    and (row.get("enable_id"), row.get("enable_generation")) == scope]
        if existing != required[:len(existing)]:
            raise self._enable_inventory_refusal()
        return len(existing)

    def _resume_enable_inventory(self, capability, candidate):
        checkpoint, raw = self._load()
        records, _ = self._verified(checkpoint)
        start = next((semantic(row) for row in reversed(records) if row.get("phase") == "enable_started"), None)
        if (start is None or approved_completion(start) != candidate or self._enable_approval is None
            or self._enable_approval("enable_complete", start) is not True
            or "disable_started" in project(records).phases):
            raise self._enable_inventory_refusal()
        right = self._observed_store()
        current = self._anchor_coordinate(checkpoint, raw, records)
        if isinstance(right, EnableCreate):
            h1 = self._enable_initial(right, checkpoint, raw, records)
        elif isinstance(right, Anchor) and right == current and right.trust[2] in (1, 2):
            head = trust_coordinate([row for row in records if row.get("phase") != "enable_complete"])
            h1 = Anchor(head, current.generation, current.opening_sha256)
        else:
            raise self._enable_inventory_refusal()
        required = list(self._inventory_rows(start, h1))
        self._check_enable_repair_history(checkpoint, records, start, h1)
        prefix = self._inventory_prefix(records, start, required)
        already_complete = any(semantic(row) == candidate for row in records)
        if already_complete:
            self._require_completed_inventory(records, start, h1)
            return
        self._inventory_read(start, required, complete=False)
        for index in range(0, len(required), 2):
            intent, created = required[index:index + 2]
            if index >= prefix:
                self._publish_enable_inventory(capability, intent)
            item = intent["item"]
            if item == "anchor":
                live = self._observed_store()
                if isinstance(live, EnableCreate) and live == right:
                    self._replace_anchor(live, h1)
                elif live != h1:
                    raise self._enable_inventory_refusal()
            else:
                expected = ("owned", intent["proof_sha256"])
                observation = self._enable_inventory.read(item, start)
                if observation == ("safe_absent", None):
                    if item == "ballast":
                        self._enable_ballast_scope = (threading.get_ident(), dict(start), intent["proof_sha256"])
                    try:
                        self._enable_inventory.create(item, start)
                    finally:
                        self._enable_ballast_scope = None
                    observation = self._enable_inventory.read(item, start)
                if observation != expected:
                    raise self._enable_inventory_refusal()
            if index + 1 >= prefix:
                self._publish_enable_inventory(capability, created)
        self._inventory_read(start, required, complete=True)

    def _publish_enable_inventory(self, capability, record):
        check(capability, record)
        result, _ = self._append_locked(capability, record, creating=True)
        if result.status != "committed":
            raise ProtocolRefusal(result.code or "cross_enable_inventory_unproved", "the inventory row is unsettled")

    def _check_enable_repair_history(self, checkpoint, records, start, h1):
        index = next(i for i, row in enumerate(records) if semantic(row) == start)
        suffix = records[index + 1:]
        if not any(row["kind"] == "cross_journal_tail_repaired" for row in suffix):
            return
        required = list(self._inventory_rows(start, h1))
        done = 0
        for row in suffix:
            if row["kind"] == "cross_inventory_item":
                done += 1
            elif row["kind"] == "cross_journal_tail_repaired":
                attempted = required[done] if done < len(required) else approved_completion(start)
                self._check_scoped_repair(row, attempted, checkpoint["journal_id"])
            elif row.get("phase") == "enable_complete":
                break

    def _check_pairing_material(self, records, *, require_attended=False):
        start = next((semantic(row) for row in reversed(records) if row.get("phase") == "enable_started"), None)
        if start is None or start.get("schema_version") not in (1, 2):
            if require_attended:
                raise self._enable_inventory_refusal()
            return  # The older fixture path does not represent an attended enable.
        if self._enable_inventory is None or not any(
            row.get("phase") == "enable_complete" and semantic(row) == approved_completion(start) for row in records
        ):
            raise self._enable_inventory_refusal()
        for item in _ENABLE_ITEMS:
            expected = self._enable_inventory.expected(item, start)
            if not _SHA256.fullmatch(str(expected)) or self._enable_inventory.read(item, start) != ("owned", expected):
                raise self._enable_inventory_refusal()

    def _require_completed_inventory(self, records, start, h1):
        required = list(self._inventory_rows(start, h1))
        if self._inventory_prefix(records, start, required) != len(required):
            raise self._enable_inventory_refusal()
        self._inventory_read(start, required, complete=True)

    def _handoff_opening(self, handoff):
        digest = handoff.intent_checkpoint
        raw = self.storage.read(f"journal-proof-{digest}.commit")
        seal = self.storage.read(f"journal-proof-{digest}.seal")
        if raw is None or _sha256(raw) != digest or seal != encode_frame({
            "format": SEAL_FORMAT, "checkpoint_sha256": digest,
        }):
            raise self._anchor_refusal()
        controller = parse_checkpoint(raw)
        intent = controller["compaction_intent"]
        if (
            intent is None or intent["reason"] != handoff.reason
            or self._position_key(controller) != handoff.predecessor or controller["previous"] is None
            or self._position_key(controller["previous"]) != handoff.predecessor
            or controller["previous"]["checkpoint_sha256"] != handoff.predecessor_checkpoint
            or controller.get("opening_sha256") != handoff.prior.opening_sha256
            or self._successor_position(intent) != handoff.successor
            or intent["successor_sha256"] != handoff.successor_sha256
            or intent.get("quota_sha256") != handoff.quota_sha256
        ):
            raise self._anchor_refusal()
        return encode_frame(self._successor_checkpoint(controller, raw))

    def _settle_anchor(self, checkpoint, raw, records, data, *, initial=False, pair_advance=False,
                       pair_phase="recover") -> None:
        if self._anchor_store is None:
            return
        current = self._observed_store()
        observed = self._anchor_coordinate(checkpoint, raw, records)
        if current == observed:
            return
        if current is None:
            if "disable_started" in project(records).phases:
                return
            if (initial and observed is not None and observed.trust[2] == 1
                and records and records[-1].get("schema_version") == 0):
                self._replace_anchor(None, observed)
                return
            raise self._anchor_refusal()
        if isinstance(current, EnableCreate):
            self._enable_initial(current, checkpoint, raw, records)
            return
        if isinstance(current, Narrowing):
            self._settle_narrowing(current, checkpoint, records, observed)
            return
        handoff = current.handoff if isinstance(current, NarrowingHandoff) else current
        if isinstance(handoff, Handoff):
            position = self._position_key(checkpoint)
            if (
                position == handoff.successor and "opening_sha256" not in checkpoint
                and len(data) == checkpoint["byte_length"] and _sha256(data) == handoff.successor_sha256
                and (
                    checkpoint["format"] != BOUND_QUOTA_FORMAT and handoff.quota_sha256 is None
                    or checkpoint["format"] == BOUND_QUOTA_FORMAT
                    and handoff.quota_sha256 == successor_quota_sha256(checkpoint["quota"])
                )
                and observed is not None and observed.trust == handoff.prior.trust
                and checkpoint["previous"] is not None
                and self._position_key(checkpoint["previous"]) == handoff.predecessor
                and checkpoint["previous"]["checkpoint_sha256"] == handoff.intent_checkpoint
            ):
                if raw != self._handoff_opening(handoff):
                    raise self._anchor_refusal()
                proof = Narrowing(handoff.prior, handoff.predecessor, handoff.predecessor_checkpoint, b"{}\n")
                predecessor = self.storage.read(f"journal-proof-{proof.predecessor_checkpoint}.commit")
                if predecessor is None:
                    raise self._anchor_refusal()
                predecessor_rows, _ = self._verified(parse_checkpoint(predecessor))
                self._prove_narrowing_predecessor(proof, predecessor_rows)
                if checkpoint["format"] == BOUND_QUOTA_FORMAT:
                    self._keep_store_predecessor(raw)
                if isinstance(current, NarrowingHandoff):
                    self._keep_store_predecessor(raw)
                    original = current.narrowing.original or current.narrowing
                    self._prove_narrowing_predecessor(original, predecessor_rows)
                    rebased = Narrowing(observed, position, _sha256(raw), original.body_frame, original)
                    self._replace_anchor(current, rebased)
                    self._settle_narrowing(rebased, checkpoint, records, observed)
                else:
                    self._replace_anchor(current, observed)
                return
            if (position == handoff.predecessor and _sha256(raw) == handoff.predecessor_checkpoint
                and len(data) == checkpoint["byte_length"] and observed == handoff.prior
                and not any(_JOURNAL_FILE.fullmatch(name) and name != checkpoint["journal_file"]
                            for name in self.storage.names())):
                pending = self._recover_handoff_plan(handoff, checkpoint, raw, records)
                if _sha256(encode_frame(pending)) != handoff.intent_checkpoint:
                    raise self._anchor_refusal()
                self._keep_store_predecessor(raw)
                payload = self._commit_checkpoint(pending)
                successor, sealed = self._finish_compaction(pending, payload)
                successor_records, successor_data = self._verified(successor)
                self._settle_anchor(successor, sealed, successor_records, successor_data)
                return
            raise self._anchor_refusal()
        if observed is not None and records and not self._narrowing(records[-1]):
            previous = trust_coordinate(records[:-1])
            if (observed.generation == current.generation and observed.opening_sha256 == current.opening_sha256
                and previous == current.trust and observed.trust[:2] == current.trust[:2]
                and observed.trust[2] == current.trust[2] + 1):
                if records[-1]["kind"] == "cross_pair_active":
                    if not pair_advance:
                        raise ProtocolRefusal("cross_pair_maintenance_required", "pair activation needs its own recovery authority")
                    activation = semantic(records[-1])
                    self._check_pairing_material(records, require_attended=activation["schema_version"] == 1)
                    if activation["schema_version"] == 1:
                        self._require_pair_opening(records[:-1], activation)
                        self._require_pair_material(activation, phase=pair_phase)
                    elif self._prove_pair_material is not None:
                        raise self._pair_material_refusal()
                if records[-1].get("phase") == "enable_complete" and records[-1].get("schema_version") in (1, 2):
                    start = next(semantic(row) for row in reversed(records[:-1]) if row.get("phase") == "enable_started")
                    if approved_completion(start) != semantic(records[-1]):
                        raise self._enable_inventory_refusal()
                    self._check_enable_repair_history(checkpoint, records, start, current)
                    self._require_completed_inventory(records, start, current)
                self._replace_anchor(current, observed)
                return
        raise self._anchor_refusal()

    # -- maintenance (recovery) ---------------------------------------------

    def _recover_handoff_plan(self, handoff, checkpoint, raw, records):
        if handoff.reason == _MIGRATION:
            plan = self.storage.read(f"journal-plan-{handoff.intent_checkpoint}.commit")
            if plan is None or _sha256(plan) != handoff.intent_checkpoint:
                raise self._quota_refusal()
            controller = parse_checkpoint(plan)
            quota = controller.get("quota")
            if quota is None or quota["source"] != sealed_descriptor(self._position_key(checkpoint), _sha256(raw)):
                raise self._quota_refusal()
            target = self._quota_target(handoff.successor[1], handoff.successor[2])
            rebuilt, _ = self._successor(checkpoint, records, _MIGRATION, target_enable=target, quota_projection=quota,
                                         expected_digest=handoff.successor_sha256)
            return self._quota_controller(checkpoint, raw, quota, rebuilt)
        pending, _, _ = self._compaction_plan(checkpoint, raw, records, handoff.reason,
                                              expected_digest=handoff.successor_sha256)
        return pending

    def _inert_quota_closeout(self, capability, checkpoint, records) -> bool:
        # Once the independently read root anchor is absent, an exact local
        # stop can still finish its inert plan. It supplies no quota authority.
        return (
            capability.writer == "L" and capability.verb == "disable"
            and capability.pair_id is None and self._anchor_store is not None
            and self._observed_store() is None and checkpoint["compaction_intent"] is None
            and "disable_started" in project(records).phases
        )

    def _maintain_locked(self, capability: Capability) -> Dict[str, object]:
        """Leave a sealed checkpoint, no intent, no tail and no stale file."""

        self._require_root_providers()
        self._observed_store()
        quota_witness = self._observed_quota()
        observed = self.storage.sync_position()
        report: Dict[str, object] = {"state": "clean", "durability": "unknown"}
        for name in self.storage.names():
            if _TEMPORARY.fullmatch(name):
                self.storage.unlink(name)
        loaded = self._load()
        if loaded is None:
            if self._established():
                raise _corrupt("an established journal has no checkpoint")
            report["state"] = "absent"
            return report
        checkpoint, raw = loaded
        if quota_witness is not None and (
            _sha256(raw) == quota_witness.predecessor_checkpoint or checkpoint["format"] != BOUND_QUOTA_FORMAT
            or checkpoint["compaction_intent"] is not None
        ):
            checkpoint, raw = self._finish_quota_migration(quota_witness, checkpoint, raw)
        records, data = self._verified(checkpoint)
        if checkpoint["format"] == BOUND_QUOTA_FORMAT:
            if quota_witness is not None:
                if not self._quota_reference_matches(quota_witness, checkpoint, data):
                    raise self._quota_refusal()
            elif not (
                isinstance(self._observed_store(), (Handoff, NarrowingHandoff))
                or self._anchored_quota_matches(checkpoint, data)
                or self._inert_quota_closeout(capability, checkpoint, records)
            ):
                raise self._quota_refusal()
        if "completion_repair" in checkpoint:
            delivery = project(records).deliveries.get(str(checkpoint["completion_repair"]))
            if delivery is None or not delivery.reserved or not delivery.remaining or delivery.repair_spent:
                raise _corrupt("the completion repair marker has no available delivery credit")
        if checkpoint["compaction_intent"] is not None and self._anchor_store is not None:
            store = self._observed_store()
            inert = store is None and "disable_started" in project(records).phases
            handoff = store.handoff if isinstance(store, NarrowingHandoff) else store
            if not inert and (
                not isinstance(handoff, Handoff) or handoff.intent_checkpoint != _sha256(raw)
                or handoff.quota_sha256 != checkpoint["compaction_intent"].get("quota_sha256")
            ):
                raise self._anchor_refusal()
        if self._seal() != _sha256(raw):
            # The barrier a dead or failed writer left open.
            self.census.append((self._origin, "M", "seal"))
            self.storage.sync_directory()
            self._write_seal(_sha256(raw))
            report["sealed"] = True
        if checkpoint["repair_intent"] is not None:
            self.census.append((self._origin, "M", "cross_journal_tail_repaired"))
            checkpoint, raw = self._finish_repair(checkpoint, raw)
            report["state"] = "repaired"
        elif checkpoint["compaction_intent"] is not None:
            self.census.append((self._origin, "M", "compaction"))
            checkpoint, raw = self._finish_compaction(checkpoint, raw)
            report["state"] = "compacted"
        elif len(data) > int(checkpoint["byte_length"]):
            self.census.append((self._origin, "M", "cross_journal_tail_repaired"))
            checkpoint, raw = self._start_repair(checkpoint, raw, data)
            report["state"] = "repaired"
        elif "completion_repair" in checkpoint:
            # The writer stopped before leaving any bytes to repair.
            clean = dict(self._carry_opening(checkpoint, raw), previous=self._previous(checkpoint, raw))
            del clean["completion_repair"]
            raw = self._commit_checkpoint(clean)
            checkpoint = clean
        if self._anchor_store is not None:
            records, data = self._verified(checkpoint)
            self._settle_anchor(checkpoint, raw, records, data, pair_advance=capability.writer == "M")
            checkpoint, raw = self._load()
        self._collect_garbage(checkpoint)
        report["through_seq"] = checkpoint["through_seq"]
        report["durability"] = self.storage.sync_grade(observed)
        return report

    def _collect_garbage(self, checkpoint: Mapping[str, object]) -> None:
        stale = [
            name for name in self.storage.names()
            if _JOURNAL_FILE.fullmatch(name) and name != checkpoint["journal_file"]
        ]
        if stale:
            # Make the seal durable before removing what an older seal would need.
            self.storage.sync_directory()
            for name in stale:
                self.storage.unlink(name)
            self.storage.sync_directory()

    @staticmethod
    def _repair_line(position: Mapping[str, object], intent: Mapping[str, object]) -> bytes:
        token = _sha256(
            f"{position['journal_id']}:{intent['offset']}:{intent['tail_sha256']}".encode("utf-8")
        )
        record = {
            "schema_version": 0,
            "id": "cross-repair-" + token[:32],
            "kind": "cross_journal_tail_repaired",
            "offset": intent["offset"],
            "tail_len": intent["tail_len"],
            "tail_sha256": intent["tail_sha256"],
            "complete_frames": intent["complete_frames"],
        }
        if "completion_repair" in position:
            record["completion_delivery"] = position["completion_repair"]
        try:
            return priced_frame(record, int(position["through_seq"]) + 1, position["head_sha256"])
        except ProtocolRefusal as exc:
            raise _corrupt("the suffix is outside the ordinary repair domain") from exc

    def _maintenance_due(self, *, inert=False) -> bool:
        """Read-only: would `_maintain_locked` write anything?"""

        if any(_TEMPORARY.fullmatch(name) for name in self.storage.names()):
            return True
        loaded = self._load()
        if loaded is None:
            if self._established():
                raise _corrupt("an established journal has no checkpoint")
            return False
        checkpoint, raw = loaded
        records, data = self._verified(checkpoint)
        return (
            self._anchor_pending(checkpoint, raw, records, inert=inert)
            or self._quota_pending(checkpoint, data)
            or self._seal() != _sha256(raw)
            or checkpoint["repair_intent"] is not None
            or checkpoint["compaction_intent"] is not None
            or "completion_repair" in checkpoint
            or len(data) > int(checkpoint["byte_length"])
            or any(_JOURNAL_FILE.fullmatch(name) and name != checkpoint["journal_file"]
                   for name in self.storage.names())
        )

    def _maintain_as(self, capability: Capability) -> None:
        """Run due maintenance under M's authority, or defer an I writer before any write."""

        self._require_root_providers()
        if not self._maintenance_due(inert=capability.writer == "L" and capability.verb == "disable"):
            return
        if not authority_row(capability).maintains:
            raise ProtocolRefusal(
                "cross_maintenance_due",
                "this journal needs maintenance first; an ingress writer defers to a local pass",
            )
        self._maintain_locked(capability)
        loaded = self._load()
        if loaded is not None:
            checkpoint, _ = loaded
            records, data = self._verified(checkpoint)
            if self._quota_pending(checkpoint, data) and not self._inert_quota_closeout(capability, checkpoint, records):
                raise self._quota_refusal()

    def _priced(self, candidate):
        kind, version = candidate["kind"], candidate.get("schema_version")
        return (
            version == 2 and (kind in COMPLETION_KINDS or kind in {"cross_ceremony_closed", "cross_optin"})
            or version == 1 and (self._narrowing(candidate) or kind == "cross_pair_active"
                                or (kind == "cross_teardown_receipt" and candidate.get("outcome") == "complete"))
        )

    def _candidate_frame(self, checkpoint, records, candidate):
        seq, prev = int(checkpoint["through_seq"]) + 1, checkpoint["head_sha256"]
        if not self._priced(candidate):
            return _frame(dict(candidate, seq=seq, prev=prev))
        prior = None
        if candidate["kind"] in {"cross_optin", "cross_pair_revoked", "cross_pair_active"}:
            prior = trust_genesis(candidate) if candidate.get("phase") == "enable_started" else trust_coordinate(records)
        trust = (prior[3], prior[2] + 1) if prior is not None else None
        return priced_frame(candidate, seq, prev, trust=trust)

    def _save_scoped_repair(self, checkpoint, raw, intent, tail):
        current = self._observed_store()
        if not isinstance(current, (Narrowing, EnableCreate, Anchor)):
            return
        records, _ = self._verified(checkpoint)
        if isinstance(current, Narrowing):
            attempted = current.body()
        else:
            h1 = current.initial if isinstance(current, EnableCreate) else current
            if h1.trust[2] != 1 or self._enable_inventory is None:
                return
            start = next((semantic(row) for row in reversed(records) if row.get("phase") == "enable_started"), None)
            if start is None or start.get("schema_version") not in (1, 2):
                return
            required = list(self._inventory_rows(start, h1))
            step = self._inventory_prefix(records, start, required)
            attempted = required[step] if step < len(required) else approved_completion(start)
        expected = self._candidate_frame(checkpoint, records, attempted)
        if not expected.startswith(tail):
            if isinstance(current, Narrowing):
                raise self._anchor_refusal()
            return
        self._keep_store_predecessor(raw)
        record = decode_frames(self._repair_line(checkpoint, intent))[0]
        name = f"journal-repair-proof-{record['id']}.commit"
        proof = encode_frame({"format": "floati-cross-repair-witness-v1", "predecessor_sha256": _sha256(raw),
                              "attempted": attempted})
        existing = self.storage.read(name)
        if existing is None:
            self.storage.create(name, proof)
        elif existing != proof:
            raise self._anchor_refusal()
        self.storage.sync(name)
        self.storage.sync_directory()

    def _check_scoped_repair(self, row, attempted, journal_id):
        witness = self.storage.read(f"journal-repair-proof-{row['id']}.commit")
        if witness is None:
            raise self._anchor_refusal()
        try:
            proof = decode_frames(witness)
        except FrameError as exc:
            raise self._anchor_refusal() from exc
        if len(proof) != 1 or not isinstance(proof[0], dict):
            raise self._anchor_refusal()
        proof = proof[0]
        digest = proof.get("predecessor_sha256")
        if (not isinstance(digest, str) or not _SHA256.fullmatch(digest) or witness != encode_frame({
            "format": "floati-cross-repair-witness-v1", "predecessor_sha256": digest, "attempted": attempted,
        })):
            raise self._anchor_refusal()
        raw = self.storage.read(f"journal-proof-{digest}.commit")
        seal = self.storage.read(f"journal-proof-{digest}.seal")
        if raw is None or _sha256(raw) != digest or seal != encode_frame({
            "format": SEAL_FORMAT, "checkpoint_sha256": digest,
        }):
            raise self._anchor_refusal()
        predecessor = parse_checkpoint(raw)
        records, _ = self._verified(predecessor)
        expected = self._candidate_frame(predecessor, records, attempted)
        if (
            predecessor["journal_id"] != journal_id or predecessor["through_seq"] + 1 != row["seq"]
            or predecessor["head_sha256"] != row["prev"] or predecessor["byte_length"] != row["offset"]
            or len(expected) < row["tail_len"] or _sha256(expected[:row["tail_len"]]) != row["tail_sha256"]
            or decode_frames(self._repair_line(predecessor, row))[0] != row
        ):
            raise self._anchor_refusal()

    def _start_repair(self, checkpoint, raw, data):
        name = str(checkpoint["journal_file"])
        offset = int(checkpoint["byte_length"])
        tail = data[offset:]
        intent = {
            "offset": offset,
            "tail_len": len(tail),
            "tail_sha256": _sha256(tail),
            "complete_frames": tail.count(b"\n"),
        }
        self._repair_line(checkpoint, intent)
        # The digest must name the tail that survives a power loss.
        self.storage.sync(name)
        self._save_scoped_repair(checkpoint, raw, intent, tail)
        pending = dict(self._carry_opening(checkpoint, raw), repair_intent=intent, previous=self._previous(checkpoint, raw))
        payload = self._commit_checkpoint(pending)
        return self._finish_repair(pending, payload)

    def _finish_repair(self, checkpoint, raw):
        intent = checkpoint["repair_intent"]
        name = str(checkpoint["journal_file"])
        offset = int(intent["offset"])
        if offset != checkpoint["byte_length"]:
            raise _corrupt("the repair intent does not start at the committed length")
        expected = self._repair_line(checkpoint, intent)
        beyond = (self.storage.read(name) or b"")[offset:]
        if beyond != expected:
            original = len(beyond) == intent["tail_len"] and _sha256(beyond) == intent["tail_sha256"]
            if beyond and not original and not expected.startswith(beyond):
                raise _corrupt("the content beyond a pending repair intent is unexplained")
            if beyond:
                self.storage.truncate(name, offset)
                self.storage.sync(name)
            self.storage.append(name, expected)
        self.storage.sync(name)
        repaired = dict(
            checkpoint,
            through_seq=int(checkpoint["through_seq"]) + 1,
            head_sha256=line_digest(expected),
            byte_length=offset + len(expected),
            repair_intent=None,
            previous=self._previous(checkpoint, raw),
        )
        repaired.pop("completion_repair", None)
        return repaired, self._commit_checkpoint(repaired)

    # -- compaction ---------------------------------------------------------

    def _successor(
        self, position: Mapping[str, object], records: Sequence[Mapping[str, object]], reason: str,
        *, target_enable: Optional[str] = None, quota_projection=None, expected_digest=None, _carry_gate=True,
    ):
        quota = quota_projection if quota_projection is not None else (
            position.get("quota") if position["format"] == BOUND_QUOTA_FORMAT else None
        )
        generation = int(position["generation"]) + 1
        trust = trust_head(project(records))
        stem = str(position["journal_id"]).rsplit("-g", 1)[0]
        modern = quota is not None or position["format"] in _MODERN_FORMATS
        if modern and reason.startswith(_ADOPT):
            stem = "cross-" + reason[len(_ADOPT):]
        if reason == _MIGRATION and target_enable is not None:
            stem = "cross-" + target_enable
        journal_id = f"{stem}-g{generation}"
        link = {
            "schema_version": 0,
            "id": "cross-link-" + _sha256(
                f"{position['journal_id']}:{position['head_sha256']}".encode("utf-8")
            )[:32],
            "kind": "cross_journal_link",
            "previous_journal_id": position["journal_id"],
            "previous_through_seq": position["through_seq"],
            "previous_head_sha256": position["head_sha256"],
            "previous_trust_head": trust,
            "reason": reason,
        }
        kept = retained(records)
        if modern:
            pre_enable_repairs = []
            for row in records:
                if row.get("phase") == "enable_started":
                    break
                if row["kind"] == "cross_journal_tail_repaired":
                    pre_enable_repairs.append({key: value for key, value in row.items() if key not in {"seq", "prev"}})
            kept = pre_enable_repairs + kept
        if _PRUNE.fullmatch(reason):
            removed, _, retirement = self._receiver_prune_inputs(records, reason)
            kept = [row for row in kept if row["id"] not in removed]
            kept.append(retirement)
        if _SENDER_PRUNE.fullmatch(reason):
            removed, replacements = self._sender_prune_inputs(records, reason)
            kept = [row for row in kept if row["id"] not in removed] + replacements
        kept_state = project(kept)
        trust_record = {
            "schema_version": 0,
            "id": "cross-trust-" + _sha256(f"{journal_id}:{trust}".encode("utf-8"))[:32],
            "kind": "cross_trust_checkpoint",
            "previous_trust_head": trust,
            "trust_head": trust_head(kept_state),
            "active_pair_ids": sorted(kept_state.active_pairs),
        }
        for row in kept:
            if row["kind"] == "cross_optin" and row.get("phase") == "enable_started":
                # Older dark journals omitted the epoch. Preserve their exact
                # successor bytes, including when replaying a pending intent.
                if "enable_generation" in row:
                    trust_record["enable_generation"] = row["enable_generation"]
                break
        if modern:
            state = project(records)
            coordinate = trust_coordinate(records)
            if coordinate is not None:
                enable_id, epoch, count, head = coordinate
                trust_record.update(schema_version=1, enable_id=enable_id, enable_generation=epoch,
                                    trust_count=count, trust_head=head, previous_trust_head=head)
                link.update(schema_version=1, previous_trust_head=head)
                if _carry_gate:
                    pair_ids = {key for key, pair in state.active_pairs.items() if pair.get("schema_version") == 1}
                    maxima = {
                        key: dict(enable_id=enable_id, enable_generation=epoch,
                                  max_session=floor.max_session, max_out_next=floor.outbound_min_next)
                        for key, floor in gate_history(records, enable_id, epoch, pair_ids).items()
                    }
                    if maxima:
                        link.update(schema_version=2, gate_maxima=maxima)
            trusted_day, trusted_count = state.quota_day, state.quota_count
            if quota is not None and quota["mode"] == "exact":
                trusted_day, trusted_count = quota["day"], quota["count"]
            trust_record.update(schema_version=1 if coordinate is not None else 2,
                                quota_day=trusted_day, quota_count=trusted_count)
        rows = [link] + kept + [trust_record]
        head = genesis_head(journal_id)
        body = b""
        for seq, row in enumerate(rows, start=1):
            line = _frame(dict(row, seq=seq, prev=head))
            body += line
            head = line_digest(line)
        if (expected_digest is not None and _sha256(body) != expected_digest and link["schema_version"] == 2
            and not any(row["kind"] == "cross_journal_link" and row.get("schema_version") == 2 for row in records)):
            # A controller published before the gate-link upgrade binds its exact old bytes.
            return self._successor(position, records, reason, target_enable=target_enable,
                                   quota_projection=quota_projection, _carry_gate=False)
        intent = {
            "successor_file": journal_file(generation),
            "successor_journal_id": journal_id,
            "successor_generation": generation,
            "successor_sha256": _sha256(body),
            "successor_length": len(body),
            "successor_through_seq": len(rows),
            "successor_head_sha256": head,
            "reason": reason,
        }
        if quota is not None:
            intent["quota_sha256"] = successor_quota_sha256(quota)
        return intent, body

    @staticmethod
    def _receiver_scope(records, pair: str, direction: str):
        return [row for row in records if row.get("pair_id") == pair and row.get("direction") == direction]

    @staticmethod
    def _receiver_frontier(scoped):
        return max((row["frontier"] for row in scoped if row["kind"] == "cross_prune_checkpoint"), default=0)

    def _receiver_prune_inputs(self, records, reason: str):
        match = _PRUNE.fullmatch(reason)
        pair, direction, target = match.groups()
        target = int(target)
        scoped = self._receiver_scope(records, pair, direction)
        prior = self._receiver_frontier(scoped)
        inbounds = [row for row in scoped if row["kind"] == "remote_inbound"]
        if (not _integer(target, prior + 1, MAX_INTEGER)
            or target > retirable_through(prior, inbounds, scoped)):
            raise ProtocolRefusal("cross_prune_frontier_unproved", "the receiver retirement must be a confirmed prefix")
        inbound_ids = {row["id"] for row in inbounds if prior < row["delivery_seq"] <= target}
        removed = {row["id"] for row in records
                   if row["id"] in inbound_ids or (row["kind"] in COMPLETION_KINDS and row.get("inbound_id") in inbound_ids)}
        archive = b"".join(_frame(row) for row in records if row["id"] in removed)
        checkpoint_digest = prune_digest(pair, direction, target, archive, [])
        retirement = {
            "schema_version": 1, "id": f"cross-prune-{checkpoint_digest}", "kind": "cross_prune_checkpoint",
            "pair_id": pair, "direction": direction, "frontier": target,
            "checkpoint_digest": checkpoint_digest, "archive_sha256": _sha256(archive), "exceptions": [],
        }
        return removed, archive, retirement

    @staticmethod
    def _prune_archive_name(retirement):
        return "archive-" + retirement["archive_sha256"] + ".jsonl"

    def _sync_prune_archive(self, retirement, archive):
        name = self._prune_archive_name(retirement)
        existing = self.storage.read(name)
        if existing is None:
            self.storage.create(name, archive)
        elif existing != archive:
            raise _corrupt("the receiver archive conflicts with its commitment")
        self.storage.sync(name)
        self.storage.sync_directory()

    def prune_receiver(self, capability: Capability, direction: str, through: int) -> dict:
        """Archive and retire an exact confirmed receiver prefix."""
        pair = capability.pair_id
        if (capability.writer != "L" or capability.verb != "prune" or not isinstance(pair, str)
            or not _ENABLE_ID.fullmatch(pair) or direction not in ("S-to-T", "T-to-S")
            or not _integer(through, 1, MAX_INTEGER)):
            raise ProtocolRefusal("cross_capability_refused", "receiver retirement requires its scoped prune capability")
        self._origin = capability.writer
        with self._lock(exclusive=True):
            self._maintain_as(capability)
            loaded = self._load()
            if loaded is None:
                raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
            records, _ = self._verified(loaded[0])
            scoped = self._receiver_scope(records, pair, direction)
            prior = self._receiver_frontier(scoped)
            inbounds = [row for row in scoped if row["kind"] == "remote_inbound"]
            frontier = min(through, retirable_through(prior, inbounds, scoped))
            if frontier <= prior:
                return {"state": "unchanged", "frontier": prior}
            result = self._compact_locked("L", f"prune-receiver:{pair}:{direction}:{frontier}")
            return dict(result, frontier=frontier)

    @staticmethod
    def _sender_floor(scoped) -> int:
        return max((row["floor"] for row in scoped if row["kind"] == "cross_sender_floor"), default=0)

    def _verify_sender_coverage(self, records, scoped, enable, epoch, pair, direction):
        legacy = any(row["kind"] == "remote_outbound" and row.get("pair_id") == pair
                     and row.get("direction", direction) == direction and row.get("schema_version") != 2
                     and (row.get("enable_id"), row.get("enable_generation")) in ((None, None), (enable, epoch))
                     for row in records)
        floor = self._sender_floor(scoped)
        live, replay = {}, {}
        for row in scoped:
            if row["kind"] == "cross_sender_floor":
                continue
            if row["kind"] == "remote_sender_replay":
                index, sender = replay, row["sender_node"]
            elif row["kind"] == "remote_outbound" and row.get("schema_version") == 2:
                index, sender = live, row["envelope"]["sender_node"]
            else:
                raise ProtocolRefusal("cross_delivery_history_lost", "the scoped sender map is unverified")
            key = sender, row["idempotency_key"]
            if key in index:
                raise ProtocolRefusal("cross_delivery_history_lost", "the sender map repeats an authenticated key")
            index[key] = row
        for key in live.keys() & replay.keys():
            source, summary = live[key], replay[key]
            if (not source["replay_retained"] or summary["result"] is None
                or sender_replay(source, summary["result"]) != semantic(summary)):
                raise ProtocolRefusal("cross_delivery_history_lost", "a retained source conflicts with its replay")
        retired = sorted(row["delivery_seq"] for row in replay.values() if row["result"] is not None)
        suffix = sorted(row["delivery_seq"] for key, row in live.items() if key not in replay)
        if (legacy or len(retired) != floor
            or any(number != index for index, number in enumerate(retired, 1))
            or any(number != index for index, number in enumerate(suffix, floor + 1))):
            raise ProtocolRefusal("cross_delivery_history_lost", "the scoped sender map is incomplete")
        return replay, suffix

    def _sender_prune_inputs(self, records, reason):
        pair, direction, target = _SENDER_PRUNE.fullmatch(reason).groups()
        target = int(target)
        trust = trust_coordinate(records)
        if trust is None:
            raise ProtocolRefusal("cross_delivery_history_lost", "retirement requires a verified sender enable")
        enable, epoch = trust[:2]
        scoped = [row for row in self._receiver_scope(records, pair, direction)
                  if (row.get("enable_id"), row.get("enable_generation")) == (enable, epoch)]
        floor = self._sender_floor(scoped)
        sender_rows = [row for row in scoped if row["kind"] in {"remote_outbound", "remote_sender_replay", "cross_sender_floor"}]
        _, suffix = self._verify_sender_coverage(records, sender_rows, enable, epoch, pair, direction)
        outbounds = sorted((row for row in scoped if row["kind"] == "remote_outbound" and row["delivery_seq"] > floor),
                           key=lambda row: row["delivery_seq"])
        highest = floor + len(suffix)
        witnessed = max((row["frontier"] for row in records if row["kind"] == "remote_reconcile_frontier"
                         and row.get("pair_id") == pair and row.get("direction") == direction), default=0)
        if not floor < target <= min(witnessed, highest) or witnessed > highest:
            raise ProtocolRefusal("cross_prune_frontier_unproved", "sender retirement lacks the exact checked receiver frontier")
        replacements = []
        removed = set()
        for row in outbounds:
            if row["delivery_seq"] > target:
                break
            if row.get("schema_version") != 2:
                raise ProtocolRefusal("cross_delivery_history_lost", "the original authenticated request is unproved")
            witness = next((candidate for candidate in records if candidate["kind"] == "remote_peer_receipt"
                            and candidate.get("envelope_id") == row["envelope_id"]
                            and isinstance(candidate.get("receipt_body"), dict)), None)
            if witness is None:
                raise ProtocolRefusal("cross_prune_frontier_unproved", "the sender has no exact retained receipt")
            replay = sender_replay(row, witness["receipt_body"].get("result"))
            receipt, receipt_hash = sender_receipt(replay, replay["result"])
            if receipt != witness["receipt_body"] or receipt_hash != witness.get("receipt_digest"):
                raise ProtocolRefusal("cross_prune_frontier_unproved", "the sender receipt conflicts with its immutable source")
            if row["envelope"]["kind"] != "message" and (
                self._sender_dependencies is None
                or self._sender_dependencies(semantic(row), receipt) is not True
            ):
                raise ProtocolRefusal("cross_prune_frontier_unproved", "the sender still has a local ask or reply dependency")
            replacements.append(replay)
            if not row["replay_retained"]:
                removed.add(row["id"])
        marker = {"kind": "cross_sender_floor", "schema_version": 1, "enable_id": enable,
                  "enable_generation": epoch, "pair_id": pair, "direction": direction, "floor": target}
        marker["id"] = "cross-sender-floor-" + _sha256(encode_frame(marker))
        return removed, [*replacements, marker]

    def prune_sender(self, capability: Capability, direction: str, through: int) -> dict:
        pair = capability.pair_id
        if (capability.writer != "L" or capability.verb != "prune" or not isinstance(pair, str)
            or not _ENABLE_ID.fullmatch(pair) or direction not in ("S-to-T", "T-to-S")
            or not _integer(through, 1, MAX_INTEGER)):
            raise ProtocolRefusal("cross_capability_refused", "sender retirement requires its scoped prune capability")
        self._origin = capability.writer
        with self._lock(exclusive=True):
            self._maintain_as(capability)
            result = self._compact_locked("L", f"prune-sender:{pair}:{direction}:{through}")
            return dict(result, floor=through)

    def compact(self, capability: Union[str, Capability] = "L", reason: str = "explicit") -> Dict[str, object]:
        capability = as_capability(capability)
        require(capability, "compacts")
        if not self._fixture_mode:
            self._require_root_verb(capability, {"L", "M"}, "compact")
        if reason == _MIGRATION or reason.startswith((_ADOPT, "prune-receiver:", "prune-sender:")):
            raise ProtocolRefusal("cross_capability_refused", "this successor reason requires its owning verb")
        self._origin = capability.writer
        if not self.directory.is_dir():
            raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
        with self._lock(exclusive=True):
            self._maintain_as(capability)
            return self._compact_locked(capability.writer, reason)

    def _compaction_plan(self, checkpoint, raw, records, reason, *, expected_digest=None):
        quota = checkpoint.get("quota") if checkpoint["format"] == BOUND_QUOTA_FORMAT else None
        if quota is not None:
            state = project(records)
            newer = state.quota_day is not None and (
                quota["day"] is None or state.quota_day > quota["day"]
                or (quota["mode"] == "exact" and state.quota_day == quota["day"] and state.quota_count > quota["count"])
            )
            if newer:
                quota = {"mode": "exact", "day": state.quota_day, "count": state.quota_count,
                         "source": sealed_descriptor(self._position_key(checkpoint), _sha256(raw))}
        intent, body = self._successor(checkpoint, records, reason, quota_projection=quota,
                                       expected_digest=expected_digest)
        pending = dict(self._carry_opening(checkpoint, raw), compaction_intent=intent,
                       previous=self._previous(checkpoint, raw))
        if quota is not None:
            pending["quota"] = quota
        return pending, intent, body

    def _compact_locked(self, writer: str, reason: str, *, narrowing: Optional[Narrowing] = None) -> Dict[str, object]:
        loaded = self._load()
        if loaded is None:
            raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
        checkpoint, raw = loaded
        records, _ = self._verified(checkpoint)
        if checkpoint["format"] == BOUND_QUOTA_FORMAT and (self._observed_quota() is not None
                                                            or self._observed_store() is None):
            raise self._quota_refusal()
        pending, intent, body = self._compaction_plan(checkpoint, raw, records, reason)
        successor, _ = verify_prefix(body, str(intent["successor_journal_id"]))
        pruning = _PRUNE.fullmatch(reason) is not None
        admit_compaction(project(records), project(successor), self.budget, self.data_budget,
                         resource="data" if pruning or _SENDER_PRUNE.fullmatch(reason) else "control")
        if pruning:
            _, archive, retirement = self._receiver_prune_inputs(records, reason)
            self._sync_prune_archive(retirement, archive)
        self.census.append((writer, writer, "compaction"))
        replacement = prior = None
        if self._anchor_store is not None:
            prior = self._observed_store()
            observed = self._anchor_coordinate(checkpoint, raw, records)
            stable = narrowing.prior if narrowing is not None and narrowing == prior else prior
            if isinstance(stable, Anchor) and stable == observed and trust_coordinate(successor) == stable.trust:
                successor_position = (
                    intent["successor_file"], intent["successor_journal_id"], intent["successor_generation"],
                    intent["successor_through_seq"], intent["successor_head_sha256"], intent["successor_length"],
                )
                handoff = Handoff(stable, self._position_key(checkpoint), _sha256(raw), _sha256(encode_frame(pending)),
                                  successor_position, intent["successor_sha256"], reason, intent.get("quota_sha256"))
                replacement = NarrowingHandoff(narrowing, handoff) if narrowing is not None else handoff

            elif prior is not None or "disable_started" not in project(records).phases:
                raise self._anchor_refusal()
        payload = self._publish_handoff(raw, pending, prior, replacement)
        checkpoint, sealed = self._finish_compaction(pending, payload)
        self._settle_anchor(checkpoint, sealed, successor, body)
        checkpoint, _ = self._load()
        self._collect_garbage(checkpoint)
        return {"state": "compacted", "journal_id": checkpoint["journal_id"],
                "through_seq": checkpoint["through_seq"]}

    def _publish_handoff(self, raw, pending, prior, replacement):
        if replacement is not None:
            self._keep_store_predecessor(raw)
            self._replace_anchor(prior, replacement)
        try:
            return self._commit_checkpoint(pending)
        except OSError:
            # Only this invocation still owns the exclusive pre-publication
            # interval. Any visible controller or outstanding helper ends it.
            if (replacement is not None and self.storage.read(CHECKPOINT) == raw
                and self._seal() == _sha256(raw) and self._observed_store() == replacement):
                self._replace_anchor(replacement, prior)
            raise

    def _finish_compaction(self, checkpoint, raw):
        intent = checkpoint["compaction_intent"]
        records, _ = self._verified(checkpoint)
        reason = str(intent["reason"])
        target = self._quota_target(intent["successor_journal_id"], intent["successor_generation"]) if reason == _MIGRATION else None
        rebuilt, body = self._successor(checkpoint, records, reason, target_enable=target,
                                       expected_digest=intent["successor_sha256"])
        if _PRUNE.fullmatch(reason):
            _, archive, retirement = self._receiver_prune_inputs(records, reason)
            if self.storage.read(self._prune_archive_name(retirement)) != archive:
                raise _corrupt("the receiver retirement archive is missing or changed")
        if rebuilt != intent:
            raise _corrupt("the compaction successor no longer matches its intent")
        name = str(intent["successor_file"])
        existing = self.storage.read(name)
        if existing != body:
            if existing is not None:
                self.storage.unlink(name)
                self.storage.sync_directory()
            self.storage.create(name, body)
        self.storage.sync(name)
        self.storage.sync_directory()
        if self._anchor_store is not None:
            current = self._observed_store()
            if isinstance(current, (Handoff, NarrowingHandoff)):
                self._keep_store_predecessor(raw)
        successor = self._successor_checkpoint(checkpoint, raw)
        return successor, self._commit_checkpoint(successor)

    def _successor_checkpoint(self, checkpoint, raw):
        intent = checkpoint["compaction_intent"]
        name = intent["successor_file"]
        successor = {
            "format": checkpoint["format"],
            "journal_file": name,
            "journal_id": intent["successor_journal_id"],
            "generation": intent["successor_generation"],
            "through_seq": intent["successor_through_seq"],
            "head_sha256": intent["successor_head_sha256"],
            "byte_length": intent["successor_length"],
            "previous": self._previous(checkpoint, raw),
            "repair_intent": None,
            "compaction_intent": None,
        }
        if "quota" in checkpoint:
            successor["quota"] = checkpoint["quota"]
        return successor

    # -- writing ------------------------------------------------------------

    @staticmethod
    def _validate(record: Mapping[str, object]) -> Dict[str, object]:
        if not isinstance(record, Mapping):
            raise ProtocolRefusal("record_not_object", "a cross record is an object")
        if POSITION_FIELDS & record.keys() or "utc_day" in record:
            raise ProtocolRefusal("journal_chain_caller_fields", "the controller assigns journal coordinates")
        kind = record.get("kind")
        if not isinstance(kind, str) or kind in CONTROLLER_KINDS or kind not in KNOWN_KINDS:
            raise ProtocolRefusal("cross_kind_refused", f"{kind!r} is not an appendable kind")
        if not isinstance(record.get("id"), str) or not record.get("id"):
            raise ProtocolRefusal("record_id_required", "a cross record carries an id")
        if "completion_reserved" in record or (
            kind not in COMPLETION_KINDS and str(record["id"]).startswith("cross-completion-")
        ):
            raise ProtocolRefusal(
                "journal_chain_caller_fields", "the controller assigns completion reservations and record ids"
            )
        return dict(record)

    def create(
        self, enable_id: str, *, enable_generation: int = 1, attended: Optional[Mapping[str, object]] = None
    ) -> CommittedView:
        """Write the genesis checkpoint, then the first record. Resumable."""

        self._require_root_providers()
        if not isinstance(enable_id, str) or _ENABLE_ID.fullmatch(enable_id) is None:
            raise ProtocolRefusal("cross_enable_id_invalid", "an enable ID requires 32 lowercase hexadecimal digits")
        if not _integer(enable_generation, 1, MAX_INTEGER):
            raise ProtocolRefusal(
                "cross_enable_generation_invalid", "the enable generation must be a positive integer at most 2**53 - 1"
            )
        journal_id = f"cross-{enable_id}-g1"
        capability = Capability("A", verb="enable")
        started = {
            "schema_version": 0,
            "id": f"cross-optin-{enable_id}-enable-started",
            "kind": "cross_optin",
            "phase": "enable_started",
            "enable_id": enable_id,
            "enable_generation": enable_generation,
            "cause": None,
        }
        if attended is not None:
            version = attended.get("schema_version")
            if not self._fixture_mode and version != 2:
                raise ProtocolRefusal("cross_enable_approval_invalid", "a new enable requires current trust framing")
            started = approved_start(enable_id, enable_generation, attended.get("token"), attended.get("plan"),
                                     schema_version=version)
            if dict(attended) != started:
                raise ProtocolRefusal("cross_enable_approval_invalid", "the approved plan or preview changed")
        if self._enable_approval is not None and attended is None:
            raise ProtocolRefusal("cross_enable_approval_invalid", "enable requires an attended start")
        if attended is not None and self._enable_approval is None:
            raise ProtocolRefusal("cross_enable_approval_invalid", "the local approval source is unavailable")
        if self._root_provenance is None:
            return self._create_started(capability, started, journal_id, attended is not None)
        root, source = self._root_provenance
        with source.exclude(root):
            self._check_root_binding(root)
            if self._load() is None and not self._independently_new(root_excluded=True):
                raise self._provenance_refusal()
            if attended is not None and self._enable_approval("start", started) is not True:
                raise ProtocolRefusal("cross_enable_approval_invalid", "the local authority did not approve the exact plan")
            if source.mark_established(root) is not True or source.read(root) != "established":
                raise self._provenance_refusal()
            return self._create_started(capability, started, journal_id, attended is not None, approved=True)

    def _create_started(self, capability, started, journal_id, attended, *, approved=False):
        enable_id, enable_generation = started["enable_id"], started["enable_generation"]
        self.storage.make_directory()
        self._origin = capability.writer
        with self._lock(exclusive=True):
            if attended and not approved and self._enable_approval("start", started) is not True:
                raise ProtocolRefusal("cross_enable_approval_invalid", "the local authority did not approve the exact plan")
            self._maintain_as(capability)
            loaded = self._load()
            if loaded is None or (
                loaded[0]["format"] in _MODERN_FORMATS
                and loaded[0]["through_seq"] == 0 and loaded[0]["generation"] == 1
                and loaded[0]["journal_id"] != journal_id
            ):
                self._commit_checkpoint({
                    "format": CHECKPOINT_FORMAT,
                    "journal_file": journal_file(1),
                    "journal_id": journal_id,
                    "generation": 1,
                    "through_seq": 0,
                    "head_sha256": genesis_head(journal_id),
                    "byte_length": 0,
                    "previous": self._previous(*loaded) if loaded is not None else None,
                    "repair_intent": None,
                    "compaction_intent": None,
                })
            else:
                checkpoint, _ = loaded
                if checkpoint["format"] == QUOTA_CHECKPOINT_FORMAT and (
                    self._anchor_store is not None or self._quota_store is not None
                ):
                    raise ProtocolRefusal("cross_legacy_admission_fenced", "legacy quota requires an independently bound successor")
                records, _ = self._verified(checkpoint)
                if str(checkpoint["journal_id"]).rsplit("-g", 1)[0] != f"cross-{enable_id}":
                    provisional = all(row["kind"] in CONTROLLER_KINDS for row in records)
                    prior_starts = [row for row in records if row.get("phase") == "enable_started"]
                    rollover = (
                        "disable_complete" in project(records).phases
                        and (self._anchor_store is None or self._observed_store() is None)
                        and all(row["enable_id"] != enable_id and row.get("enable_generation", 0) < enable_generation
                                for row in prior_starts)
                    )
                    if checkpoint["format"] not in _MODERN_FORMATS or not (provisional or rollover):
                        raise ProtocolRefusal("cross_journal_exists", "this root already has another cross journal")
                    if checkpoint["format"] == BOUND_QUOTA_FORMAT:
                        raise self._quota_refusal()
                    intent, _ = self._successor(checkpoint, records, _ADOPT + enable_id)
                    pending = dict(self._carry_opening(*loaded), compaction_intent=intent,
                                   previous=self._previous(*loaded))
                    payload = self._commit_checkpoint(pending)
                    successor, _ = self._finish_compaction(pending, payload)
                    self._collect_garbage(successor)
                if enable_generation == 1 and any(
                    row["id"] == started["id"] and "enable_generation" not in row for row in records
                ):
                    del started["enable_generation"]
            check(capability, started)
            if attended and self._anchor_store is None:
                raise ProtocolRefusal("cross_enable_authority_missing", "attended enable requires current independent evidence")
            result = self._append_with_reserve_file(capability, started, creating=attended)
            if attended:
                checkpoint, raw = self._load()
                rows, _ = self._verified(checkpoint)
                if result.status == "committed":
                    self._install_enable_create(started, checkpoint, raw, rows)
                elif result.status != "existing" or self._observed_store() is None:
                    raise ProtocolRefusal(result.code or "cross_enable_authority_missing",
                                          "the attended transaction has no current creation right")
        return self._read_committed(False)

    def cancel_partial_enable(self, capability: Capability, attended: Mapping[str, object]) -> bool:
        """Remove only the independent continuation for this exact incomplete enable."""
        if capability.writer != "L" or capability.verb != "disable" or capability.pair_id is not None:
            raise ProtocolRefusal("cross_capability_refused", "partial enable cancellation requires local disable")
        if self._anchor_store is None or not self.directory.is_dir():
            raise self._enable_inventory_refusal()
        with self._lock(exclusive=True):
            loaded = self._load()
            if loaded is None:
                raise self._enable_inventory_refusal()
            checkpoint, raw = loaded
            records, data = self._verified(checkpoint)
            start = next((semantic(row) for row in reversed(records) if row.get("phase") == "enable_started"), None)
            if (
                start is None or start != attended or start.get("schema_version") not in (1, 2) or self._seal() != _sha256(raw)
                or len(data) != checkpoint["byte_length"] or checkpoint["repair_intent"] is not None
                or checkpoint["compaction_intent"] is not None
            ):
                raise self._enable_inventory_refusal()
            index = next(i for i, row in enumerate(records) if semantic(row) == start)
            if any(row["kind"] not in {"cross_inventory_item", "cross_journal_tail_repaired"} for row in records[index + 1:]):
                raise self._enable_inventory_refusal()
            current = self._observed_store()
            if isinstance(current, EnableCreate):
                self._enable_initial(current, checkpoint, raw, records)
            elif current is None:
                return True
            elif current != self._anchor_coordinate(checkpoint, raw, records) or current.trust[2] != 1:
                raise self._enable_inventory_refusal()
            self._replace_anchor(current, None)
            return True

    def seal_legacy_quota_cutoff(
        self, capability: Capability, *, attended: Optional[Mapping[str, object]] = None,
    ) -> str:
        """Publish the inclusive cut under the injected exclusive legacy-opener lease."""
        if (capability.writer != "A" or capability.verb != "legacy_rollover" or capability.pair_id is not None
            or self._legacy_exclusion is None or self._anchor_store is None or self._quota_store is None
            or not self.directory.is_dir()):
            raise ProtocolRefusal("cross_quota_cutoff_unproved", "legacy rollover requires exclusive opener evidence")
        with self._legacy_exclusion() as charged_days:
            with self._lock(exclusive=True):
                loaded = self._load()
                if loaded is None:
                    raise self._quota_refusal()
                checkpoint, raw = loaded
                recut = (
                    checkpoint["format"] == BOUND_QUOTA_FORMAT
                    and self._observed_store() is None and self._observed_quota() is None
                )
                if not recut:
                    self._maintain_as(Capability("A", verb="enable"))
                    checkpoint, raw = self._load()
                records, data = self._verified(checkpoint)
                if (self._seal() != _sha256(raw) or len(data) != checkpoint["byte_length"]
                    or checkpoint["compaction_intent"] is not None or checkpoint["repair_intent"] is not None):
                    raise self._quota_refusal()
                target = None
                if attended is not None:
                    target = self._quota_attended_target(attended, records)
                if checkpoint["format"] == BOUND_QUOTA_FORMAT and not recut:
                    if self._quota_pending(checkpoint, data) or (
                        target is not None and target != self._quota_target(checkpoint["journal_id"], checkpoint["generation"])
                    ):
                        raise self._quota_refusal()
                    return checkpoint["quota"]["day"]
                state = project(records)
                if "disable_complete" not in state.phases or state.open_ceremonies:
                    raise ProtocolRefusal("cross_quota_cutoff_unproved", "legacy teardown and opening closure must settle")
                prior_day = None if recut else checkpoint.get("quota", {}).get("day")
                known = [*charged_days, *filter(None, (state.quota_day, prior_day))]
                cutoff = legacy_quota_cutoff(self._utc_day(), known, exclusive=True)
                return self._start_quota_migration(checkpoint, raw, records, cutoff, target)

    def seal_authenticated_quota_history(
        self, capability: Capability, proof: object, *, attended: Optional[Mapping[str, object]] = None,
    ) -> dict:
        """Bind an independently authenticated complete charge history to its actual cut.

        The injected verifier authenticates completeness for the supplied six-field
        source and returns immutable (opening ID, token, UTC day) tuples, or None.
        It executes under the ceremony and cross exclusions.
        """
        if (capability.writer != "A" or capability.verb != "legacy_rollover" or capability.pair_id is not None
            or self._legacy_exclusion is None or self._anchor_store is None or self._quota_store is None
            or self._authenticate_quota_history is None or not self.directory.is_dir()):
            raise self._quota_refusal()
        with self._legacy_exclusion():
            with self._lock(exclusive=True):
                loaded = self._load()
                if loaded is None or self._observed_quota() is not None:
                    raise self._quota_refusal()
                checkpoint, raw = loaded
                records, data = self._verified(checkpoint)
                if (self._seal() != _sha256(raw) or len(data) != checkpoint["byte_length"]
                    or checkpoint["compaction_intent"] is not None or checkpoint["repair_intent"] is not None
                    or "completion_repair" in checkpoint):
                    raise self._quota_refusal()
                prior = self._observed_store()
                stable = prior.prior if isinstance(prior, Narrowing) else prior
                if stable is not None and stable != self._anchor_coordinate(checkpoint, raw, records):
                    raise self._anchor_refusal()
                if isinstance(prior, Narrowing) and self._narrowing_semantic_suffix(prior, checkpoint, records):
                    raise self._anchor_refusal()
                if stable is None and (
                    "disable_complete" not in project(records).phases or project(records).open_ceremonies
                ):
                    raise self._quota_refusal()
                source = sealed_descriptor(self._position_key(checkpoint), _sha256(raw))
                try:
                    history = self._authenticate_quota_history(dict(source), proof)
                except OSError as exc:
                    raise self._quota_refusal() from exc
                if not isinstance(history, tuple):
                    raise ProtocolRefusal("cross_quota_history_unproved", "the complete charge history is not authenticated")
                day, count = fold_quota_history(history)
                authenticated = set(history)
                if any((row["id"], row.get("ceremony_token"), row.get("utc_day")) not in authenticated
                       for row in records if row["kind"] == "cross_ceremony_opened"):
                    raise ProtocolRefusal("cross_quota_history_unproved", "authenticated history omits a retained opening")
                quota = {"mode": "exact", "day": day, "count": count, "source": source}
                target = self._quota_attended_target(attended, records) if attended is not None else None
                if stable is None:
                    if len(self._quota_inventory_items()) >= 160:
                        raise ProtocolRefusal("cross_quota_inventory_full", "quota migration needs one independent inventory slot")
                    self._publish_quota_migration(checkpoint, raw, records, quota, target)
                elif target is None:
                    self._publish_anchored_quota(checkpoint, raw, records, quota, prior, stable)
                else:
                    raise self._quota_refusal()
                return quota

    def _publish_anchored_quota(self, checkpoint, raw, records, quota, prior, stable):
        intent, body = self._successor(checkpoint, records, _MIGRATION, quota_projection=quota)
        successor_rows, _ = verify_prefix(body, intent["successor_journal_id"])
        admit_compaction(project(records), project(successor_rows), self.budget, self.data_budget, resource=None)
        controller = self._quota_controller(checkpoint, raw, quota, intent)
        frame = encode_frame(controller)
        digest = _sha256(frame)
        filename = f"journal-plan-{digest}.commit"
        existing = self.storage.read(filename)
        if existing is None:
            self.storage.create(filename, frame)
        elif existing != frame:
            raise self._quota_refusal()
        self.storage.sync(filename)
        self.storage.sync_directory()
        handoff = Handoff(stable, self._position_key(checkpoint), _sha256(raw), digest,
                          self._successor_position(intent), intent["successor_sha256"], _MIGRATION, intent["quota_sha256"])
        replacement = NarrowingHandoff(prior, handoff) if isinstance(prior, Narrowing) else handoff
        controller_raw = self._publish_handoff(raw, controller, prior, replacement)
        opening, sealed = self._finish_compaction(controller, controller_raw)
        self._settle_anchor(opening, sealed, successor_rows, body)
        latest, _ = self._load()
        self._collect_garbage(latest)

    def _quota_attended_target(self, attended, records):
        start = approved_start(attended.get("enable_id"), attended.get("enable_generation"),
                               attended.get("token"), attended.get("plan"), schema_version=attended.get("schema_version"))
        starts = [row for row in records if row.get("phase") == "enable_started"]
        if (start != attended or self._enable_approval is None or self._enable_approval("start", start) is not True
            or any(row["enable_id"] == start["enable_id"] or row.get("token") == start["token"]
                   or row.get("enable_generation", 0) >= start["enable_generation"] for row in starts)):
            raise self._quota_refusal()
        return start["enable_id"]

    def _quota_target(self, journal_id, generation):
        match = re.fullmatch(r"cross-([0-9a-f]{32})-g" + str(generation), str(journal_id))
        if match is None:
            raise self._quota_refusal()
        return match.group(1)

    def _start_quota_migration(self, checkpoint, raw, records, cutoff, target):
        if self._anchor_store is None or self._observed_store() is not None or self._observed_quota() is not None:
            raise self._quota_refusal()
        if len(self._quota_inventory_items()) >= 160:
            raise ProtocolRefusal("cross_quota_inventory_full", "quota migration needs one independent inventory slot")
        predecessor = self._position_key(checkpoint)
        previous_digest = _sha256(raw)
        quota = {"mode": "legacy_blocked_through", "day": cutoff,
                 "source": sealed_descriptor(predecessor, previous_digest)}
        self._publish_quota_migration(checkpoint, raw, records, quota, target)
        return cutoff

    def _publish_quota_migration(self, checkpoint, raw, records, quota, target):
        intent, body = self._successor(checkpoint, records, _MIGRATION, target_enable=target, quota_projection=quota)
        successor = self._successor_position(intent)
        admit_compaction(project(records), project(verify_prefix(body, intent["successor_journal_id"])[0]),
                         self.budget, self.data_budget, resource=None)
        witness = QuotaMigration(self._position_key(checkpoint), _sha256(raw), successor, intent["successor_sha256"],
                                 successor_quota_sha256(quota), secrets.token_hex(16))
        candidate = self._quota_controller(checkpoint, raw, quota, intent)
        self._keep_store_predecessor(raw)
        name = f"journal-quota-{witness.token}.commit"
        self.storage.create(name, encode_frame(candidate))
        self.storage.sync(name)
        self.storage.sync_directory()
        try:
            if not self._quota_store.compare_and_set(None, witness) or self._observed_quota() != witness:
                raise self._quota_refusal()
        except OSError as exc:
            raise self._quota_refusal() from exc
        self._finish_quota_migration(witness, checkpoint, raw)

    def _quota_inventory_items(self) -> tuple:
        if self._quota_inventory is None:
            raise self._quota_refusal()
        items = tuple(self._quota_inventory())
        if any(not isinstance(item, str) or not item or item.startswith("quota-migration:") for item in items):
            raise self._quota_refusal()
        if len(set(items)) != len(items):
            raise self._quota_refusal()
        return items

    def quota_migration_inventory(self) -> tuple:
        """Read the fixed migration slot independently of journal publication."""
        with self._read_lock() as lock:
            if lock != "locked" or self._quota_store is None:
                raise self._quota_refusal()
            items = self._quota_inventory_items()
            witness = self._quota_slot()
            if witness is None:
                return items
            if isinstance(witness, (QuotaMigration, QuotaMigrationPending)) and _ENABLE_ID.fullmatch(witness.token):
                return (*items, f"quota-migration:{witness.token}")
            raise self._quota_refusal()

    def quota_migration_disposition(self, token: str) -> dict:
        """Measure the token-owned item without issuing a store replacement."""
        if not isinstance(token, str) or not _ENABLE_ID.fullmatch(token):
            raise self._quota_refusal()
        result = {"item": f"quota-migration:{token}", "class": "unknown"}
        with self._read_lock() as lock:
            if lock != "locked" or self._quota_store is None:
                return result
            try:
                current = self._quota_slot()
            except ProtocolRefusal:
                return result
            if current is None:
                result["class"] = "confirmed_absent"
            elif isinstance(current, QuotaMigration):
                result["class"] = "still_reachable" if current.token == token else "foreign_or_changed"
        return result

    def dispose_quota_migration(self, capability: Capability, token: str) -> dict:
        """Measure and remove only the selected migration item of a stopped root."""
        if (capability.writer != "L" or capability.verb != "disable" or capability.pair_id is not None
            or self._legacy_exclusion is None or self._anchor_store is None or self._quota_store is None
            or not isinstance(token, str) or not _ENABLE_ID.fullmatch(token) or not self.directory.is_dir()):
            raise self._quota_refusal()
        with self._legacy_exclusion():
            with self._lock(exclusive=True):
                loaded = self._load()
                if loaded is None or self._observed_store() is not None:
                    raise self._quota_refusal()
                checkpoint, raw = loaded
                records, data = self._verified(checkpoint)
                if (self._seal() != _sha256(raw) or len(data) != checkpoint["byte_length"]
                    or checkpoint["compaction_intent"] is not None or checkpoint["repair_intent"] is not None
                    or "disable_complete" not in project(records).phases):
                    raise self._quota_refusal()
                current = self._observed_quota()
                if current is not None:
                    if current.token != token:
                        raise self._quota_refusal()
                    try:
                        if not self._quota_store.compare_and_set(current, None) or self._observed_quota() is not None:
                            raise self._quota_refusal()
                    except OSError as exc:
                        raise self._quota_refusal() from exc
                return {"item": f"quota-migration:{token}", "class": "confirmed_absent"}

    def _retained_quota_witness(self, token):
        raw = self.storage.read(f"journal-quota-{token}.commit")
        if raw is None:
            raise self._quota_refusal()
        controller = parse_checkpoint(raw)
        intent = controller["compaction_intent"]
        previous = controller["previous"]
        if (controller["format"] != BOUND_QUOTA_FORMAT or intent is None or intent["reason"] != _MIGRATION
            or previous is None):
            raise self._quota_refusal()
        return QuotaMigration(self._position_key(previous), previous["checkpoint_sha256"],
                              self._successor_position(intent), intent["successor_sha256"],
                              intent["quota_sha256"], token)

    def _fund_quota_anchor(self, items):
        if len(items) + 1 + int("anchor" not in items) > 160:
            raise ProtocolRefusal("cross_quota_inventory_full", "the migration witness and anchor need funded slots")
        if "anchor" not in items and (
            self._reserve_quota_anchor is None or self._reserve_quota_anchor() is not True
        ):
            raise self._quota_refusal()
        items = self._quota_inventory_items()
        if "anchor" not in items or len(items) + 1 > 160:
            raise self._quota_refusal()
        return items

    def transfer_quota_migration(self, capability: Capability, token: str) -> bool:
        """Transfer a measured migration obligation to an independently authorized opening."""
        if (capability.writer != "A" or capability.verb != "legacy_rollover" or capability.pair_id is not None
            or self._legacy_exclusion is None or self._anchor_store is None or self._quota_store is None
            or not isinstance(token, str) or not _ENABLE_ID.fullmatch(token)):
            raise self._quota_refusal()
        with self._legacy_exclusion():
            with self._lock(exclusive=True):
                loaded = self._load()
                if loaded is None:
                    raise self._quota_refusal()
                checkpoint, raw = loaded
                records, data = self._verified(checkpoint)
                witness = self._retained_quota_witness(token)
                current = self._observed_quota()
                candidate = self._anchor_coordinate(checkpoint, raw, records)
                if (
                    checkpoint["format"] != BOUND_QUOTA_FORMAT or candidate is None
                    or checkpoint["compaction_intent"] is not None or checkpoint["repair_intent"] is not None
                    or self._seal() != _sha256(raw) or len(data) != checkpoint["byte_length"]
                    or (current is not None and current != witness)
                    or not self._quota_reference_matches(witness, checkpoint, data)
                ):
                    raise self._quota_refusal()
                anchor = self._observed_store()
                items = self._quota_inventory_items()
                if anchor is None and current == witness:
                    required = len(items) + 1 + int("anchor" not in items)
                    if required > 160:
                        raise ProtocolRefusal("cross_quota_inventory_full", "the migration witness and anchor need funded slots")
                    if (self._authorize_quota_transfer is None
                        or self._authorize_quota_transfer(capability, candidate, witness) is not True):
                        raise self._quota_refusal()
                    items = self._fund_quota_anchor(items)
                    self._replace_anchor(None, candidate)
                    anchor = self._observed_store()
                if anchor != candidate or not self._anchored_quota_matches(checkpoint, data):
                    raise self._quota_refusal()
                if current is None:
                    return True
                if "anchor" not in items or len(items) + 1 > 160:
                    raise ProtocolRefusal("cross_quota_inventory_full", "the migration witness and anchor need funded slots")
                if self._observed_quota() != witness:
                    raise self._quota_refusal()
                try:
                    if not self._quota_store.compare_and_set(witness, None) or self._observed_quota() is not None:
                        raise self._quota_refusal()
                except OSError as exc:
                    raise self._quota_refusal() from exc
                if self._observed_store() != anchor:
                    raise self._quota_refusal()
                return True

    def attended_reviews(self) -> Tuple[Dict[str, object], ...]:
        """Project exact published enable and activation review groups."""
        view = self.read_committed(require_settled=True)
        starts = {}
        reviews = {}
        for row in view.records:
            if row.get("schema_version") not in (1, 2):
                continue
            if row["kind"] == "cross_pair_active":
                item = activation_review_item(row)
            elif row["kind"] == "cross_optin":
                enable = row.get("enable_id"), row.get("enable_generation")
                if row.get("phase") == "enable_started":
                    starts[enable] = row
                if row.get("phase") != "enable_complete":
                    continue
                if enable not in starts:
                    raise _corrupt("an attended completion has no retained approval")
                item = review_item(starts[enable], row)
            else:
                continue
            if item["id"] in reviews:
                raise _corrupt("an attended review identity is duplicated")
            reviews[item["id"]] = item
        return tuple(reviews.values())

    @staticmethod
    def _pair_material_refusal():
        return ProtocolRefusal("cross_pairing_material_unknown", "the exact pair material could not be verified")

    def _require_pair_material(self, activation, *, phase):
        try:
            if self._prove_pair_material is not None and self._prove_pair_material(deepcopy(activation), phase=phase) is True:
                return
        except OSError as exc:
            raise self._pair_material_refusal() from exc
        raise self._pair_material_refusal()

    @staticmethod
    def _require_pair_opening(records, activation):
        opening = next((row for row in reversed(records) if row["kind"] == "cross_ceremony_opened"
                        and row.get("ceremony_id") == activation["opening_id"]), None)
        if (opening is None or activation["opening_id"] not in project(records).open_ceremonies
            or any(opening.get(key) != activation[key] for key in ("ceremony_token", "enable_id", "enable_generation"))):
            raise ProtocolRefusal("cross_ceremony_token_invalid", "the activation needs its exact local opening")

    def _pair_authority(self, capability, activation):
        candidate = self._validate(activation)
        validate_pair_activation(candidate)
        check(capability, candidate)
        verb = "pair_show" if candidate["role"] == "S" else "pair_join"
        if capability.writer != "A" or capability.verb != verb or self._prove_pair_material is None:
            raise self._pair_material_refusal()
        return deepcopy(candidate)

    @staticmethod
    def _pair_pending():
        return ProtocolRefusal("cross_publication_pending", "the pair journal needs a settled publication")

    def _live_pair_prefix(self, activation, lock):
        if lock != "locked" or self._maintenance_due():
            raise self._pair_pending()
        loaded = self._load()
        if loaded is None:
            raise self._pair_pending()
        checkpoint, raw = loaded
        records, _ = self._verified(checkpoint)
        state = project(records)
        anchor = self._anchor_coordinate(checkpoint, raw, records)
        if (checkpoint["format"] not in {CHECKPOINT_FORMAT, BOUND_QUOTA_FORMAT}
            or self._anchor_store is None or anchor is None or self._observed_store() != anchor
            or anchor.trust[:2] != (activation["enable_id"], activation["enable_generation"])
            or "enable_complete" not in state.phases or "disable_started" in state.phases):
            raise self._pair_pending()
        self._check_pairing_material(records, require_attended=True)
        return checkpoint, records, state, anchor

    def _preflight_pair_locked(self, activation, lock):
        checkpoint, records, state, anchor = self._live_pair_prefix(activation, lock)
        pair = activation["pair_id"]
        current = state.active_pairs.get(pair)
        if current is not None and semantic(current) == activation:
            return next(row for row in records if row["id"] == activation["id"])
        self._require_pair_opening(records, activation)
        if any((row["kind"] in {"cross_pair_active", "cross_pair_revoked"} and row.get("pair_id") == pair)
               or (row["kind"] == "cross_pair_active" and row.get("schema_version") == 1
                   and row.get("ceremony_id") == activation["ceremony_id"]) for row in records):
            raise ProtocolRefusal("cross_pairing_collision", "this root has already used the pair identity")
        if len(state.active_pairs) >= 64:
            raise ProtocolRefusal("cross_pair_capacity", "this root already has 64 active pairs")
        if any(row.get("label") == activation["label"] for row in state.active_pairs.values()):
            raise ProtocolRefusal("cross_pair_label_in_use", "the pair label is already active")
        check_trust_reserve(state, activation, anchor.trust[2] + 1)
        frame = self._candidate_frame(checkpoint, records, activation)
        admit(state, decode_frames(frame)[0], len(frame), self.budget, self.data_budget)
        return None

    def preflight_pair_activation(self, capability: Capability, activation: Mapping[str, object]) -> None:
        """Check a proposed activation under a nonmutating, settled journal read."""
        candidate = self._pair_authority(capability, activation)
        with self._read_lock() as lock:
            self._preflight_pair_locked(candidate, lock)

    def activate_pair(self, capability: Capability, activation: Mapping[str, object], *, prepare) -> AppendResult:
        """Preflight, prepare local material and publish under one journal exclusion."""
        candidate = self._pair_authority(capability, activation)
        with self._read_lock(exclusive=True) as lock:
            existing = self._preflight_pair_locked(candidate, lock)
            if existing is not None:
                self._require_pair_material(candidate, phase="handoff")
                return AppendResult("existing", candidate["id"], existing["seq"])
            prepare(deepcopy(candidate))
            self._require_pair_material(candidate, phase="publish")
            self._preflight_pair_locked(candidate, lock)
            self._origin = capability.writer
            return self._append_with_reserve_file(capability, candidate)

    def _verify_pair_handoff(self, candidate, lock):
        _, _, state, anchor = self._live_pair_prefix(candidate, lock)
        active = state.active_pairs.get(candidate["pair_id"])
        if active is None or semantic(active) != candidate:
            raise self._pair_pending()
        self._require_pair_material(candidate, phase="handoff")
        if self._observed_store() != anchor:
            raise self._pair_pending()

    def with_pair_activation(self, capability: Capability, activation: Mapping[str, object], *, enqueue):
        """Recheck the live pair and atomically permit one local queue deposit."""
        candidate = self._pair_authority(capability, activation)
        with self._read_lock(exclusive=True) as lock:
            self._verify_pair_handoff(candidate, lock)
            return enqueue()

    def record_pair_seen(self, capability: Capability, activation: Mapping[str, object], *, verify_testimony) -> AppendResult:
        """Publish the show-side testimony only while the exact pair remains live."""
        candidate = self._pair_authority(capability, activation)
        if capability.verb != "pair_show":
            raise self._pair_material_refusal()
        record = pair_seen_record(candidate)
        check(capability, record)
        with self._read_lock(exclusive=True) as lock:
            self._verify_pair_handoff(candidate, lock)
            if verify_testimony(deepcopy(candidate)) is not True:
                raise self._pair_material_refusal()
            self._origin = capability.writer
            return self._append_with_reserve_file(capability, record)

    def lookup_pair(self, pair_id: str) -> dict:
        """Read local publication and the immutable pair and revocation identities."""
        if not isinstance(pair_id, str) or _ENABLE_ID.fullmatch(pair_id) is None:
            raise self._pair_material_refusal()
        view = self.read_committed()
        if view.pending:
            return {"state": "pending", "pair_id": pair_id}
        rows = {row["kind"]: semantic(row) for row in view.records if row.get("pair_id") == pair_id
                and row["kind"] in {"cross_pair_active", "cross_pair_revoked"}}
        activation = rows.get("cross_pair_active")
        revoked = rows.get("cross_pair_revoked")
        review = None if activation is None or activation.get("schema_version") != 1 else activation_review_item(activation)
        state = "revoked" if revoked is not None else "absent"
        if revoked is None and activation is not None:
            state = "published" if review is not None else "legacy_fenced"
        return {"state": state, "pair_id": pair_id, "activation": activation, "review": review, "revocation": revoked}

    def _fence_legacy_pair(self, state, pair_id):
        active = state.active_pairs.get(pair_id)
        if self._prove_pair_material is not None and active is not None and active.get("schema_version") != 1:
            raise ProtocolRefusal("cross_legacy_admission_fenced", "an old activation grants no live pair authority")

    def _require_admitting_pair(self, state, pair_id, checkpoint, raw, records):
        if self._fixture_mode:
            self._fence_legacy_pair(state, pair_id)
            return
        self._require_root_providers()
        if self._anchor_pending(checkpoint, raw, records):
            raise self._pair_pending()
        if pair_id is not None:
            pair = state.active_pairs.get(pair_id)
            if pair is None or pair.get("schema_version") != 1:
                raise self._pair_material_refusal()
            self._require_pair_material(semantic(pair), phase="traffic")

    def _validate_live_opening(self, candidate, checkpoint, raw, records):
        self._check_pairing_material(records, require_attended=True)
        anchor = self._anchor_coordinate(checkpoint, raw, records)
        fields = {"id", "kind", "schema_version", "enable_id", "enable_generation", "ceremony_id", "ceremony_token"}
        opening, token = candidate.get("ceremony_id"), candidate.get("ceremony_token")
        if (set(candidate) != fields or not _integer(candidate["schema_version"], 1, 1)
            or not isinstance(opening, str) or _ENABLE_ID.fullmatch(opening) is None
            or not isinstance(token, str) or _ENABLE_ID.fullmatch(token) is None or token == opening
            or candidate["id"] != f"cross-ceremony-opened-{opening}" or self._anchor_store is None
            or anchor is None or not _integer(candidate["enable_generation"], 1, MAX_INTEGER)
            or anchor.trust[:2] != (candidate["enable_id"], candidate["enable_generation"])
            or self._observed_store() != anchor):
            raise self._pair_pending()
        if any(row["id"] != candidate["id"] and row["kind"] in {"cross_ceremony_opened", "cross_pair_active"}
               and (row.get("ceremony_token") == token or row.get("opening_id") == opening) for row in records):
            raise ProtocolRefusal("cross_pairing_collision", "this root has already used the opening identity")

    @staticmethod
    def _gate_refusal():
        return ProtocolRefusal("cross_gate_ownership_unproved", "the locked journal and current gate owners are unproved")

    def _lan_prefix(self, *, permit_stopped=False):
        if self._exclusive_token() is None or not self._root_providers_present() or self._maintenance_due():
            raise self._gate_refusal()
        loaded = self._load()
        if loaded is None:
            raise self._gate_refusal()
        checkpoint, raw = loaded
        records, _ = self._verified(checkpoint)
        state = project(records)
        anchor = self._anchor_coordinate(checkpoint, raw, records)
        if (anchor is None or self._observed_store() != anchor or "enable_complete" not in state.phases
            or "disable_complete" in state.phases or ("disable_started" in state.phases and not permit_stopped)):
            raise self._gate_refusal()
        self._check_pairing_material(records, require_attended=True)
        start = next(semantic(row) for row in reversed(records) if row.get("phase") == "enable_started")
        if anchor.trust[:2] != (start["enable_id"], start["enable_generation"]):
            raise self._gate_refusal()
        return checkpoint, records, state, start

    def _gate_journal_id(self, checkpoint, records, start):
        # A carried start has been rechained. Its sealed H1 proof still fixes
        # the original journal ID via the retained anchor inventory witness.
        latest_checkpoint = max((i for i, row in enumerate(records) if row["kind"] == "cross_trust_checkpoint"),
                                default=-1)
        if any(row["id"] == start["id"] for row in records[latest_checkpoint + 1:]):
            return checkpoint["journal_id"]
        scope = start["enable_id"], start["enable_generation"]
        anchor_item = next((row for row in records if row["kind"] == "cross_inventory_item"
                            and row["item"] == "anchor" and row["phase"] == "created"
                            and (row["enable_id"], row["enable_generation"]) == scope
                            and row["token"] == start["token"]), None)
        if anchor_item is None:
            raise self._gate_refusal()
        original = set()
        for name in self.storage.names():
            match = re.fullmatch(r"journal-proof-([0-9a-f]{64})\.commit", name)
            if match is None:
                continue
            proof = self.storage.read(name)
            if proof is None or _sha256(proof) != match[1]:
                continue
            value = parse_checkpoint(proof)
            if not value["journal_id"].startswith(f"cross-{scope[0]}-g"):
                continue
            h1 = self._anchor_coordinate(value, proof, (start,))
            if h1 is not None and _sha256(encode_frame(stable_descriptor(h1))[:-1]) == anchor_item["proof_sha256"]:
                if self.storage.read(name.removesuffix(".commit") + ".seal") == encode_frame({
                    "format": SEAL_FORMAT, "checkpoint_sha256": match[1],
                }):
                    original.add(value["journal_id"])
        if len(original) != 1:
            raise self._gate_refusal()
        return original.pop()

    @contextmanager
    def gate_transaction(self):
        """Hold the shared writer exclusion for a fresh LAN or SSH gate operation."""
        with self._read_lock(exclusive=True) as lock:
            if lock != "locked":
                raise self._gate_refusal()
            yield self._gate_projection(writable=True)

    def current_gate(self, *, existing_open: bool = False) -> GateProjection:
        """Read gate owners and floors under the caller's existing exclusion."""
        return self._gate_projection(existing_open=existing_open)

    def _gate_projection(self, *, existing_open=False, writable=False):
        lease = self._exclusive_token()
        opened = getattr(self._lock_context, "authenticating_open", None)
        if (lease is None or (existing_open and opened is None)):
            raise self._gate_refusal()
        checkpoint, records, state, start = self._lan_prefix(permit_stopped=existing_open)
        journal_id = self._gate_journal_id(checkpoint, records, start)
        raw = self.storage.read("gate-state")
        if raw is None or self.storage.read(".gate-state.tmp") is not None:
            raise self._gate_refusal()
        image = GateImage.decode(raw)
        if image.journal_id != journal_id:
            raise self._gate_refusal()
        enable, epoch = start["enable_id"], start["enable_generation"]
        active = state.active_pairs if not existing_open else {opened.pair_id: state.active_pairs.get(opened.pair_id)}
        history = gate_history(records, enable, epoch, active.keys())
        addresses = {row["pair_id"]: semantic(deepcopy(row)) for row in records
                     if row["kind"] == "cross_peer_address" and row.get("pair_id") in active}
        pairs = []
        for pair_id, pair in sorted(active.items()):
            if pair is None or pair.get("schema_version") != 1 or (pair["enable_id"], pair["enable_generation"]) != (enable, epoch):
                raise self._gate_refusal()
            self._require_pair_material(semantic(pair), phase="traffic")
            owner = GateOwner(journal_id, enable, epoch, pair["ceremony_token"], pair_id)
            counters = {}
            outgoing = "S-to-T" if pair["role"] == "S" else "T-to-S"
            for direction in DIRECTIONS:
                next_counter, inbound, _ = image.counters(owner, direction)
                floors = history.get(f"{pair_id}/{direction}", GateFloors(0, 0, 1, 1))
                if (next_counter < floors.outbound_min_next if direction == outgoing else inbound < floors.max_session):
                    raise self._gate_refusal()
                counters[direction] = floors
            pairs.append(GatePairProjection(owner, semantic(deepcopy(pair)), counters, addresses.get(pair_id)))
        active_keys = frozenset(key for pair in pairs for key in pair.owner.keys())
        return GateProjection(_GateStorage(self, lease, writable, active_keys), image, tuple(pairs))

    @staticmethod
    def _endpoint_authority(capability):
        CrossJournal._require_root_verb(capability, {"I"}, "endpoint_start")

    @contextmanager
    def _listener_lock(self):
        if self._exclusive_token() is not None:
            yield "locked"
        else:
            with self._read_lock(exclusive=True) as lock:
                yield lock

    def _endpoint_body(self, identifier, binding, start):
        if (self._verify_endpoint is None or not isinstance(identifier, str) or not _ENABLE_ID.fullmatch(identifier)
            or not isinstance(binding, Mapping) or set(binding) != {
            "approved", "bound", "dual_stack", "pid", "start_token", "socket_token",
        }):
            raise endpoint_refusal()
        record = dict(deepcopy(dict(binding)), id=f"cross-endpoint-{identifier}", kind="remote_endpoint_registered",
                      schema_version=1, enable_id=start["enable_id"], enable_generation=start["enable_generation"])
        validate_endpoint_registration(record)
        if endpoint_bind_spec(record["approved"]) not in start["plan"]["listen"]:
            raise endpoint_refusal()
        return record

    def _endpoint_state(self, identifier, binding, owner, *, permit_stopped=False):
        _, records, _, start = self._lan_prefix(permit_stopped=permit_stopped)
        candidate = self._endpoint_body(identifier, binding, start)
        stored = next((row for row in records if row["id"] == candidate["id"]), None)
        if stored is None:
            return "missing"
        if semantic(stored) != candidate:
            raise endpoint_refusal()
        observed = self._verify_endpoint(candidate, owner)
        return observed if observed in ("bound", "listening", "inert") else "pending"

    def bind_allowed(self, approved: Mapping[str, object]) -> bool:
        """Check the settled, attended bind before the local controller opens a socket."""
        with self._listener_lock() as lock:
            if lock != "locked" or self._verify_endpoint is None:
                return False
            try:
                _, _, _, start = self._lan_prefix()
                return endpoint_bind_spec(dict(approved)) in start["plan"]["listen"]
            except (OSError, ProtocolRefusal, TypeError, ValueError):
                return False

    def register_listener(self, capability: Capability, identifier: str, binding: Mapping[str, object], *, owner):
        """Publish the exact locally bound socket and process before listening."""
        self._endpoint_authority(capability)
        with self._listener_lock() as lock:
            if lock != "locked":
                raise endpoint_refusal()
            _, records, _, start = self._lan_prefix()
            record = self._endpoint_body(identifier, binding, start)
            if self._verify_endpoint(deepcopy(record), owner) != "bound":
                raise endpoint_refusal()
            if any(row["id"] != record["id"] and row.get("socket_token") == record["socket_token"] for row in records):
                raise endpoint_refusal()
            self._origin = capability.writer
            return self._append_with_reserve_file(capability, record)

    def listener_current(self, identifier: str, binding: Mapping[str, object], *, owner, listening: bool = False) -> bool:
        """Recheck the exact registered socket, including in a live-open callback."""
        previous_open = getattr(self._lock_context, "authenticating_open", None)
        with self._listener_lock() as lock:
            if lock != "locked":
                return False
            try:
                state = self._endpoint_state(identifier, binding, owner, permit_stopped=previous_open is not None)
            except (OSError, ProtocolRefusal):
                return False
            return state == "listening" or (not listening and state == "bound")

    def resolve_listener(self, capability: Capability, identifier: str, binding: Mapping[str, object], *, owner) -> str:
        """Measure this controller's exact prior registration without issuing it."""
        self._endpoint_authority(capability)
        with self._listener_lock() as lock:
            if lock != "locked":
                return "pending"
            try:
                return self._endpoint_state(identifier, binding, owner, permit_stopped=True)
            except (OSError, ProtocolRefusal):
                return "pending"

    def dispose_listener(self, capability: Capability, identifier: str, binding: Mapping[str, object], *, owner, dispose) -> str:
        """Dispose only the original bound, non-listening socket and measure absence."""
        self._endpoint_authority(capability)
        with self._listener_lock() as lock:
            if lock != "locked":
                raise endpoint_refusal()
            state = self._endpoint_state(identifier, binding, owner, permit_stopped=True)
            if state == "inert":
                return state
            if state != "bound":
                raise endpoint_refusal()
            dispose()
            return ("inert" if self._endpoint_state(identifier, binding, owner, permit_stopped=True) == "inert"
                    else "pending")

    def _local_dial_source(self, capability, sender, source_id, records):
        if (capability.writer != "L" or capability.verb not in ("ask", "send")
            or self._authorize_local_send is None or self._authorize_local_send(capability, sender) is not True):
            raise ProtocolRefusal("cross_capability_refused", "dial requires an authenticated local caller")
        outbound = next((row for row in records if row["id"] == source_id and row["kind"] == "remote_outbound"
                         and row.get("schema_version") == 2), None)
        if (outbound is None or outbound["pair_id"] != capability.pair_id or outbound["verb"] != capability.verb
            or outbound["envelope"]["sender_node"] != sender
            or any(row["kind"] == "remote_sender_replay" and row["envelope_id"] == outbound["envelope_id"]
                   and row["enable_id"] == outbound["enable_id"] for row in records)):
            raise ProtocolRefusal("cross_delivery_request_invalid", "dial requires an eligible immutable sender item")
        state = project(records)
        self._fence_legacy_pair(state, capability.pair_id)
        check_data_scope(state, outbound, capability.pair_id)
        if "enable_complete" not in state.phases or capability.pair_id not in state.active_pairs:
            raise ProtocolRefusal("cross_pair_scope_refused", "dial requires the active pair")
        return semantic(outbound)

    def begin_local_dial(self, capability: Capability, record_id: str, sender: str) -> LocalDial:
        """Publish the authorized local attempt before acquiring transport witnesses."""
        self._require_root_providers()
        if self._dial_prerequisites is None or not self.directory.is_dir() or capability.writer != "L":
            raise ProtocolRefusal("cross_capability_refused", "dial requires local prerequisites")
        with self._lock(exclusive=True):
            loaded = self._load()
            if loaded is None or self._maintenance_due():
                raise ProtocolRefusal("cross_publication_pending", "dial requires a settled root")
            records, _ = self._verified(loaded[0])
            outbound = self._local_dial_source(capability, sender, record_id, records)
            self._require_admitting_pair(project(records), capability.pair_id, *loaded, records)
            if self._dial_prerequisites(capability, outbound) is not True:
                raise ProtocolRefusal("cross_capability_refused", "dial requires local prerequisites")
            row = {key: outbound[key] for key in ("enable_id", "enable_generation", "pair_id", "direction",
                                                  "envelope_id", "envelope_digest", "delivery_seq")}
            row.update(id="cross-local-dial-" + secrets.token_hex(16), kind="remote_transport_attempt",
                       schema_version=1, outbound_id=record_id)
            check(capability, row)
            result = self._append_with_reserve_file(capability, row)
            if result.status != "committed":
                raise ProtocolRefusal(result.code or "cross_publication_pending", "the local dial attempt is unsettled")
            dial = LocalDial(capability, sender, encode_frame(outbound), encode_frame(row))
            self._pending_dials.add(dial)
            return dial

    def _current_dial_source(self, dial, records):
        if dial not in self._pending_dials:
            raise ProtocolRefusal("cross_stream_owner_invalid", "the local dial attempt is no longer current")
        source, = decode_frames(dial.outbound_frame)
        attempt, = decode_frames(dial.attempt_frame)
        outbound = self._local_dial_source(dial.capability, dial.sender, source["id"], records)
        if outbound != source or not any(semantic(row) == attempt for row in records):
            raise ProtocolRefusal("cross_stream_owner_invalid", "the local dial source or attempt is unproved")
        return source, attempt

    def dial_current(self, dial: LocalDial) -> bool:
        """Revalidate a volatile local attempt during its gate transaction."""
        if self._exclusive_token() is None:
            raise self._gate_refusal()
        try:
            _, records, state, _ = self._lan_prefix()
            source, _ = self._current_dial_source(dial, records)
            pair = state.active_pairs[source["pair_id"]]
            if pair.get("schema_version") != 1:
                return False
            self._require_pair_material(semantic(pair), phase="traffic")
            return (source["enable_id"], source["enable_generation"]) == (
                pair["enable_id"], pair["enable_generation"]
            ) and (source["direction"] == "S-to-T") == (pair["role"] == "S")
        except (OSError, ProtocolRefusal):
            return False

    def discard_local_dial(self, dial: LocalDial) -> None:
        """Retire only the volatile dial witness, preserving all published history."""
        with self._listener_lock():
            self._pending_dials.discard(dial)

    def open_authenticated(
        self, proof: Mapping[str, object], stream: object, *, dial: Optional[LocalDial] = None,
    ) -> AuthenticatedOpen:
        """Publish a locally authenticated stream before granting any frame capability."""

        self._require_root_providers()
        body = self._validate(proof)
        fields = {"id", "schema_version", "kind", "pair_id", "direction", "stream_id", "enable_id",
                  "enable_generation", "local_side", "peer_der_sha256", "gate_counter", "owner_id"}
        if (
            self._authenticate_open is None or set(body) != fields
            or not _integer(body["schema_version"], 1, 1) or body["kind"] != "remote_session_opened"
            or body["local_side"] not in ("listener", "dialler")
            or body["direction"] not in ("S-to-T", "T-to-S")
            or not _integer(body["gate_counter"], 1, MAX_INTEGER)
            or (body["local_side"] == "dialler" and body["gate_counter"] == MAX_INTEGER)
            or not _integer(body["enable_generation"], 1, MAX_INTEGER)
            or not isinstance(body["stream_id"], str) or not _ENABLE_ID.fullmatch(body["stream_id"])
            or not isinstance(body["peer_der_sha256"], str) or not _SHA256.fullmatch(body["peer_der_sha256"])
            or not isinstance(body["owner_id"], str) or not body["owner_id"]
        ):
            raise ProtocolRefusal("cross_stream_owner_invalid", "the authenticated local stream proof is incomplete")
        capability = Capability("I", body["pair_id"], verb="open")
        check(capability, body)
        with self._lock(exclusive=True):
            self._maintain_as(capability)
            loaded = self._load()
            if loaded is None:
                raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
            records, _ = self._verified(loaded[0])
            state = project(records)
            self._require_admitting_pair(state, body["pair_id"], *loaded, records)
            check_data_scope(state, body, capability.pair_id)
            if not self._fixture_mode:
                pair = state.active_pairs[body["pair_id"]]
                outgoing = "S-to-T" if pair["role"] == "S" else "T-to-S"
                if (body["peer_der_sha256"] != pair["peer_sha256"]
                    or (body["direction"] == outgoing) != (body["local_side"] == "dialler")):
                    raise self._pair_material_refusal()
            if body["local_side"] == "dialler" and (not self._fixture_mode or self._dial_prerequisites is not None):
                source, attempted = self._current_dial_source(dial, records)
                if body["owner_id"] != attempted["id"] or any(
                    body[key] != source[key] for key in ("enable_id", "enable_generation", "pair_id", "direction")
                ):
                    raise ProtocolRefusal("cross_stream_owner_invalid", "dialler open has a different local attempt")
            elif dial is not None:
                raise ProtocolRefusal("cross_stream_owner_invalid", "a listener does not own a local dial attempt")
            coordinate = trust_coordinate(records)
            if (coordinate is None or coordinate[:2] != (body["enable_id"], body["enable_generation"])
                or self._authenticate_open(body, stream) is not True):
                raise ProtocolRefusal("cross_stream_owner_invalid", "the stream is not owned by the local lifecycle")
            encoded = encode_frame(body)
            stream_id = body["stream_id"]
            live = self._live_streams.get(stream_id)
            if live is not None and live.stream is stream and live.record_frame == encoded:
                return live
            if live is not None or any(row.get("stream_id") == stream_id for row in records):
                raise ProtocolRefusal("cross_stream_owner_invalid", "the stream identity already belongs to another open")
            result, _ = self._append_locked(capability, body)
            if result.status != "committed":
                raise ProtocolRefusal(result.code or "cross_publication_pending", "the authenticated open is unsettled")
            live = AuthenticatedOpen(encoded, stream, body["pair_id"], getattr(stream, "permit_local_output", None),
                                     getattr(stream, "enqueue_local_output", None))
            self._live_streams[stream_id] = live
            if dial is not None:
                self._pending_dials.remove(dial)
            return live

    def close_authenticated(self, opened: AuthenticatedOpen) -> None:
        with self._lock(exclusive=True):
            body = decode_frames(opened.record_frame)[0]
            if self._live_streams.get(body["stream_id"]) is opened:
                del self._live_streams[body["stream_id"]]
                self._pending_outputs = {dispatch for dispatch in self._pending_outputs if dispatch.opened is not opened}

    def _check_live_stream(self, capability: Capability, records, *, disabling: bool) -> None:
        opened = capability.session
        if not isinstance(opened, AuthenticatedOpen):
            raise ProtocolRefusal("cross_stream_owner_invalid", "frame dispatch requires its current authenticated stream")
        body = decode_frames(opened.record_frame)[0]
        if self._live_streams.get(body["stream_id"]) is not opened or body["pair_id"] != capability.pair_id:
            raise ProtocolRefusal("cross_stream_owner_invalid", "frame dispatch lost its original live transport")
        eligible = records
        if disabling:
            stop = next(row["seq"] for row in reversed(records) if row.get("phase") == "disable_started")
            eligible = [row for row in records if row["seq"] < stop]
        if not any(semantic(row) == body for row in eligible if row["kind"] == "remote_session_opened"):
            raise ProtocolRefusal("cross_stream_owner_invalid", "the exact authenticated open is not in the eligible prefix")
        self._lock_context.authenticating_open = opened
        try:
            if self._authenticate_open(body, opened.stream) is not True:
                raise ProtocolRefusal("cross_stream_owner_invalid", "frame dispatch lost its original live transport")
        finally:
            self._lock_context.authenticating_open = None

    def _local_output_evidence(self, capability, opened, sender, outbound, checkpoint, raw, records):
        if (
            capability.writer != "L" or capability.verb not in ("ask", "send")
            or self._authorize_local_send is None or self._authorize_local_send(capability, sender) is not True
        ):
            raise ProtocolRefusal("cross_capability_refused", "first output requires its scoped authenticated local caller")
        if not isinstance(opened, AuthenticatedOpen) or capability.pair_id != opened.pair_id:
            raise ProtocolRefusal("cross_stream_owner_invalid", "the local request does not own this stream")
        self._check_live_stream(opened.capability("reconcile"), records, disabling=False)
        proof = decode_frames(opened.record_frame)[0]
        if (
            outbound["kind"] != "remote_outbound"
            or outbound.get("schema_version") != 2
            or outbound.get("verb") != capability.verb
            or outbound.get("pair_id") != opened.pair_id
            or outbound.get("envelope", {}).get("sender_node") != sender
            or (outbound.get("enable_id"), outbound.get("enable_generation"))
            != (proof["enable_id"], proof["enable_generation"])
            or (outbound.get("direction") == proof["direction"]) != (proof["local_side"] == "dialler")
            or not callable(opened.permit_output) or not callable(opened.enqueue_output)
            or opened.permit_output(outbound.get("direction")) is not True
        ):
            raise ProtocolRefusal("cross_stream_owner_invalid", "the exact local request has no eligible reverse permit")
        if any(row["kind"] == "remote_sender_replay" and row.get("envelope_id") == outbound["envelope_id"]
               and (row.get("enable_id"), row.get("enable_generation"))
               == (outbound["enable_id"], outbound["enable_generation"]) for row in records):
            raise ProtocolRefusal("cross_delivery_request_invalid", "a retired sender key grants no stream output")
        state = project(records)
        self._require_admitting_pair(state, opened.pair_id, checkpoint, raw, records)
        check_data_scope(state, outbound, capability.pair_id)
        if "enable_complete" not in state.phases or opened.pair_id not in state.active_pairs:
            raise ProtocolRefusal("cross_pair_scope_refused", "output requires the active pair")
        return proof

    def prepare_local_output(self, capability, opened, record_id, sender):
        """Authorize an exact attempt from the listener's local command entry."""
        if capability.writer != "L":
            raise ProtocolRefusal("cross_capability_refused", "a received frame cannot originate local output")
        self._require_root_providers()
        with self._lock(exclusive=True):
            if self._maintenance_due():
                raise ProtocolRefusal("cross_publication_pending", "the local output prefix is unsettled")
            loaded = self._load()
            if loaded is None:
                raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
            records, _ = self._verified(loaded[0])
            outbound = next((row for row in records if row["id"] == record_id and row["kind"] == "remote_outbound"
                             and row.get("pair_id") == capability.pair_id), None)
            if outbound is None:
                raise ProtocolRefusal("cross_delivery_request_invalid", "the exact local intent is unavailable")
            proof = self._local_output_evidence(capability, opened, sender, outbound, *loaded, records)
            body = {key: outbound[key] for key in ("enable_id", "enable_generation", "pair_id", "direction",
                                                   "delivery_seq", "envelope_digest", "envelope_id")}
            identifier = _sha256(encode_frame({"intent": record_id, "open": proof["id"]}))
            body.update(id="cross-first-output-" + identifier, kind="remote_attempt", schema_version=1,
                        outbound_id=record_id, open_id=proof["id"], wire_seq=body["delivery_seq"])
            check(capability, body)
            result = self._append_with_reserve_file(capability, body)
            if result.status != "committed":
                raise ProtocolRefusal(result.code or "cross_local_output_pending", "the first local attempt cannot be repeated")
            dispatch = LocalOutput(opened, capability, sender, encode_frame(semantic(outbound)), encode_frame(body))
            self._pending_outputs.add(dispatch)
            return dispatch

    def deposit_local_output(self, dispatch: LocalOutput) -> bool:
        """Make a bounded local queue deposit under the stop fence."""
        with self._lock(exclusive=True):
            if dispatch not in self._pending_outputs:
                return False
            self._pending_outputs.remove(dispatch)
            if self._maintenance_due():
                return False
            loaded = self._load()
            if loaded is None:
                return False
            records, _ = self._verified(loaded[0])
            outbound = decode_frames(dispatch.outbound_frame)[0]
            attempt = decode_frames(dispatch.attempt_frame)[0]
            if not all(any(semantic(row) == value for row in records) for value in (outbound, attempt)):
                return False
            try:
                self._local_output_evidence(dispatch.capability, dispatch.opened, dispatch.sender, outbound, *loaded, records)
            except ProtocolRefusal:
                return False
            frame = {"t": "envelope", "envelope": outbound["envelope"], "envelope_digest": outbound["envelope_digest"]}
            body = encode_frame(frame)[:-1]
            return dispatch.opened.enqueue_output(len(body).to_bytes(4, "big") + body) is True

    def allocate_outbound(self, capability: Capability, key: str, request: Mapping[str, object], timestamp: str):
        """Allocate the next immutable outbound intent while holding the cross lock."""
        self._require_root_providers()
        if (capability.writer != "L" or capability.verb != request.get("verb")
            or not capability.pair_id or not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", key)):
            raise ProtocolRefusal("cross_capability_refused", "allocation requires the scoped local send capability")
        commitment = request_commitment(dict(request))
        fields = dict(request["envelope_fields"])
        pair, direction = fields["pair_id"], fields["direction"]
        if pair != capability.pair_id:
            raise ProtocolRefusal("cross_pair_scope_refused", "the request does not belong to this pair")
        if not self.directory.is_dir():
            raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
        with self._lock(exclusive=True):
            if self._authorize_local_send is None or self._authorize_local_send(capability, fields["sender_node"]) is not True:
                raise ProtocolRefusal("cross_capability_refused", "lookup requires the authenticated local sender")
            if self._maintenance_due():
                raise ProtocolRefusal("cross_publication_pending", "local send lookup requires a settled root")
            loaded = self._load()
            if loaded is None:
                raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
            records, _ = self._verified(loaded[0])
            state = project(records)
            self._require_admitting_pair(state, pair, *loaded, records)
            check_data_scope(state, {"kind": "remote_outbound", "pair_id": pair}, pair)
            if "enable_complete" not in state.phases or pair not in state.active_pairs:
                raise ProtocolRefusal("cross_pair_scope_refused", "the allocation requires an active pair")
            trust = trust_coordinate(records)
            if trust is None:
                raise ProtocolRefusal("cross_legacy_admission_fenced", "delivery numbers require a verified enable coordinate")
            enable, epoch = trust[:2]
            scoped = [row for row in records if row["kind"] in {"remote_outbound", "remote_sender_replay", "cross_sender_floor"}
                      and (row.get("enable_id"), row.get("enable_generation"), row.get("pair_id"), row.get("direction"))
                      == (enable, epoch, pair, direction)]
            replay, suffix = self._verify_sender_coverage(records, scoped, enable, epoch, pair, direction)
            retired = replay.get((fields["sender_node"], key))
            if retired is not None:
                if retired["request_sha256"] != commitment:
                    raise ProtocolRefusal("cross_record_id_conflict", "this key names another outbound request")
                status = "outcome_unknown" if retired["result"] is None else "existing"
                return AppendResult(status, f"remote-outbound-{enable}-{retired['envelope_id']}",
                                    retired["seq"], delivery_seq=retired["delivery_seq"], outcome=retired["result"])
            for row in scoped:
                if row["kind"] == "remote_outbound" and row["idempotency_key"] == key and (
                    row["envelope"]["sender_node"] == fields["sender_node"]
                ):
                    if row["request_sha256"] != commitment:
                        raise ProtocolRefusal("cross_record_id_conflict", "this key names another outbound request")
                    return AppendResult("existing", row["id"], row["seq"], delivery_seq=row["delivery_seq"])
            if any(row["result"] is None for row in replay.values()):
                raise ProtocolRefusal("cross_delivery_history_lost", "new allocation needs a proved delivery suffix")
            sequence = next_sequence(self._sender_floor(scoped), suffix)
            wire_id = envelope_id(pair, direction, sequence)
            envelope = dict(fields, seq=sequence, envelope_id=wire_id, sender_timestamp=timestamp)
            body = {
                "kind": "remote_outbound", "schema_version": 2, "id": f"remote-outbound-{enable}-{wire_id}",
                "enable_id": enable, "enable_generation": epoch, "pair_id": pair, "direction": direction,
                "delivery_seq": sequence, "envelope_id": wire_id, "envelope": envelope,
                "envelope_digest": digest(enc(b"floati-remote-envelope-v5"), enc(encode_frame(envelope)[:-1])),
                "idempotency_key": key, "request_sha256": commitment, "verb": request["verb"],
                "replay_retained": self._retain_sender_source is not None and self._retain_sender_source(dict(request)) is True,
            }
            body["replay_price"] = sender_replay_price(body)
            validate_outbound(body)
            check(capability, body)
            return self._append_with_reserve_file(capability, body)

    def provision_reserve_file(self, size: int = BALLAST_BYTES) -> None:
        """Enable's physical reserve: synced bytes close-out may release on disk full."""

        if not self._fixture_mode:
            raise ProtocolRefusal("cross_capability_refused", "ballast requires its attended inventory intent")
        with self._lock(exclusive=True):
            if self.storage.read(BALLAST) is None:
                self.storage.create(BALLAST, b"\0" * size)
                self.storage.sync(BALLAST)
                self.storage.create(BALLAST_INVENTORY, self._ballast_identity())
                self.storage.sync(BALLAST_INVENTORY)
                self.storage.sync_directory()

    def _ballast_identity(self, scope: Optional[str] = None) -> bytes:
        identity = (self.directory / BALLAST).lstat()
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1 or identity.st_uid != os.getuid():
            raise ProtocolRefusal("cross_closeout_unproved", "the close-out ballast ownership is uncertain")
        body = {
            "format": "floati-cross-ballast-inventory-v1",
            "device": identity.st_dev, "inode": identity.st_ino, "ctime_ns": identity.st_ctime_ns,
            "size": identity.st_size, "uid": identity.st_uid,
        }
        if scope is not None:
            body.update(format="floati-cross-ballast-inventory-v2", scope_sha256=scope)
        return encode_frame(body)

    @staticmethod
    def _ballast_scope(attended, proof) -> str:
        return _sha256(encode_frame({"start": dict(attended), "proof_sha256": proof}))

    @staticmethod
    def _ballast_bytes(scope, size):
        header = _BALLAST_HEADER + bytes.fromhex(scope)
        if not _integer(size, len(header), BALLAST_BYTES):
            raise ProtocolRefusal("cross_closeout_unproved", "the scoped ballast size is invalid")
        return header + b"\0" * (size - len(header))

    def _ballast_file(self, name):
        try:
            identity = (self.directory / name).lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1 or identity.st_uid != os.getuid():
            raise self._enable_inventory_refusal()
        return self.storage.read(name)

    def _ballast_intent(self, attended, proof):
        loaded = self._load()
        if loaded is None or self._seal() != _sha256(loaded[1]):
            return ()
        records, _ = self._verified(loaded[0])
        start = next((semantic(row) for row in reversed(records) if row.get("phase") == "enable_started"), None)
        if start != attended:
            return ()
        return tuple(row["phase"] for row in records if row.get("kind") == "cross_inventory_item"
                     and row.get("item") == "ballast" and row.get("token") == attended["token"]
                     and row.get("enable_id") == attended["enable_id"]
                     and row.get("enable_generation") == attended["enable_generation"]
                     and row.get("proof_sha256") == proof)

    def inspect_enable_ballast(self, attended: Mapping[str, object], *, size: int = BALLAST_BYTES) -> tuple:
        """Measure the scoped physical ballast for the inventory read callback."""
        if self._enable_inventory is None:
            raise self._enable_inventory_refusal()
        proof = self._enable_inventory.expected("ballast", attended)
        scope = self._ballast_scope(attended, proof)
        wanted = self._ballast_bytes(scope, size)
        current = self._ballast_file(BALLAST)
        inventory = self._ballast_file(BALLAST_INVENTORY)
        if current is None and inventory is None:
            return "safe_absent", None
        intent = self._ballast_intent(attended, proof)
        if not intent or current is None or not wanted.startswith(current):
            return "disputed", None
        identity = self._ballast_identity(scope)
        if inventory is not None and (current != wanted or not identity.startswith(inventory)):
            return "disputed", None
        if current == wanted and inventory == identity and (
            "created" in intent or self._synced_enable_ballast == (scope, identity)
        ):
            return "owned", proof
        return "safe_absent", None

    def provision_enable_ballast(self, attended: Mapping[str, object], *, size: int = BALLAST_BYTES) -> str:
        """Provision ballast from the lock-held create callback for the sealed intent."""
        lease = self._enable_ballast_scope
        if (lease is None or lease[:2] != (threading.get_ident(), attended)
            or "intent" not in self._ballast_intent(attended, lease[2])):
            raise self._enable_inventory_refusal()
        proof = lease[2]
        scope = self._ballast_scope(attended, proof)
        if self.inspect_enable_ballast(attended, size=size)[0] == "disputed":
            raise self._enable_inventory_refusal()
        self._fill_ballast_file(BALLAST, self._ballast_bytes(scope, size))
        # A surviving sidecar must never be the only durable name.
        self.storage.sync_directory()
        identity = self._ballast_identity(scope)
        self._fill_ballast_file(BALLAST_INVENTORY, identity)
        self.storage.sync_directory()
        self._synced_enable_ballast = scope, identity
        return proof

    def _fill_ballast_file(self, name, wanted):
        current = self._ballast_file(name)
        if current is None:
            self.storage.create(name, wanted)
        elif not wanted.startswith(current):
            raise self._enable_inventory_refusal()
        elif current != wanted:
            self.storage.append(name, wanted[len(current):])
        self.storage.sync(name)

    def append(
        self, capability: Union[str, Capability], record: Mapping[str, object], *, output_token: Optional[bytes] = None
    ) -> AppendResult:
        capability = as_capability(capability)
        self._require_root_providers()
        candidate = self._validate(record)
        check(capability, candidate)
        if not self._fixture_mode and candidate["kind"] == "remote_endpoint_registered":
            raise endpoint_refusal()
        if not self._fixture_mode and capability.writer == "M" and candidate.get("cause") in {
            "anchor_absent_reconstructed", "crash"
        }:
            raise ProtocolRefusal("cross_capability_refused", "maintenance needs its proof-bound entry point")
        if not self._fixture_mode and capability.writer == "L" and candidate["kind"] in _SENDER_KINDS:
            raise ProtocolRefusal("cross_capability_refused", "the local sender needs its phased entry point")
        if candidate["kind"] in {"cross_pair_active", "cross_pair_seen"} and (
            candidate.get("schema_version") != 0 or not self._fixture_mode
            or (capability.writer == "A" and self._prove_pair_material is not None)
        ):
            raise self._pair_material_refusal()
        if (self._authenticate_open is not None or not self._fixture_mode) and candidate["kind"] == "remote_session_opened":
            raise ProtocolRefusal("cross_stream_owner_invalid", "only the local lifecycle publishes an authenticated open")
        if candidate["kind"] == "remote_outbound" and candidate.get("schema_version") == 2:
            raise ProtocolRefusal("cross_capability_refused", "the local allocator owns the immutable sender row")
        if candidate["kind"] == "cross_inventory_item" and self._enable_approval is not None:
            raise self._enable_inventory_refusal()
        self._origin = capability.writer
        if not self.directory.is_dir():
            raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
        with self._lock(exclusive=True):
            loaded = self._load()
            if not self._fixture_mode and candidate["kind"] in DATA_KINDS and self._observed_store() is None:
                raise self._pair_pending()
            if loaded is not None and capability.writer == "L" and candidate["kind"] in CONTROL_KINDS:
                records, _ = self._verified(loaded[0])
                step = retires(project(records), candidate)
                if step is not None:
                    return self._execute_closeout_step(capability, candidate)
            if loaded is not None and candidate["kind"] in {"remote_attempt", "remote_transport_attempt"}:
                records, _ = self._verified(loaded[0])
                source_id, envelope_id = candidate.get("outbound_id"), candidate.get("envelope_id")
                matching = [
                    row for row in records if row["kind"] in {"remote_outbound", "remote_sender_replay"}
                    and row.get("pair_id") == candidate.get("pair_id")
                    and (row["id"] == source_id or row.get("envelope_id") == envelope_id)
                ]
                if any(row["kind"] == "remote_sender_replay" for row in matching):
                    raise ProtocolRefusal("cross_delivery_request_invalid", "a retired sender key grants no stream output")
                if any(row.get("schema_version") == 2 for row in matching) and not (
                    capability.writer == "I" and capability.verb == "reconcile"
                    and any(row["kind"] == "remote_attempt" and semantic(row) == candidate for row in records)
                ):
                    raise ProtocolRefusal("cross_stream_owner_invalid",
                                          "an immutable sender attempt requires the verified output entry")
            self._maintain_as(capability)
            if candidate["kind"] == "cross_optin" and candidate.get("phase") == "enable_complete" and (
                candidate.get("schema_version") in (1, 2)
            ):
                self._resume_enable_inventory(capability, candidate)
            if candidate["kind"] == "remote_hold_rejected" and candidate.get("schema_version") == 2:
                if self._hold_source is None:
                    raise ProtocolRefusal("cross_hold_evidence_invalid", "the settled hold ledger is unavailable")
                with self._hold_source(candidate) as decision:
                    return self._append_with_reserve_file(capability, candidate, hold_decision=decision)
            return self._append_with_reserve_file(capability, candidate, output_token=output_token)

    def _append_with_reserve_file(
        self, capability: Capability, candidate: Dict[str, object], *, output_token: Optional[bytes] = None,
        hold_decision=None, creating: bool = False, reconstructing: bool = False,
    ) -> AppendResult:
        observed = self.storage.sync_position()
        result, _ = self._append_locked(capability, candidate, output_token=output_token, hold_decision=hold_decision,
                                        creating=creating, reconstructing=reconstructing)
        self.census.append((capability.writer, capability.writer, str(candidate["kind"])))
        return replace(result, barrier_durability=self.storage.sync_grade(observed))

    def _bind_narrowing(self, capability, candidate, checkpoint, raw, records, state, authorized_intent, reconstructing):
        current = self._observed_store()
        if candidate.get("cause") == "anchor_absent_reconstructed" and not self._fixture_mode:
            if not reconstructing or current is not None:
                raise self._anchor_refusal()
        elif isinstance(current, Anchor):
            if current != self._anchor_coordinate(checkpoint, raw, records) or (
                not self._fixture_mode and current.trust[:2] != (candidate.get("enable_id"), candidate.get("enable_generation"))
            ):
                raise self._anchor_refusal()
            if candidate.get("cause") == "key_absent_reconstructed" and (
                capability.writer != "M" or self._confirm_pair_absent is None
                or self._confirm_pair_absent(candidate["pair_id"]) is not True
            ):
                raise ProtocolRefusal("cross_pair_absence_unproved", "the exact pair item is not confirmed absent")
            self._keep_store_predecessor(raw)
            intent = Narrowing(current, self._position_key(checkpoint), _sha256(raw), encode_frame(candidate))
            try:
                self._replace_anchor(current, intent)
            except ProtocolRefusal as exc:
                return exc.code
        elif current is not None and (current != authorized_intent or current.body() != candidate):
            raise self._anchor_refusal()
        elif current is None and "disable_started" not in state.phases:
            raise self._anchor_refusal()

    def _execute_closeout_step(self, capability: Capability, candidate: Dict[str, object]) -> AppendResult:
        """Settle publication and retry the exact obligation after one ballast release."""
        released = False
        while True:
            try:
                self._maintain_as(capability)
                checkpoint, _ = self._load()
                records, _ = self._verified(checkpoint)
                state = project(records)
                step = retires(state, candidate)
                limiting = limiting_control(state, self.budget, headroom=1) if step else None
                if limiting is not None:
                    try:
                        self._compact_locked("M", "closeout-replenishment")
                    except ProtocolRefusal as exc:
                        if exc.code not in {"cross_compaction_no_gain", "cross_reserve_protected", "cross_storage_cap"}:
                            raise
                        return AppendResult("deferred", str(candidate["id"]), code="cross_closeout_record_pending",
                                            operation=step, limiting_resource=limiting)
                result = self._append_with_reserve_file(capability, candidate)
                if result.code != "disk_full":
                    return result
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    raise
                result = AppendResult("failed", str(candidate["id"]), code="disk_full")
            if released:
                return result
            try:
                if not self._closeout_ballast_owned():
                    return AppendResult("indeterminate", str(candidate["id"]), code="cross_closeout_unproved")
                self.storage.unlink(BALLAST)
                self.storage.sync_directory()
            except FileNotFoundError:
                return result
            except (OSError, ProtocolRefusal):
                return AppendResult("indeterminate", str(candidate["id"]), code="cross_closeout_unproved")
            released = True

    def _closeout_ballast_owned(self) -> bool:
        stored = self._ballast_file(BALLAST_INVENTORY)
        if stored == self._ballast_identity():
            return True
        checkpoint, _ = self._load()
        records, _ = self._verified(checkpoint)
        start = next((semantic(row) for row in reversed(records) if row.get("phase") == "enable_started"), None)
        if start is None or start.get("schema_version") not in (1, 2):
            return False
        intent = next((row for row in reversed(records) if row.get("item") == "ballast"
                       and row.get("phase") == "intent" and row.get("token") == start["token"]), None)
        if intent is None:
            return False
        scope = self._ballast_scope(start, intent["proof_sha256"])
        payload = self._ballast_file(BALLAST)
        return stored == self._ballast_identity(scope) and payload == self._ballast_bytes(scope, len(payload))

    def _append_locked(
        self, capability: Capability, candidate: Dict[str, object], *, output_token: Optional[bytes] = None,
        authorized_intent: Optional[Narrowing] = None,
        hold_decision=None, creating: bool = False, reconstructing: bool = False,
    ) -> Tuple[AppendResult, Optional[str]]:
        """Append under a held lock whose maintenance is already done."""

        record_id = caller_id = str(candidate["id"])
        loaded = self._load()
        if loaded is None:
            raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
        checkpoint, raw = loaded
        records, _ = self._verified(checkpoint)
        state = project(records)
        kind = str(candidate["kind"])
        if kind == "cross_pair_revoked":
            activation = state.pair_history.get(f"cross-pair-active-{candidate.get('pair_id')}")
            if activation is not None:
                validate_pair_revocation(activation, candidate)
        if ((self._authenticate_open is not None or not self._fixture_mode) and capability.writer == "I"
            and capability.verb not in {"open", "endpoint_start"}):
            self._check_live_stream(capability, records, disabling="disable_started" in state.phases)
        legacy = checkpoint["format"] == LEGACY_CHECKPOINT_FORMAT or (
            checkpoint["format"] == QUOTA_CHECKPOINT_FORMAT
            and (self._anchor_store is not None or self._quota_store is not None)
        )
        if legacy and (
            kind in DATA_KINDS or (
                kind != "cross_optin" and retires(state, candidate) is None
            ) or candidate.get("phase") in {"enable_started", "enable_complete"}
        ):
            raise ProtocolRefusal("cross_legacy_admission_fenced", "legacy cross history requires teardown before fresh enable")
        if capability.writer == "A" and (kind == "cross_ceremony_closed" or (
            kind in {"cross_pair_active", "cross_pair_seen"} and candidate.get("schema_version") == 0
        )):
            opening = next((row for row in reversed(records) if row["kind"] == "cross_ceremony_opened"
                            and row.get("ceremony_id") == candidate.get("ceremony_id")), None)
            if (opening is None or candidate.get("ceremony_token") != opening.get("ceremony_token")
                or (kind == "cross_pair_active" and opening["ceremony_id"] not in state.open_ceremonies)):
                raise ProtocolRefusal("cross_ceremony_token_invalid", "the attended action requires its recorded opening")
        if kind == "cross_optin" and candidate.get("phase") in {"enable_started", "enable_complete"}:
            if self._enable_approval is not None and candidate.get("schema_version") not in (1, 2):
                raise ProtocolRefusal("cross_enable_approval_invalid", "a bare enable body grants no attended authority")
            if candidate.get("schema_version") in (1, 2):
                if self._enable_approval is None:
                    raise ProtocolRefusal("cross_enable_approval_invalid", "the local approval source is unavailable")
                if candidate["phase"] == "enable_started":
                    if not creating or self._anchor_store is None:
                        raise ProtocolRefusal("cross_enable_authority_missing", "the original attended transaction must publish the start")
                    expected = approved_start(candidate.get("enable_id"), candidate.get("enable_generation"),
                                              candidate.get("token"), candidate.get("plan"),
                                              schema_version=candidate["schema_version"])
                    source = candidate
                else:
                    source = next((row for row in reversed(records) if row.get("phase") == "enable_started"), None)
                    if source is None:
                        raise ProtocolRefusal("cross_enable_approval_invalid", "enable completion needs its published start")
                    expected = approved_completion(source)
                if expected != candidate or self._enable_approval(candidate["phase"], semantic(source)) is not True:
                    raise ProtocolRefusal("cross_enable_approval_invalid", "the attended enable evidence changed")
                if candidate["phase"] == "enable_complete":
                    current = self._observed_store()
                    current_anchor = self._anchor_coordinate(checkpoint, raw, records)
                    if (not isinstance(current, Anchor) or current != current_anchor or "disable_started" in state.phases
                        or current_anchor.trust[2] not in (1, 2)):
                        raise ProtocolRefusal("cross_enable_authority_missing", "the sealed start has no current continuation")
        if kind == "cross_ceremony_opened":
            if self._prove_pair_material is not None or not self._fixture_mode:
                self._validate_live_opening(candidate, checkpoint, raw, records)
            else:
                self._check_pairing_material(records)
            prior_open = next((row for row in records if row["id"] == record_id), None)
            if prior_open is not None and str(candidate.get("ceremony_id")) not in state.open_ceremonies:
                raise ProtocolRefusal("cross_transition_invalid", "this ceremony has already closed")
            if prior_open is None:
                try:
                    day = self._utc_day()
                    if date.fromisoformat(day).isoformat() != day:
                        raise ValueError("noncanonical date")
                except (OSError, TypeError, ValueError) as exc:
                    raise ProtocolRefusal("cross_ceremony_quota", "the UTC date is unavailable") from exc
                quota = checkpoint.get("quota")
                if quota and quota["mode"] == "legacy_blocked_through" and day <= quota["day"]:
                    raise ProtocolRefusal("cross_ceremony_quota", "the legacy cutoff includes this UTC date")
                if state.quota_day is not None and (
                    day < state.quota_day or (day == state.quota_day and state.quota_count >= 32)
                ):
                    raise ProtocolRefusal("cross_ceremony_quota", "the UTC opening budget is unavailable")
            else:
                day = prior_open["utc_day"]
            candidate = dict(candidate, utc_day=day, schema_version=1)
        inbound_id = candidate.get("inbound_id")
        key = None
        delivery = None
        if kind in COMPLETION_KINDS and isinstance(inbound_id, str) and inbound_id:
            key = delivery_key(inbound_id)
            delivery = state.deliveries.get(key)
            if delivery is not None and kind in delivery.kinds:
                pair = delivery.pair_id
                if not isinstance(pair, str) or not pair or candidate.get("pair_id", pair) != pair:
                    raise ProtocolRefusal(
                        "cross_pair_scope_refused", "a completion must belong to its inbound delivery's pair"
                    )
                if kind == "remote_presented" and candidate.get("schema_version") == 2:
                    record_id = completion_id(str(candidate.get("token_sha256")), kind)
                else:
                    record_id = completion_id(key, kind)
                if candidate.get("schema_version") == 2 and candidate["id"] != record_id:
                    raise ProtocolRefusal("cross_priced_record_invalid", "the completion has a different stable identity")
                candidate = dict(candidate, id=record_id, pair_id=pair)
                check(capability, candidate)
            else:
                delivery = None
        check_data_scope(state, candidate, capability.pair_id)
        if kind in DATA_KINDS and kind != "remote_hold_rejected":
            if "enable_complete" not in state.phases:
                raise ProtocolRefusal("cross_disabled", "cross-machine is disabled on this root")
            pair = candidate.get("pair_id")
            self._require_admitting_pair(state, pair, checkpoint, raw, records)
            if kind != "remote_endpoint_registered" and pair not in state.active_pairs:
                raise ProtocolRefusal("cross_pair_scope_refused", "the record requires an active pair")
        if kind == "remote_inbound" and isinstance(candidate.get("delivery_seq"), int):
            scoped = self._receiver_scope(records, candidate.get("pair_id"), candidate.get("direction"))
            if candidate["delivery_seq"] <= self._receiver_frontier(scoped):
                raise ProtocolRefusal("remote_below_frontier", "the receiver has retired this delivery coordinate")
        if kind == "remote_receipt_confirmed" and candidate.get("schema_version") == 2:
            inbound = next((row for row in records if row["id"] == inbound_id), None)
            fields = ("enable_id", "enable_generation", "pair_id", "direction", "delivery_seq",
                      "envelope_digest", "receipt_id", "receipt_digest")
            if inbound is None or any(inbound.get(field) != candidate.get(field) for field in fields):
                raise ProtocolRefusal("cross_completion_delivery_invalid", "confirmation requires the exact receipt")
        if kind == "remote_hold_rejected":
            inbound = next((row for row in records if row["id"] == inbound_id), None)
            fields = ("pair_id", "local_message_id", "recipient", "hold_id", "hold_digest")
            if candidate.get("schema_version") == 2:
                fields += ("enable_id", "enable_generation", "direction", "delivery_seq", "envelope_digest")
            original = dict(candidate, id=caller_id)
            if candidate.get("schema_version") == 2:
                proved = (
                    isinstance(hold_decision, Mapping)
                    and hold_decision.get("phase") == "rejected"
                    and hold_decision.get("id") == candidate.get("decision_id")
                    and hold_decision.get("node_id") == candidate.get("recipient")
                    and hold_decision.get("message_id") == candidate.get("local_message_id")
                    and hold_decision.get("hold_id") == candidate.get("hold_id")
                    and hold_decision.get("envelope_digest") == candidate.get("hold_digest")
                    and _sha256(encode_frame(hold_decision)) == candidate.get("decision_sha256")
                )
            else:
                proved = self._hold_rejection is not None and self._hold_rejection(original) is True
            if (
                inbound is None or delivery is None or not delivery.reserved
                or any(not candidate.get(field) or inbound.get(field) != candidate[field] for field in fields)
                or not candidate.get("decision_id")
                or not proved
                or any(row["kind"] == "remote_local_ack" and row.get("inbound_id") == inbound_id for row in records)
            ):
                raise ProtocolRefusal("cross_hold_evidence_invalid", "the exact settled hold rejection is required")
        elif kind in {"remote_presented", "remote_local_ack"} and any(
            row["kind"] == "remote_hold_rejected" and row.get("inbound_id") == inbound_id for row in records
        ):
            raise ProtocolRefusal("cross_completion_delivery_invalid", "the accepted item has a terminal rejection")
        if kind == "remote_reconcile_frontier":
            direction, frontier, digest = (candidate.get(k) for k in ("direction", "frontier", "checkpoint_digest"))
            if (direction not in {"S-to-T", "T-to-S"} or not _integer(frontier, 0, MAX_INTEGER)
                or (digest is not None if frontier == 0 else not isinstance(digest, str) or not _SHA256.fullmatch(digest))):
                raise ProtocolRefusal("cross_reconcile_evidence_invalid", "the witnessed frontier is malformed")
            prior = next((row for row in reversed(records) if row["kind"] == kind
                          and row.get("pair_id") == candidate["pair_id"] and row.get("direction") == direction), None)
            if prior is not None:
                if frontier < prior["frontier"] or (frontier == prior["frontier"] and digest != prior["checkpoint_digest"]):
                    raise ProtocolRefusal("cross_delivery_history_lost", "the witnessed frontier regressed or changed")
                if frontier == prior["frontier"]:
                    return AppendResult("existing", str(prior["id"]), int(prior["seq"])), None
        replacement = False
        if delivery is not None and kind in {"remote_presented", "remote_local_ack"}:
            row = next(row for row in records if row["id"] == inbound_id)
            fields = ("pair_id", "envelope_digest", "recipient", "local_message_id")
            if candidate.get("schema_version") == 2:
                fields += ("enable_id", "enable_generation", "direction", "delivery_seq")
            else:
                fields += ("envelope_id",)
            if any(not candidate.get(field) or candidate[field] != row.get(field) for field in fields):
                raise ProtocolRefusal("cross_completion_delivery_invalid", "the worker release differs from the accepted delivery")
            session = candidate.get("worker_session_id")
            if not isinstance(session, str) or not session:
                raise ProtocolRefusal("cross_output_token_invalid", "a worker release requires its authenticated session")
            releases = [row for row in records if row["kind"] == "remote_presented"
                        and row.get("inbound_id") == inbound_id]
            current = releases[-1] if releases else None
            if kind == "remote_presented":
                if "remote_local_ack" not in delivery.remaining:
                    raise ProtocolRefusal("cross_output_token_invalid", "the accepted item is already terminal")
                verifier = candidate.get("token_sha256")
                if not isinstance(verifier, str) or _SHA256.fullmatch(verifier) is None:
                    raise ProtocolRefusal("cross_output_token_invalid", "a worker release requires its token verifier")
                if candidate.get("schema_version") == 2:
                    replacement = current is not None and current["id"] != record_id
                    previous = current["id"] if replacement else None
                else:
                    previous = candidate.get("supersedes_id")
                    if previous is not None:
                        record_id = f"{record_id}-{verifier}"
                        candidate = dict(candidate, id=record_id)
                        replacement = True
                if current is not None and current["id"] != record_id:
                    if not replacement or previous != current["id"] or "remote_local_ack" not in delivery.remaining:
                        raise ProtocolRefusal("cross_output_token_invalid", "the worker release is no longer current")
                    if any(release.get("token_sha256") == verifier for release in releases):
                        raise ProtocolRefusal("cross_output_token_invalid", "a replacement requires a fresh output token")
                elif replacement and current is None:
                    raise ProtocolRefusal("cross_output_token_invalid", "the prior worker release is missing")
            else:
                verifier = current.get("token_sha256") if current else None
                if (
                    current is None or current["id"] != candidate.get("release_id")
                    or current.get("worker_session_id") != session or not isinstance(verifier, str)
                    or not isinstance(output_token, bytes) or len(output_token) != 32
                    or not hmac.compare_digest(verifier, _sha256(output_token))
                ):
                    raise ProtocolRefusal("cross_output_token_invalid", "ack requires the current worker output token")
        check_committed(capability, candidate, records)
        if ("disable_started" in state.phases and kind != "remote_hold_rejected"
            and kind in COMPLETION_KINDS | {"remote_peer_receipt"}):
            fence = next(row for row in reversed(records) if row.get("phase") == "disable_started")
            before = [row for row in records if row["seq"] < fence["seq"]]
            if kind == "remote_peer_receipt":
                fields = ("pair_id", "direction", "envelope_id", "envelope_digest")
                number = candidate.get("wire_seq")
                matched = set()
                for row in before:
                    source_kind = row["kind"]
                    if source_kind not in {"remote_outbound", "remote_attempt"}:
                        continue
                    number_field = "delivery_seq" if source_kind == "remote_outbound" and row.get("schema_version") == 2 else "wire_seq"
                    if (type(row.get(number_field)) is int and row[number_field] == number
                        and all(candidate.get(field) is not None and row.get(field) == candidate[field] for field in fields)):
                        matched.add(source_kind)
                if not _integer(number, 1, MAX_INTEGER) or matched != {"remote_outbound", "remote_attempt"}:
                    raise ProtocolRefusal("cross_disable_in_progress",
                                          "completion requires its pre-stop outbound and attempt")
            else:
                inbound = next((row for row in before if row["id"] == inbound_id), None)
                fields = ("pair_id",)
                if candidate.get("schema_version") == 2:
                    fields += ("enable_id", "enable_generation", "direction", "delivery_seq", "envelope_digest")
                else:
                    fields += ("envelope_id",)
                if kind == "remote_receipt_confirmed":
                    fields += ("receipt_id", "receipt_digest")
                    if candidate.get("schema_version") != 2:
                        fields += ("direction", "wire_seq")
                if inbound is None or any(not candidate.get(field) or inbound.get(field) != candidate[field]
                                          for field in fields):
                    raise ProtocolRefusal("cross_completion_delivery_invalid",
                                          "completion requires the exact pre-stop delivery")
        for row in records:
            if row.get("id") == record_id:
                stored = semantic(row)
                stored.pop("completion_reserved", None)
                if stored != candidate:
                    raise ProtocolRefusal("cross_record_id_conflict", "this id names another record")
                return AppendResult("existing", record_id, int(row["seq"])), None
        if kind in COMPLETION_KINDS and delivery is None:
            raise ProtocolRefusal(
                "cross_completion_delivery_invalid", "a completion requires a committed inbound receipt for that kind"
            )
        if inbound_result(candidate) is not None:
            candidate = dict(candidate, completion_reserved=True)
        seq = int(checkpoint["through_seq"]) + 1
        strict = self._enable_approval is not None
        if strict and (
            kind in COMPLETION_KINDS or kind == "cross_ceremony_closed" or self._narrowing(candidate)
            or (kind == "cross_teardown_receipt" and candidate.get("outcome") == "complete")
        ) and not self._priced(candidate):
            raise ProtocolRefusal("cross_priced_record_invalid", "the operation requires a current closed record")
        anchored_narrowing = self._anchor_store is not None and self._narrowing(candidate)
        prior = trust_coordinate(records) if anchored_narrowing else None
        if (prior is not None and prior[2] == MAX_INTEGER and self._priced(candidate)
            and retires(state, candidate) is not None):
            # Validate the immutable denial body even though no new trust count can be allocated.
            priced_frame(candidate, seq, checkpoint["head_sha256"], trust=(prior[3], prior[2]))
            if self._seal() != _sha256(raw) or (not self._fixture_mode
                and prior[:2] != (candidate.get("enable_id"), candidate.get("enable_generation"))):
                raise self._anchor_refusal()
            failure = self._bind_narrowing(capability, candidate, checkpoint, raw, records, state,
                                           authorized_intent, reconstructing)
            if failure is not None:
                return AppendResult("indeterminate", record_id, code=failure, journal_publication="absent"), None
            return AppendResult("deferred", record_id, code="cross_trust_counter_exhausted",
                                journal_publication="absent", independent_denial="confirmed"), None
        if kind in {"cross_optin", "cross_pair_active", "cross_pair_revoked"}:
            coordinate = trust_coordinate([*records, candidate])
            if coordinate is not None and (kind == "cross_pair_active"
                or (kind == "cross_optin" and candidate.get("phase") in {"enable_started", "enable_complete"})):
                check_trust_reserve(state, candidate, coordinate[2])
        line = self._candidate_frame(checkpoint, records, candidate)
        entry = decode_frames(line)[0] if self._priced(candidate) else dict(candidate, seq=seq, prev=checkpoint["head_sha256"])
        if delivery is not None and len(line) + len(str(MAX_SEQUENCE)) - len(str(seq)) > COMPLETION_PRICE:
            raise ProtocolRefusal("cross_record_over_price", f"{kind} exceeds its {COMPLETION_PRICE}-byte price")
        prior_start = next((row for row in reversed(records) if row.get("phase") == "enable_started"), None)
        rollover = (
            capability.verb == "enable" and candidate.get("phase") == "enable_started"
            and "disable_complete" in state.phases and prior_start is not None
            and prior_start["enable_id"] != candidate.get("enable_id")
            and _integer(candidate.get("enable_generation"), prior_start.get("enable_generation", 0) + 1, MAX_INTEGER)
            and str(checkpoint["journal_id"]).rsplit("-g", 1)[0] == f"cross-{candidate.get('enable_id')}"
        )
        admitted = admit(state, entry, len(line), self.budget, self.data_budget,
                         delivery=delivery, replacement=replacement, rollover=rollover)
        if admitted == "completion_deferred":
            limiting = limiting_control(state, self.budget, headroom=3) or "data_capacity"
            return AppendResult("deferred", record_id, code="cross_completion_deferred",
                                delivery_id=str(inbound_id), operation=kind, limiting_resource=limiting), admitted
        if admitted == "completion_reserved":
            pending = dict(self._carry_opening(checkpoint, raw), completion_repair=key, previous=self._previous(checkpoint, raw))
            try:
                raw = self._commit_checkpoint(pending)
            except OSError as exc:
                code = _durability_failure(exc, self.directory / CHECKPOINT).code
                return AppendResult("failed", record_id, seq, code), admitted
            checkpoint = pending
        if anchored_narrowing:
            failure = self._bind_narrowing(capability, candidate, checkpoint, raw, records, state,
                                           authorized_intent, reconstructing)
            if failure is not None:
                return AppendResult("indeterminate", record_id, seq, failure, journal_publication="absent"), admitted
        name = str(checkpoint["journal_file"])
        try:
            created = self.storage.append(name, line)
            self.storage.sync(name)
            if created:
                self.storage.sync_directory()
        except OSError as exc:
            code = _durability_failure(exc, self.directory / name).code
            return AppendResult("failed", record_id, seq, code), admitted
        stage = {"failure_status": "failed"}
        advanced = dict(
            self._carry_opening(checkpoint, raw),
            through_seq=seq,
            head_sha256=line_digest(line),
            byte_length=int(checkpoint["byte_length"]) + len(line),
            previous=self._previous(checkpoint, raw),
        )
        advanced.pop("completion_repair", None)
        if kind == "cross_ceremony_opened" and checkpoint["format"] == QUOTA_CHECKPOINT_FORMAT:
            day = candidate["utc_day"]
            count = state.quota_count + 1 if day == state.quota_day else 1
            advanced["quota"] = {"mode": "charged", "day": day, "count": count}
        try:
            sealed = self._commit_checkpoint(advanced, stage)
        except OSError as exc:
            code = _durability_failure(exc, self.directory / CHECKPOINT).code
            return AppendResult(stage["failure_status"], record_id, seq, code), admitted
        if self._anchor_store is not None and not creating:
            records, data = self._verified(advanced)
            try:
                self._settle_anchor(advanced, sealed, records, data,
                                    initial=capability.verb == "enable" and candidate.get("phase") == "enable_started",
                                    pair_advance=kind == "cross_pair_active" and capability.writer == "A"
                                    and capability.verb in {"pair_show", "pair_join"}, pair_phase="anchor")
            except ProtocolRefusal as exc:
                return AppendResult("committed_unpublished", record_id, seq, exc.code,
                                    journal_publication="published"), admitted
            if self._narrowing(candidate):
                return AppendResult("committed", record_id, seq, journal_publication="published",
                                    independent_denial="confirmed"), admitted
        return AppendResult("committed", record_id, seq), admitted

    @staticmethod
    def _require_root_verb(capability, writers, verb):
        if (capability.writer not in writers or capability.verb != verb or capability.pair_id is not None
            or capability.session is not None or capability.ceremony_token is not None):
            raise ProtocolRefusal("cross_capability_refused", f"this operation requires root-scoped {verb} authority")

    def close_abandoned_ceremony(self, capability: Capability, opening_id: str) -> AppendResult:
        """Close an unactivated opening under its independently verified abandonment lease."""
        self._require_root_verb(capability, {"M"}, "maintain")
        self._require_root_providers()
        if (self._abandoned_ceremony is None or not self.directory.is_dir()
            or not isinstance(opening_id, str) or _ENABLE_ID.fullmatch(opening_id) is None):
            raise ProtocolRefusal("cross_ceremony_abandonment_unproved", "the opening has no verified abandonment lease")
        with self._abandoned_ceremony(opening_id) as lease, self._read_lock(exclusive=True) as lock:
            if lock != "locked" or self._maintenance_due():
                raise self._pair_pending()
            checkpoint, raw = self._load()
            records, _ = self._verified(checkpoint)
            opening = next((row for row in records if row["kind"] == "cross_ceremony_opened"
                            and row.get("ceremony_id") == opening_id), None)
            anchor = self._anchor_coordinate(checkpoint, raw, records)
            if (opening is None or anchor is None or self._observed_store() != anchor
                or (opening.get("enable_id"), opening.get("enable_generation")) != anchor.trust[:2]
                or lease != (*anchor.trust[:2], opening_id, opening.get("ceremony_token"))
                or opening_id not in project(records).open_ceremonies):
                raise ProtocolRefusal("cross_ceremony_abandonment_unproved", "the lease does not cover this open ceremony")
            candidate = {
                "kind": "cross_ceremony_closed", "schema_version": 2, "id": f"cross-ceremony-closed-{opening_id}",
                "enable_id": anchor.trust[0], "enable_generation": anchor.trust[1], "ceremony_id": opening_id,
                "ceremony_token": opening["ceremony_token"], "cause": "crash",
            }
            check(capability, candidate)
            self._origin = capability.writer
            return self._append_with_reserve_file(capability, candidate)

    def reconstruct_disable(self, capability: Capability) -> AppendResult:
        """Publish an inert stop for a completed enable with independently confirmed anchor absence."""
        self._require_root_verb(capability, {"M"}, "maintain")
        self._require_root_providers()
        with self._read_lock(exclusive=True) as lock:
            if lock != "locked" or self._observed_store() is not None:
                raise self._anchor_refusal()
            loaded = self._load()
            if loaded is None:
                raise self._anchor_refusal()
            checkpoint, raw = loaded
            records, data = self._verified(checkpoint)
            if (checkpoint["format"] not in _MODERN_FORMATS or self._seal() != _sha256(raw)
                or len(data) != checkpoint["byte_length"] or checkpoint["repair_intent"] is not None
                or checkpoint["compaction_intent"] is not None or "completion_repair" in checkpoint):
                raise self._anchor_refusal()
            start = next((semantic(row) for row in reversed(records) if row.get("phase") == "enable_started"), None)
            if start is None or start.get("schema_version") not in (1, 2):
                raise self._anchor_refusal()
            completion = approved_completion(start)
            if not any(row["id"] == completion["id"] and semantic(row) == completion for row in records):
                raise self._anchor_refusal()
            candidate = closeout_record(start["enable_id"], "disable_started", ())
            candidate.update(schema_version=1, enable_generation=start["enable_generation"],
                             cause="anchor_absent_reconstructed")
            check(capability, candidate)
            self._origin = capability.writer
            return self._append_with_reserve_file(capability, candidate, reconstructing=True)

    def maintain(self, capability: Union[str, Capability] = "M") -> Dict[str, object]:
        capability = as_capability(capability)
        require(capability, "maintains")
        if not self._fixture_mode:
            self._require_root_verb(capability, {"M"}, "maintain")
            self._require_root_providers()
        self._origin = capability.writer
        if not self.directory.is_dir():
            return {"state": self._missing_view(False).state}
        with self._lock(exclusive=True):
            return self._maintain_locked(capability)

    def remove_disable_anchor(self, capability: Capability, enable_id: str, enable_generation: int) -> bool:
        """Confirm removal of the exact root anchor after its independently bound stop."""
        if (capability.writer != "L" or capability.verb != "disable" or capability.pair_id is not None
            or self._anchor_store is None or not self.directory.is_dir()
            or not isinstance(enable_id, str) or _ENABLE_ID.fullmatch(enable_id) is None
            or not _integer(enable_generation, 1, MAX_INTEGER)):
            raise self._anchor_refusal()
        with self._lock(exclusive=True):
            self._remove_disable_anchor_locked(enable_id, enable_generation)
            return True

    def _remove_disable_anchor_locked(self, enable_id, enable_generation) -> bool:
        loaded = self._load()
        if loaded is None:
            raise self._anchor_refusal()
        checkpoint, raw = loaded
        records, data = self._verified(checkpoint)
        observed = self._anchor_coordinate(checkpoint, raw, records)
        state = project(records)
        if (self._seal() != _sha256(raw) or len(data) != checkpoint["byte_length"]
            or checkpoint["repair_intent"] is not None or checkpoint["compaction_intent"] is not None
            or "disable_started" not in state.phases or observed is None
            or observed.trust[:2] != (enable_id, enable_generation)):
            raise self._anchor_refusal()
        current = self._observed_store()
        if current is None:
            return False
        if current != observed:
            raise self._anchor_refusal()
        self._replace_anchor(current, None)
        return True

    @staticmethod
    def _teardown_refusal():
        return ProtocolRefusal("cross_closeout_unproved", "the exact stopped inventory is unproved")

    def _teardown_prefix(self, attended):
        loaded = self._load()
        if loaded is None:
            raise self._teardown_refusal()
        checkpoint, raw = loaded
        position = checkpoint if self._seal() == _sha256(raw) else checkpoint["previous"]
        if position is None or (position is not checkpoint and self._seal() != position["checkpoint_sha256"]):
            raise self._teardown_refusal()
        records, _ = self._verified(position)
        index = next((index for index in range(len(records) - 1, -1, -1)
                      if records[index].get("phase") == "enable_started"), None)
        if index is None or semantic(records[index]) != attended or attended.get("schema_version") not in (1, 2):
            raise self._teardown_refusal()
        return checkpoint, raw, records, records[index:]

    def _teardown_record(self, start, records, step, items=(), proof=None):
        record = closeout_record(start["enable_id"], step, items)
        record.update(schema_version=1, enable_id=start["enable_id"], enable_generation=start["enable_generation"])
        if step.startswith("close:"):
            opening = next((row for row in reversed(records) if row["kind"] == "cross_ceremony_opened"
                            and row.get("ceremony_id") == record["ceremony_id"]), None)
            if opening is None:
                raise self._teardown_refusal()
            record.update(schema_version=2, ceremony_token=opening["ceremony_token"])
        if proof is not None:
            record.update(proof)
        return record

    def _teardown_inventory(self, start, scope, effects, witness):
        if self._enable_inventory is None:
            raise self._teardown_refusal()
        intents = {row["item"]: row["proof_sha256"] for row in scope
                   if row["kind"] == "cross_inventory_item" and row.get("phase") == "intent"
                   and row.get("token") == start["token"]}
        proofs = {item: self._enable_inventory.expected(item, start) for item in _ENABLE_ITEMS}
        if any(proofs.get(item) != proof for item, proof in intents.items() if item != "anchor"):
            raise self._teardown_refusal()
        extras = effects.inventory(dict(start), tuple(dict(row) for row in scope))
        if not isinstance(extras, Mapping) or set(extras) & (set(proofs) | {"anchor"}):
            raise self._teardown_refusal()
        proofs.update(extras)
        required = {f"pair:{row['pair_id']}" for row in scope if row["kind"] == "cross_pair_active"}
        if witness is not None:
            required.add(f"quota-migration:{witness.token}")
        if not required.issubset(proofs):
            raise self._teardown_refusal()
        return proofs

    def _teardown_binding(self, start, scope):
        stop = next((row for row in scope if row.get("phase") == "disable_started"), None)
        if (stop is None or stop.get("schema_version") != 1 or stop.get("enable_id") != start["enable_id"]
            or stop.get("enable_generation") != start["enable_generation"]
            or not isinstance(stop.get("trust_head"), str) or _SHA256.fullmatch(stop["trust_head"]) is None):
            raise self._teardown_refusal()
        return dict(format="floati-cross-closeout-inventory-v2", start_sha256=_sha256(encode_frame(start)),
                    stop_trust_head=stop["trust_head"])

    def _teardown_plan(self, start, scope, effects, witness):
        proofs = self._teardown_inventory(start, scope, effects, witness)
        binding = self._teardown_binding(start, scope)
        stored = self._ballast_file(_TEARDOWN_PLAN)
        if stored is not None:
            try:
                saved, = decode_frames(stored)
            except (FrameError, ValueError):
                saved = None
            if saved is not None:
                if (not isinstance(saved, dict) or set(saved) != set(binding) | {"items"}
                    or any(saved.get(key) != value for key, value in binding.items())
                    or not isinstance(saved["items"], dict) or encode_frame(saved) != stored
                    or any(saved["items"].get(item) != proof for item, proof in proofs.items())):
                    raise self._teardown_refusal()
                proofs = saved["items"]
        if (len(proofs) >= 160 or any(not isinstance(item, str) or _ITEM_ID.fullmatch(item) is None
                                     or not isinstance(proof, str) or _SHA256.fullmatch(proof) is None
                                     for item, proof in proofs.items())):
            raise self._teardown_refusal()
        pairs = {item.removeprefix("pair:") for item in proofs if item.startswith("pair:")}
        if len(pairs) > 64 or any(_ENABLE_ID.fullmatch(pair) is None for pair in pairs):
            raise self._teardown_refusal()
        self._fill_ballast_file(_TEARDOWN_PLAN, encode_frame(dict(binding, items=proofs)))
        self.storage.sync_directory()
        return proofs

    @staticmethod
    def _removed_item(item, removed):
        return {"item": item, "class": "confirmed_absent",
                "cause": "removed_and_verified" if removed else "absent_on_recheck"}

    def _dispose_scoped_ballast(self, start, proof) -> Optional[bool]:
        payload = self._ballast_file(BALLAST)
        receipt = self._ballast_file(BALLAST_INVENTORY)
        if payload is None and receipt is None:
            return None
        scope = self._ballast_scope(start, proof)
        if "intent" not in self._ballast_intent(start, proof):
            raise self._teardown_refusal()
        if payload is not None:
            if (not payload.startswith(_BALLAST_HEADER + bytes.fromhex(scope))
                or not self._ballast_bytes(scope, BALLAST_BYTES).startswith(payload)
                or (receipt is not None and not self._ballast_identity(scope).startswith(receipt))):
                raise self._teardown_refusal()
        elif receipt is not None:
            try:
                body, = decode_frames(receipt)
            except (FrameError, ValueError) as exc:
                raise self._teardown_refusal() from exc
            fields = {"format", "scope_sha256", "device", "inode", "ctime_ns", "size", "uid"}
            if (not isinstance(body, dict) or set(body) != fields or body["format"] != "floati-cross-ballast-inventory-v2"
                or body["scope_sha256"] != scope or encode_frame(body) != receipt or body["uid"] != os.getuid()
                or any(type(body[key]) is not int or body[key] < 0 for key in fields - {"format", "scope_sha256"})):
                raise self._teardown_refusal()
        # An unfinished sidecar must be resolved before deleting the only
        # inode that supplies its identity. After that, either unlink can be retried.
        if payload is not None and receipt is not None:
            self._fill_ballast_file(BALLAST_INVENTORY, self._ballast_identity(scope))
            self.storage.sync_directory()
        if payload is not None:
            self.storage.unlink(BALLAST)
        if receipt is not None:
            self.storage.unlink(BALLAST_INVENTORY)
        self.storage.sync_directory()
        if self._ballast_file(BALLAST) is not None or self._ballast_file(BALLAST_INVENTORY) is not None:
            raise self._teardown_refusal()
        return True

    def _measure_teardown_item(self, item, start, proof, effects, witness):
        if item == "ballast":
            removed = self._dispose_scoped_ballast(start, proof)
            if removed is not None:
                return self._removed_item(item, removed)
        if item.startswith("quota-migration:"):
            if self._quota_store is None or self._legacy_exclusion is None:
                raise self._quota_refusal()
            current = self._observed_quota()
            if current is not None:
                if current != witness or item != f"quota-migration:{current.token}":
                    raise self._quota_refusal()
                if (not self._quota_store.compare_and_set(current, None) or self._observed_quota() is not None):
                    raise self._quota_refusal()
            return self._removed_item(item, current is not None)
        observation = effects.read(item, dict(start), proof)
        if observation == ("safe_absent", None):
            return self._removed_item(item, False)
        if observation != ("owned", proof):
            raise self._teardown_refusal()
        effects.remove(item, dict(start), proof)
        if effects.read(item, dict(start), proof) != ("safe_absent", None):
            raise self._teardown_refusal()
        return self._removed_item(item, True)

    def teardown_disable(
        self, capability: Capability, attended: Mapping[str, object], *, effects: TeardownEffects,
        progress: Optional[TeardownProgress] = None,
    ) -> dict:
        """Fence, measure token-owned teardown and publish under one journal lock.

        Effects provide inventory(start, records), read(item, start, proof),
        remove(item, start, proof), and checks(start). Readback must report exact
        ownership or qualified absence. These callbacks must not lock the journal.
        A caller can collect progress even if teardown raises before a receipt.
        """
        if (capability.writer != "L" or capability.verb != "disable" or capability.pair_id is not None
            or self._anchor_store is None or not self.directory.is_dir()):
            raise self._teardown_refusal()
        self._origin = capability.writer
        with ExitStack() as exclusion:
            if self._root_provenance is not None:
                root, source = self._root_provenance
                exclusion.enter_context(source.exclude(root))
                self._check_root_binding(root)
            if self._legacy_exclusion is not None:
                exclusion.enter_context(self._legacy_exclusion())
            exclusion.enter_context(self._lock(exclusive=True))
            return self._teardown_disable_locked(capability, dict(attended), effects, progress or TeardownProgress())

    def _teardown_disable_locked(self, capability, start, effects, progress):
        self._teardown_prefix(start)
        self._maintain_as(capability)
        steps = []
        while True:
            checkpoint, raw, records, scope = self._teardown_prefix(start)
            state = project(records)
            plan = obligations(state)
            if not plan:
                self._retire_teardown_plan(start, scope)
                return {"state": "complete", "steps": steps}
            step = plan[0][0]
            if step in steps:
                raise self._teardown_refusal()
            if step == "disable_started":
                current = self._observed_store()
                if isinstance(current, EnableCreate):
                    initial = self._enable_initial(current, checkpoint, raw, records)
                    record = self._teardown_record(start, records, step)
                    self._keep_store_predecessor(raw)
                    self._replace_anchor(current, Narrowing(initial, self._position_key(checkpoint), _sha256(raw),
                                                            encode_frame(record)))
            items, proof = (), None
            if step.startswith("revoke:") or step == "receipt":
                try:
                    witness = self._observed_quota()
                    if witness is not None:
                        _, data = self._verified(checkpoint)
                        if self._legacy_exclusion is None or not self._quota_reference_matches(witness, checkpoint, data):
                            raise self._quota_refusal()
                    # Keep all pair resources before the first removal of an
                    # active pair from the projection. Compaction may drop its row.
                    inventory = self._teardown_plan(start, scope, effects, witness)
                    progress.inventory = {item: {"item": item, "class": "unproved"}
                                          for item in sorted({"anchor", *inventory})}
                    if step == "receipt":
                        items, proof = self._measure_teardown(start, inventory, effects, witness, progress)
                except (OSError, ProtocolRefusal) as exc:
                    return {"state": "interrupted", "step": step,
                            "code": getattr(exc, "code", "cross_closeout_unproved")}
            record = self._teardown_record(start, records, step, items, proof)
            check(capability, record)
            result = self._execute_closeout_step(capability, record)
            if result.status not in {"committed", "existing"}:
                return {"state": "interrupted", "step": step, "status": result.status, "code": result.code,
                        "limiting_resource": result.limiting_resource}
            steps.append(step)

    def _measure_teardown(self, start, proofs, effects, witness, progress):
        removed = self._remove_disable_anchor_locked(start["enable_id"], start["enable_generation"])
        items = [self._removed_item("anchor", removed)]
        progress.inventory["anchor"] = items[0]
        for item, proof in sorted(proofs.items()):
            outcome = self._measure_teardown_item(item, start, proof, effects, witness)
            progress.inventory[item] = outcome
            items.append(outcome)
        checks = effects.checks(dict(start))
        if isinstance(checks, Mapping) and set(checks) <= _TEARDOWN_CHECKS:
            progress.checks.update((key, value) for key, value in checks.items() if type(value) is bool)
        if not isinstance(checks, Mapping) or set(checks) != _TEARDOWN_CHECKS or any(v is not True for v in checks.values()):
            raise self._teardown_refusal()
        if self._observed_store() is not None or self._observed_quota() is not None:
            raise self._teardown_refusal()
        pairs = sorted(item.removeprefix("pair:") for item in proofs if item.startswith("pair:"))
        return sorted(items, key=lambda item: item["item"]), {
            "checks": dict(checks), "notices": [{"pair_id": pair, "outcome": "UNKNOWN"} for pair in pairs],
            "step_ms": [None] * 7, "total_ms": None,
        }

    def _retire_teardown_plan(self, start, scope):
        stored = self._ballast_file(_TEARDOWN_PLAN)
        if stored is None:
            return
        receipt = next((row for row in scope if row["kind"] == "cross_teardown_receipt"
                        and row.get("outcome") == "complete" and row.get("enable_id") == start["enable_id"]
                        and row.get("enable_generation") == start["enable_generation"]), None)
        try:
            plan, = decode_frames(stored)
        except (FrameError, ValueError) as exc:
            raise self._teardown_refusal() from exc
        binding = self._teardown_binding(start, scope)
        if (receipt is None or not isinstance(plan, dict) or encode_frame(plan) != stored
            or set(plan) != set(binding) | {"items"} or not isinstance(plan.get("items"), dict)
            or any(plan[key] != value for key, value in binding.items())
            or set(plan["items"]) | {"anchor"} != {item["item"] for item in receipt["items"]}):
            raise self._teardown_refusal()
        self.storage.unlink(_TEARDOWN_PLAN)
        self.storage.sync_directory()

    def closeout(
        self, capability: Union[str, Capability], receipt_items: Sequence[Mapping[str, object]], *,
        teardown_proof: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        """Finish disable, one plan step per lock, each decided from committed state.

        Every process derives the same record for the same step, so a resumed
        or racing shutdown appends each step exactly once.
        """

        capability = as_capability(capability)
        require(capability, "closes_out")
        if capability.verb != "disable":
            raise ProtocolRefusal("cross_capability_refused", "close-out requires the disable capability")
        self._origin = capability.writer
        done: List[str] = []
        previous_step = None
        while True:
            with self._lock(exclusive=True):
                loaded = self._load()
                if loaded is None:
                    raise ProtocolRefusal("cross_journal_absent", "this root has no cross journal")
                checkpoint, raw = loaded
                position = checkpoint
                if self._seal() != _sha256(raw) and checkpoint["previous"] is not None:
                    position = checkpoint["previous"]
                records, _ = self._verified(position)
                plan = obligations(project(records))
                if not plan:
                    self._maintain_as(capability)
                    return {"state": "complete", "steps": done}
                step = plan[0][0]
                if step == previous_step:
                    raise IntegrityFailure("cross_closeout_stalled", f"step {step} did not retire")
                enable_id = str(loaded[0]["journal_id"])[len("cross-"):].rsplit("-g", 1)[0]
                record = closeout_record(enable_id, step, receipt_items)
                if self._enable_approval is not None:
                    started = next((row for row in reversed(records) if row.get("phase") == "enable_started"), None)
                    if started is None or started.get("enable_id") != enable_id:
                        raise ProtocolRefusal("cross_enable_approval_invalid", "the close-out enable is unproved")
                    record.update(schema_version=1, enable_id=enable_id, enable_generation=started["enable_generation"])
                    if step.startswith("close:"):
                        opening = next((row for row in reversed(records) if row["kind"] == "cross_ceremony_opened"
                                        and row.get("ceremony_id") == record["ceremony_id"]), None)
                        if opening is None:
                            raise _corrupt("close-out has no scoped ceremony opening")
                        record.update(schema_version=2, ceremony_token=opening["ceremony_token"])
                    if step == "receipt":
                        if teardown_proof is None:
                            return {"state": "interrupted", "step": step, "code": "cross_closeout_record_pending"}
                        if set(teardown_proof) != {"notices", "checks", "step_ms", "total_ms"}:
                            raise ProtocolRefusal("cross_priced_record_invalid", "the teardown proof has the wrong members")
                        record.update(teardown_proof)
                check(capability, record)
                result = self._execute_closeout_step(capability, record)
            if result.status not in {"committed", "existing"}:
                return {"state": "interrupted", "step": step, "status": result.status, "code": result.code,
                        "limiting_resource": result.limiting_resource}
            done.append(step)
            previous_step = step

    def resolve(self, capability: Union[str, Capability], record: Mapping[str, object]) -> str:
        """Observe an immutable scoped operation without acquiring repair authority."""
        capability = as_capability(capability)
        candidate = self._validate(record)
        check(capability, candidate)
        if not self._fixture_mode and candidate["kind"] == "remote_endpoint_registered":
            raise endpoint_refusal()
        if not self._fixture_mode and capability.writer == "L" and candidate["kind"] in _SENDER_KINDS:
            if candidate["kind"] != "remote_outbound" or candidate.get("schema_version") != 2:
                raise ProtocolRefusal("cross_resolution_evidence_required", "resolve the sender through its command entry")
            validate_outbound(candidate)
            if (capability.verb != candidate["verb"] or self._authorize_local_send is None
                or self._authorize_local_send(capability, candidate["envelope"]["sender_node"]) is not True):
                raise ProtocolRefusal("cross_capability_refused", "resolution requires its authenticated local sender")
        if candidate["kind"] in COMPLETION_KINDS | {"cross_ceremony_opened"}:
            raise ProtocolRefusal("cross_resolution_evidence_required", "retry this operation with its original evidence")
        with self._read_lock() as lock:
            if lock != "locked":
                return "pending" if self._missing_with_exclusion(lock, False).pending else "unknown"
            if self._maintenance_due():
                return "pending"
            loaded = self._load()
            if loaded is None:
                return "pending" if self._missing_view(require_settled=False).pending else "unknown"
            records, _ = self._verified(loaded[0])
            state = project(records)
            if ((self._authenticate_open is not None or not self._fixture_mode) and capability.writer == "I"
                and capability.verb != "endpoint_start"):
                self._check_live_stream(capability, records, disabling="disable_started" in state.phases)
            check_data_scope(state, candidate, capability.pair_id)
            check_committed(capability, candidate, records)
            if candidate["kind"] in DATA_KINDS and candidate["kind"] != "remote_hold_rejected":
                self._require_admitting_pair(state, candidate.get("pair_id"), *loaded, records)
            if candidate["kind"] in DATA_KINDS and (
                "enable_complete" not in state.phases
                or (candidate["kind"] != "remote_endpoint_registered" and candidate.get("pair_id") not in state.active_pairs)
            ):
                raise ProtocolRefusal("cross_pair_scope_refused", "resolution requires a current eligible pair")
            for row in records:
                if row["id"] != candidate["id"]:
                    continue
                if row["kind"] != candidate["kind"] or row.get("pair_id") != candidate.get("pair_id"):
                    raise ProtocolRefusal("cross_pair_scope_refused", "the operation is outside this scope")
                stored = semantic(row)
                stored.pop("completion_reserved", None)
                return "committed" if stored == candidate else "conflict"
            return "absent" if loaded[0]["generation"] == 1 else "unknown"


def closeout_record(enable_id: str, step: str, receipt_items) -> Dict[str, object]:
    """The one record for one close-out step; its id is the same in every process."""

    base: Dict[str, object] = {"schema_version": 0}
    if step == "disable_started":
        return dict(base, id=f"cross-optin-{enable_id}-disable-started", kind="cross_optin",
                    phase="disable_started", enable_id=enable_id, cause="local")
    if step.startswith("close:"):
        ceremony = step.split(":", 1)[1]
        return dict(base, id=f"cross-ceremony-closed-{ceremony}", kind="cross_ceremony_closed",
                    ceremony_id=ceremony, cause="disabled")
    if step.startswith("revoke:"):
        pair = step.split(":", 1)[1]
        return dict(base, id=f"cross-pair-revoked-{pair}", kind="cross_pair_revoked",
                    pair_id=pair, cause="disable", idempotency_key=None)
    if step == "receipt":
        return dict(base, id=f"cross-teardown-{enable_id}-complete", kind="cross_teardown_receipt",
                    outcome="complete", items=[dict(item) for item in receipt_items])
    if step == "disable_complete":
        return dict(base, id=f"cross-optin-{enable_id}-disable-complete", kind="cross_optin",
                    phase="disable_complete", enable_id=enable_id, cause=None)
    raise ValueError(f"unknown close-out step {step!r}")


__all__ = [
    "AppendResult",
    "CommittedView",
    "CrossJournal",
    "RootJournalProvenance",
    "Storage",
    "TeardownEffects",
    "genesis_head",
    "journal_file",
    "line_digest",
    "parse_checkpoint",
    "verify_prefix",
]
