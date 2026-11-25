"""Dependency-closed carry; existing uncompressed archives remain the proof.

No derived state is persisted. Verification caches are process-local and bind
complete archive membership, every native identity field, receipt facts and the
parser contract. A cold process must still verify the required archive chain.
"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat

from .errors import IntegrityFailure
from .epoch_prefix import _ArchivePrefixProjection, _projection_contract, _projector_arguments
from .framing import decode_frames, encode_frame
from .jsonl import (MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, VerifiedLedgerCursor,
                    _decode_path_records)
from .records import WAKE_HOLD_KINDS, validate_record


_FACT_CACHE = OrderedDict()
_ORIGIN_CACHE = OrderedDict()
_PROOF_SEAL = object()
MAX_DEPENDENCY_WITNESS_ENTRIES = 100000
MAX_DEPENDENCY_WITNESS_DEPTH = 64


def _failure(detail):
    return IntegrityFailure("epoch_carry_evidence_invalid", detail)


def carry_dependency_witness(root):
    """Cheap advisory no-op witness, never archive admission or write authority.

    Include selected membership, every unselected plane (including open holds
    and presentation write-ahead state), and retained archive membership and
    native seven-field identities. No bytes are read. A changing/unavailable
    namespace returns None and therefore cannot inherit a cached no-op.
    """
    from .bus_epoch import _selected_member_identity as identity
    from .adhoc_records import PROVIDER_STATE_DIRECTORY
    rows, frames, contexts, descriptors = [], [], [], set()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    def enter(descriptor, relative, before, parent=None, name=None):
        if identity(os.fstat(descriptor)) != identity(before):
            raise OSError("namespace directory changed before enumeration")
        if parent is not None and identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(before):
            raise OSError("namespace directory name changed before enumeration")
        manager = os.scandir(descriptor)
        contexts.append(manager)
        iterator = manager.__enter__()
        frames.append((descriptor, relative, before, parent, name, manager, iterator))
    try:
        before = root.tenant_home.lstat()
        if not stat.S_ISDIR(before.st_mode) or MAX_DEPENDENCY_WITNESS_ENTRIES < 1:
            return None
        descriptor = os.open(root.tenant_home, flags)
        descriptors.add(descriptor)
        rows.append((".", identity(before)))
        enter(descriptor, Path(), before)
        while frames:
            descriptor, relative, before, parent, name, manager, iterator = frames[-1]
            # One bounded EOF probe preserves complete trees exactly at the
            # row limit; no over-limit entry is retained or descended.
            try:
                entry = next(iterator)
            except StopIteration:
                if identity(os.fstat(descriptor)) != identity(before):
                    return None
                if parent is not None and identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(before):
                    return None
                manager.__exit__(None, None, None)
                contexts.remove(manager)
                frames.pop()
                descriptors.remove(descriptor)
                os.close(descriptor)
                continue
            # Provider-native conversation storage is outside bus epoch truth.
            # Prune the private namespace before observing or descending it.
            if relative == Path() and entry.name == PROVIDER_STATE_DIRECTORY.name:
                continue
            if len(rows) >= MAX_DEPENDENCY_WITNESS_ENTRIES:
                return None
            child = relative / entry.name
            if len(os.fsencode(child)) > 4096:
                return None
            status = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
            rows.append((child.as_posix(), identity(status)))
            if stat.S_ISDIR(status.st_mode):
                if len(frames) >= MAX_DEPENDENCY_WITNESS_DEPTH:
                    return None
                child_descriptor = os.open(entry.name, flags, dir_fd=descriptor)
                descriptors.add(child_descriptor)
                enter(child_descriptor, child, status, descriptor, entry.name)
        if identity(root.tenant_home.lstat()) != rows[0][1]:
            return None
        return tuple(sorted(rows))
    except OSError:
        return None
    finally:
        for manager in reversed(contexts):
            try:
                manager.__exit__(None, None, None)
            except OSError:
                pass
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def unique_epoch_rows(batches):
    """Coalesce carried identities only when their entire native row is exact."""
    seen, result = {}, []
    for batch in batches:
        for row in batch:
            identity, frame = row["id"], encode_frame(row)
            if identity in seen:
                if seen[identity] != frame:
                    raise _failure("one carried record ID has conflicting native testimony")
                continue
            seen[identity] = frame
            result.append(row)
    return result


def _decode(root, relative, raw, kinds, domain, *, events=False):
    if not isinstance(raw, bytes) or len(raw) > MAX_LEDGER_BYTES:
        raise _failure("selected source exceeds its native bound")
    unknown = {} if events else None
    rows = _decode_path_records(root.tenant_home / relative, root.tenant_id,
                                frozenset(kinds), raw, unrecognized=unknown)
    physical = decode_frames(raw)
    frames = raw.splitlines(keepends=True)
    if len(frames) != len(physical) or len(frames) > MAX_LEDGER_RECORDS:
        raise _failure("selected physical framing is inconsistent")
    from .records import is_known_record_kind
    known = [(frame, row) for frame, row in zip(frames, physical)
             if not events or is_known_record_kind(row["kind"])]
    if len(known) != len(rows) or any(frame != encode_frame(row)
            for (frame, _physical), row in zip(known, rows)):
        raise _failure("selected source is not native canonical framing")
    digest = VerifiedLedgerCursor._initial_digest(domain)
    prefixes = [digest.hexdigest()]
    for frame, _row in known:
        digest.update(frame)
        prefixes.append(digest.hexdigest())
    return rows, tuple(prefixes), tuple(zip(frames, physical))


def _binding(source, node, worker, tenant):
    return (tenant, node, worker, tuple(tuple(encode_frame(row) for row in source[index])
            for index in (0, 2, 4)), tuple(tuple(source[index]) for index in (1, 3, 5)))


def _original_hold_cuts(source, receipt, ordinal):
    """Locate an original physical hold cut; carried copies return None.

    Shared by native Path-backed resolution and pure survivor-data replay.
    Merely locating these cuts grants no runtime proof admission.
    """
    events, ep, deliveries, dp, acknowledgments, ap = source
    from .archive_survivor_sources import _query_charge
    # Membership and locating the matching prefix each scan these two axes.
    # No allowance means unchanged ordinary native resolver behavior.
    _query_charge(2 * len(ep) + 2 * len(ap) + 1)
    if (receipt.get("delivery_prefix_digest") != dp[ordinal]
            or receipt.get("event_prefix_digest") not in ep
            or receipt.get("acknowledgment_prefix_digest") not in ap):
        return None
    event_count = max(index for index, value in enumerate(ep)
                      if value == receipt["event_prefix_digest"])
    ack_count = ap.index(receipt["acknowledgment_prefix_digest"])
    # Exact combined lengths of the twelve source/prefix reference slices:
    # before = 2*(e+d+a)+3; after = 2*(e+d+a)+5. Recursive and repeated
    # kernel/native calls pay again at this actual allocation boundary.
    _query_charge(4 * (event_count + ordinal + ack_count) + 8)
    before = (events[:event_count], ep[:event_count + 1], deliveries[:ordinal],
              dp[:ordinal + 1], acknowledgments[:ack_count], ap[:ack_count + 1])
    after = (events[:event_count], ep[:event_count + 1], deliveries[:ordinal + 1],
             dp[:ordinal + 2], acknowledgments[:ack_count], ap[:ack_count + 1])
    return before, after


def _imported_hold_origins(source, node, worker, tenant, lookup):
    """Check exact imported origin DATA, without returning admission objects."""
    events, ep, deliveries, dp, acknowledgments, ap = source
    imported = [row for index, row in enumerate(deliveries)
                if row.get("kind") == "wake_hold_receipt" and row.get("schema_version") != 3 and (
                    row.get("delivery_prefix_digest") != dp[index]
                    or row.get("event_prefix_digest") not in ep
                    or row.get("acknowledgment_prefix_digest") not in ap)]
    if not imported or not events or events[0].get("kind") != "bus_epoch_roll_receipt":
        return {}
    origins = {row["id"]: lookup(row) for row in imported}
    current_messages = {row["id"]: encode_frame(row) for row in events
                        if row.get("kind") == "message_envelope"}
    for receipt in imported:
        original_source = origins[receipt["id"]][1]
        original_messages = {row["id"]: row for row in original_source[0]
                             if row.get("kind") == "message_envelope"}
        if (origins[receipt["id"]][0] != encode_frame(receipt)
                or any(item not in original_messages or current_messages.get(item) != encode_frame(original_messages[item])
                       for item in receipt["item_ids"])):
            raise _failure("imported hold envelope differs from its exact original native frame")
    return origins


def _project_origin_data(source, node, worker, tenant, origins):
    """Pure native replay; any temporary admission stays inside this call.

    Origins supplied here are data for a semantic check, not committed authority.
    This function deliberately returns only projected states, never the sealed
    proof used internally by the existing projector.
    """
    from .wake_hold import project_wake_items
    proofs = (_VerifiedHoldProofs(_binding(source, node, worker, tenant), origins, _PROOF_SEAL)
              if origins else None)
    return project_wake_items(events=source[0], deliveries=source[2], acknowledgments=source[4],
        recipient=node, worker_session_id=worker, tenant_id=tenant,
        event_prefix_digests=source[1], delivery_prefix_digests=source[3],
        acknowledgment_prefix_digests=source[5], _hold_proofs=proofs)


class _VerifiedHoldProofs:
    """Only the native resolver can issue admission for one exact live cut."""
    def __init__(self, binding, origins, seal):
        if seal is not _PROOF_SEAL:
            raise _failure("imported hold proof was not issued by the native resolver")
        self.binding, self._origins = binding, origins

    @property
    def origins(self):
        """Inspection never lends the resolver's private native testimony."""
        return deepcopy(self._origins)

    def assert_source(self, source, node, worker, tenant):
        if self.binding != _binding(source, node, worker, tenant):
            raise _failure("imported hold proof does not bind this exact source cut")

    def validates(self, receipt):
        origin = self._origins.get(receipt.get("id"))
        return origin is not None and encode_frame(receipt) == origin[0]

    def original(self, receipt):
        if not self.validates(receipt):
            raise _failure("imported hold does not reproduce its verified origin")
        return deepcopy(self._origins[receipt["id"]][1:])


class ArchiveProofResolver:
    """Resolve only native receipt-named archives, never arbitrary directories."""
    def __init__(self, root, *, frozen_payloads=None, head=None, _planning=None):
        self.root = root
        # Collector-only budget. Ordinary native readers retain their defaults.
        self._planning = _planning
        self.frozen = dict((relative, raw) for relative, _plane, raw in frozen_payloads) if frozen_payloads is not None else None
        self.head = head
        self._paths = None
        self._heads = None
        self._sources = {}
        self._origins = {}
        self._resolving = set()
        self._planning_lineage_work = 0
        self._planning_snapshots = {}
        self._verification_depth = 0
        self._admitted_archives = None
        self._prefix_projections = None

    @contextmanager
    def _verification_scope(self):
        """Only nested calls in one outer hold verification share admission."""
        if self._verification_depth == 0 and self._planning is None:
            self._admitted_archives = {}
            self._prefix_projections = {}
        self._verification_depth += 1
        try:
            yield
        finally:
            self._verification_depth -= 1
            if self._verification_depth == 0:
                self._admitted_archives = None
                self._prefix_projections = None

    def _source_contract(self):
        from . import bus_epoch, events, jsonl, records
        return (ArchiveProofResolver._verification_scope, ArchiveProofResolver._source_contract,
            ArchiveProofResolver._source, ArchiveProofResolver.source, _decode,
            _decode_path_records, jsonl._decode_path_records, jsonl._decode_bounded_frames,
            validate_record, jsonl.validate_record,
            records.is_known_record_kind, jsonl.is_known_record_kind, jsonl.validate_unknown_record,
            events.validate_event_records, decode_frames, jsonl.decode_frames, encode_frame,
            VerifiedLedgerCursor._initial_digest, bus_epoch._read_bound_regular,
            bus_epoch._epoch_archive_snapshot, bus_epoch._assert_epoch_archive_snapshot,
            bus_epoch._selected_member_identity, MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS,
            jsonl.MAX_LEDGER_BYTES, jsonl.MAX_LEDGER_RECORDS, jsonl.MAX_RECORD_BYTES,
            ArchiveProofResolver._project_archived_cut, type(self)._project_archived_cut,
            type(self)._hold_origin, _ArchivePrefixProjection,
            _ArchivePrefixProjection.__init__, _ArchivePrefixProjection._admit,
            _ArchivePrefixProjection.project, _projector_arguments,
            _projection_contract, _projection_contract())

    def archives(self):
        if self._paths is not None:
            if self._planning is not None:
                self._planning.step(self._planning_lineage_work)
            # No invocation-local proof can survive changed archive membership.
            for path, snapshot in self._paths:
                from .bus_epoch import _assert_epoch_archive_snapshot
                if self._planning is None:
                    _assert_epoch_archive_snapshot(path, snapshot)
                else:
                    self._planning.assert_archive_snapshot(path, snapshot)
            return tuple(path for path, _snapshot in self._paths)
        from . import bus_epoch, jsonl
        if self.head is None:
            raw = self.frozen.get("events.jsonl", b"") if self.frozen is not None else (
                self.root.tenant_home / "events.jsonl").read_bytes()
            first, newline, _tail = raw[:65537].partition(b"\n")
            head = bus_epoch._parse_physical_first_roll_receipt(first + newline, self.root.tenant_id)
        else:
            head = self.head
        paths, heads, seen = [], [], set()
        while head is not None and head.get("kind") == "bus_epoch_roll_receipt":
            if len(paths) >= jsonl.MAX_LEDGER_RECORDS:
                raise _failure("archive receipt lineage exceeds the native record bound")
            receipt = validate_record(dict(head), self.root.tenant_id,
                frozenset({"bus_epoch_roll_receipt"}), integrity=True)
            path = bus_epoch._epoch_receipt_archive_path(self.root, receipt)
            if path in seen:
                raise _failure("archive receipt lineage contains a cycle")
            seen.add(path)
            snapshot = (bus_epoch._epoch_archive_snapshot(path) if self._planning is None
                        else self._planning.archive_snapshot(path))
            if self._planning is not None:
                self._planning.step(1 + len(snapshot))
            identities = tuple((name, plane, bus_epoch._selected_member_identity(status))
                               for name, plane, status in snapshot)
            contract = (jsonl._decode_path_records, jsonl.validate_record, decode_frames,
                        encode_frame, bus_epoch._read_epoch_archive_payloads,
                        bus_epoch._archive_facts_from_payloads, bus_epoch._epoch_archive_snapshot,
                        bus_epoch._assert_epoch_archive_snapshot, jsonl.decode_frames,
                        bus_epoch._parse_physical_first_roll_receipt,
                        bus_epoch.decode_frames, bus_epoch.validate_record,
                        jsonl.is_known_record_kind, jsonl.validate_unknown_record,
                        jsonl.MAX_LEDGER_BYTES, jsonl.MAX_LEDGER_RECORDS, jsonl.MAX_RECORD_BYTES)
            key = (self.root.tenant_home, self.root.tenant_id, path,
                   encode_frame(receipt), identities, contract)
            cached = _FACT_CACHE.get(key) if self._planning is None else None
            if cached is None:
                payloads, bound = (bus_epoch._read_epoch_archive_payloads(path) if self._planning is None
                                   else self._planning.archive_payloads(path))
                facts = bus_epoch._archive_facts_from_payloads(payloads)
                if any(receipt.get(field) != value for field, value in facts.items()):
                    raise _failure("receipt-owned archive facts changed")
                events = next(raw for relative, _plane, raw in payloads if relative == "events.jsonl")
                first, newline, _tail = events[:65537].partition(b"\n")
                next_head = bus_epoch._parse_physical_first_roll_receipt(
                    first + newline, self.root.tenant_id)
                if self._planning is None:
                    bus_epoch._assert_epoch_archive_snapshot(path, bound)
                else:
                    self._planning.assert_archive_snapshot(path, bound)
                # Cache an immutable encoded first frame, not public mutable rows.
                cached = encode_frame(next_head) if next_head is not None else b""
                if self._planning is None:
                    _FACT_CACHE[key] = cached
                    _FACT_CACHE.move_to_end(key)
                    while len(_FACT_CACHE) > 32:
                        _FACT_CACHE.popitem(last=False)
            if self._planning is None:
                bus_epoch._assert_epoch_archive_snapshot(path, snapshot)
            else:
                self._planning.assert_archive_snapshot(path, snapshot)
            paths.append((path, snapshot))
            head = bus_epoch._parse_physical_first_roll_receipt(cached, self.root.tenant_id)
            heads.append((path, head))
        self._paths = tuple(paths)
        self._heads = tuple(heads)
        if self._planning is not None:
            self._planning_snapshots = dict(paths)
            self._planning_lineage_work = sum(1 + len(snapshot) for _path, snapshot in paths)
        return tuple(path for path, _snapshot in paths)

    def epoch_homes(self, genesis):
        """Map verified archived anchors without decoding unrelated payloads."""
        self.archives()
        result = {}
        for path, first in self._heads:
            anchor = str(first["id"]) if first and first.get("kind") == "bus_epoch_roll_receipt" else genesis
            if anchor in result:
                raise _failure("native epoch lineage has duplicate anchors")
            result[anchor] = path
        return result

    def source(self, home, node, worker=None):
        """Return detached source rows; native internals borrow only privately."""
        result = self._source(home, node, worker)
        if self._planning is not None:
            self._planning.source(result)
        return deepcopy(result)

    def _source(self, home, node, worker=None):
        from .cursor import SparseCursor
        from .events import EVENT_KINDS, validate_event_records
        from .wake_hold import WakeHoldController
        coordinates = (("events.jsonl", EVENT_KINDS, WakeHoldController._EVENT_DOMAIN),
            (SparseCursor(self.root)._delivery_relative_path_for(node, worker_session_id=worker).as_posix(),
             WAKE_HOLD_KINDS, WakeHoldController._DELIVERY_DOMAIN),
            (SparseCursor(self.root)._relative_path_for(node, worker_session_id=worker).as_posix(),
             {"ack_receipt"}, WakeHoldController._ACK_DOMAIN))
        contract = self._source_contract()
        admission_key = None
        if home != self.root.tenant_home and home not in self.archives():
            raise _failure("imported proof source is outside native archive lineage")
        if home != self.root.tenant_home and self._planning is None and self._admitted_archives is not None:
            from .bus_epoch import _assert_epoch_archive_snapshot, _selected_member_identity
            bound = dict(self._paths)[home]
            admission_key = (self.root.tenant_home, self.root.tenant_id, home, node, worker,
                tuple((relative, frozenset(kinds), domain) for relative, kinds, domain in coordinates),
                tuple((name, plane, _selected_member_identity(status)) for name, plane, status in bound),
                contract)
            admitted = self._admitted_archives.get(admission_key)
            if admitted is not None:
                # archives() above rechecked the entire receipt-owned lineage;
                # the target's complete membership/identities still bind this hit.
                _assert_epoch_archive_snapshot(home, bound)
                return admitted
        result = []
        for relative, kinds, domain in coordinates:
            if home == self.root.tenant_home and self.frozen is not None:
                raw = self.frozen.get(relative, b"")
            else:
                path = home / relative
                if home != self.root.tenant_home:
                    from .bus_epoch import _read_bound_regular
                    bound = (self._planning_snapshots[home] if self._planning is not None
                             else dict(self._paths)[home])
                    snapshot = dict((name, status) for name, _plane, status in bound)
                    status = snapshot.get(relative)
                    raw = b"" if status is None else (self._planning.read_member(path, status)
                        if self._planning is not None else _read_bound_regular(path, status,
                        code="epoch_carry_evidence_invalid", detail="archive source changed during its bound read"))
                else:
                    # Reader callers normally supply their native live six-tuple;
                    # this branch is for explicit historical-cut reconstruction.
                    if path.exists():
                        raw = (path.read_bytes() if self._planning is None
                               else self._planning.read_member(path, path.lstat()))
                    else:
                        raw = b""
            if self._planning is not None:
                self._planning.raw_source(raw)
            key = (home, relative, kinds if isinstance(kinds, frozenset) else frozenset(kinds), domain,
                   hashlib.sha256(raw).digest(), contract)
            if self._planning is not None:
                key += (self._planning.contract(), _decode, encode_frame,
                        _decode_path_records, validate_record, MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS)
            decoded = self._sources.get(key)
            if decoded is None:
                decoded = _decode(self.root, (home.relative_to(self.root.tenant_home) / relative).as_posix(),
                                  raw, kinds, domain, events=relative == "events.jsonl")[:2]
                self._sources[key] = decoded
            result.extend(decoded)
        validate_event_records(result[0])
        if home != self.root.tenant_home:
            from .bus_epoch import _assert_epoch_archive_snapshot
            if self._planning is None:
                _assert_epoch_archive_snapshot(home, dict(self._paths)[home])
            else:
                self._planning.assert_archive_snapshot(home, self._planning_snapshots[home])
        result = tuple(result)
        if admission_key is not None:
            # Publish only after all bound reads, canonical decoding, native
            # event semantics and the final archive assertion succeeded.
            self._admitted_archives[admission_key] = result
        return result

    def _project_archived_cut(self, source, counts, home, node, worker):
        """One interception seam; unsupported cuts keep sealed native replay."""
        from .wake_hold import project_wake_items
        from .bus_epoch import _assert_epoch_archive_snapshot
        if (home != self.root.tenant_home and self._prefix_projections is not None
                and self._admitted_archives is not None):
            # Reproduce complete lineage membership and seven-field identities
            # before every borrowed projection, including an already built hit.
            self.archives()
            bound = dict(self._paths)[home]
            _assert_epoch_archive_snapshot(home, bound)
            contract = (self._source_contract(), project_wake_items)
            private = any(key[2:5] == (home, node, worker) and admitted is source
                and key[-1] == contract[0]
                for key, admitted in self._admitted_archives.items())
            if private:
                key = (home, node, worker, id(source), contract)
                projection = self._prefix_projections.get(key)
                if projection is None:
                    projection = _ArchivePrefixProjection(source, node, worker,
                        self.root.tenant_id, native_projector=project_wake_items)
                    self._prefix_projections[key] = projection
                states = projection.project(counts)
                if states is not None:
                    return states
        if self._planning is not None:
            self._planning.source(source)
        requested = tuple(part for plane, count in zip((0, 2, 4), counts)
            for part in (source[plane][:count], source[plane + 1][:count + 1]))
        proofs = verified_hold_proofs(self.root, requested, node, worker, home=home, resolver=self)
        arguments = _projector_arguments(requested, node, worker, self.root.tenant_id)
        return project_wake_items(**arguments, _hold_proofs=proofs)

    def hold_origin(self, receipt, node, worker, home):
        """Public origin testimony never lends private admission or states."""
        if self._planning is None:
            return deepcopy(self._hold_origin(receipt, node, worker, home))
        with self._planning.origin():
            result = self._hold_origin(receipt, node, worker, home)
            self._planning.source(result[1])
            return deepcopy(result)

    def _hold_origin(self, receipt, node, worker, home):
        from .wake_hold import project_wake_items
        from . import jsonl
        from .bus_epoch import _selected_member_identity
        frame = encode_frame(receipt)
        self.archives()
        # A retained proof is admitted only after the entire archive lineage
        # has just reproduced its membership and all seven identity fields.
        # It never contains public caller-owned mutable source objects.
        if self._planning is not None:
            self._planning.step(self._planning_lineage_work)
        lineage = tuple((path, tuple((name, plane, _selected_member_identity(status))
            for name, plane, status in snapshot)) for path, snapshot in self._paths)
        contract = (project_wake_items, validate_record, jsonl.validate_record,
                    jsonl._decode_path_records, jsonl._decode_bounded_frames,
                    jsonl.decode_frames, jsonl.is_known_record_kind,
                    jsonl.validate_unknown_record, decode_frames, encode_frame,
                    VerifiedLedgerCursor._initial_digest,
                    jsonl.MAX_LEDGER_BYTES, jsonl.MAX_LEDGER_RECORDS, jsonl.MAX_RECORD_BYTES,
                    self._source_contract(), _original_hold_cuts, _imported_hold_origins,
                    ArchiveProofResolver._hold_origin,
                    ArchiveProofResolver.hold_origin, verified_hold_proofs,
                    _VerifiedHoldProofs.validates, _VerifiedHoldProofs.original,
                    _VerifiedHoldProofs.origins.fget)
        key = (home, node, worker, frame, contract)
        if key in self._origins:
            return self._origins[key]
        if key in self._resolving:
            raise _failure("imported hold proof contains a cycle")
        proof_key = (self.root.tenant_home, self.root.tenant_id, key, lineage, contract)
        if self._planning is None and proof_key in _ORIGIN_CACHE:
            result = deepcopy(_ORIGIN_CACHE[proof_key])
            self._origins[key] = result
            return result
        self._resolving.add(key)
        try:
            paths = self.archives()
            start = 0 if home == self.root.tenant_home else paths.index(home) + 1
            for archive in paths[start:]:
                if self._planning is not None:
                    self._planning.step()
                source = self._source(archive, node, worker)
                if self._planning is not None:
                    self._planning.source(source)
                events, ep, deliveries, dp, acknowledgments, ap = source
                for ordinal, old in enumerate(deliveries):
                    if old.get("id") != receipt.get("id"):
                        continue
                    if encode_frame(old) != frame:
                        raise _failure("imported hold ID has conflicting archived testimony")
                    cuts = _original_hold_cuts(source, old, ordinal)
                    if cuts is None:
                        continue  # This archive contains another carried copy.
                    before, _after = cuts
                    event_count, ack_count = len(before[0]), len(before[4])
                    self._project_archived_cut(source, (event_count, ordinal + 1, ack_count),
                        archive, node, worker)
                    original = self._project_archived_cut(source, (event_count, ordinal, ack_count),
                        archive, node, worker)
                    result = frame, before, original
                    self._origins[key] = result
                    if self._planning is None:
                        _ORIGIN_CACHE[proof_key] = deepcopy(result)
                        _ORIGIN_CACHE.move_to_end(proof_key)
                        while len(_ORIGIN_CACHE) > 32:
                            _ORIGIN_CACHE.popitem(last=False)
                    return result
            raise _failure("imported hold lacks its exact verified native archive cut")
        finally:
            self._resolving.remove(key)


def verified_hold_proofs(root, source, node, worker=None, *, home=None, resolver=None):
    """Issue sealed admission only for holds absent from this physical prefix."""
    home = root.tenant_home if home is None else home
    events = source[0]
    resolver = resolver or ArchiveProofResolver(root, head=events[0] if events else None)
    if resolver._planning is not None:
        resolver._planning.source(source)
    with resolver._verification_scope():
        lookup = resolver._hold_origin if resolver._planning is None else resolver.hold_origin
        origins = _imported_hold_origins(source, node, worker, root.tenant_id,
            lambda row: lookup(row, node, worker, home))
        if not origins:
            return None
        return _VerifiedHoldProofs(_binding(source, node, worker, root.tenant_id), origins, _PROOF_SEAL)


def verify_hold_proof_source(proofs, source, node, worker, tenant):
    if type(proofs) is not _VerifiedHoldProofs:
        raise _failure("imported hold proof is not native resolver testimony")
    proofs.assert_source(source, node, worker, tenant)


def original_carried_hold(root, receipt, node, worker=None):
    """Replay an explicit old-key retry, never the Stop waiter's arm path.

    Every predecessor comes from the immediate receipt-owned outgoing archive.
    New physical carry identities do not replace the original decision identity
    in an already durable wake attempt or its idempotent public artifact.
    """
    from .bus_epoch import _epoch_receipt_archive_path, _require_owned_epoch_guard
    from .wake_hold import original_wake_selection
    _require_owned_epoch_guard(root, exclusive=False)
    resolver = ArchiveProofResolver(root)
    fields = ('timestamp', 'recipient', 'worker_session_id', 'idempotency_key', 'limit',
        'item_ids', 'decision_digest')
    with resolver._verification_scope():
        source = resolver._source(root.tenant_home, node, worker)
        matches = [row for row in source[2] if row.get('id') == receipt.get('id')]
        if len(matches) != 1 or encode_frame(matches[0]) != encode_frame(receipt):
            raise _failure('carried retry does not name one exact live hold')
        current, home = matches[0], root.tenant_home
        if current.get('schema_version') != 3:
            raise _failure('carried retry requires a roll-minted hold')
        paths = resolver.archives()
        for archive in paths:
            counts = tuple(len(source[axis]) for axis in (0, 2, 4))
            resolver._project_archived_cut(source, counts, home, node, worker)
            if (not source[0] or source[0][0].get('kind') != 'bus_epoch_roll_receipt'
                    or _epoch_receipt_archive_path(root, source[0][0]) != archive):
                raise _failure('carried retry predecessor is outside its immediate owned archive')
            predecessor_source = resolver._source(archive, node, worker)
            predecessors = [row for row in predecessor_source[2]
                if row.get('id') == current['carried_from']['hold_id']]
            if len(predecessors) != 1 or predecessors[0].get('kind') != 'wake_hold_receipt':
                raise _failure('carried retry lacks its exact retained predecessor hold')
            predecessor = predecessors[0]
            if (any(current.get(field) != predecessor.get(field) for field in fields)
                    or ('selection_reason' in current) != ('selection_reason' in predecessor)
                    or current.get('selection_reason') != predecessor.get('selection_reason')):
                raise _failure('carried retry predecessor changed its original decision semantics')
            if predecessor.get('schema_version') == 3:
                current, source, home = predecessor, predecessor_source, archive
                continue
            # The predecessor may itself be a legacy byte-exact imported copy.
            # Existing sealed resolution finds and replays its true original cut.
            _frame, before, states = resolver.hold_origin(predecessor, node, worker, home)
            fresh, _held = original_wake_selection(states, predecessor)
            if [item.message['id'] for item in fresh[:predecessor['limit']]] != predecessor['item_ids']:
                raise _failure('carried retry does not reproduce its original semantic decision')
            return deepcopy(predecessor), before, states
        raise _failure('carried retry original decision history is no longer retained')


@dataclass(frozen=True)
class CarryPlan:
    live_payloads: tuple
    retained_ids: frozenset = frozenset()


def reanchor_carried_holds(payloads, receipt):
    """Roll-only attestation of admitted holds on the successor's actual axes.

    The planner has proved the outgoing selection. Its decision digest remains
    testimony about that decision; prefixes now name the new physical cut.
    Each carry has a new identity and names its immediate predecessor.
    """
    from .ids import uuid7_hex
    from .records import is_known_record_kind
    from .wake_hold import WakeHoldController
    by_name = {relative: raw for relative, _plane, raw in payloads}
    event_state = VerifiedLedgerCursor._initial_digest(WakeHoldController._EVENT_DOMAIN)
    event_raw = by_name['events.jsonl']
    # Future event frames remain byte-exact in the epoch, but native prefix
    # axes contain only recognized rows, as in _decode and VerifiedLedgerCursor.
    for frame, row in zip(event_raw.splitlines(keepends=True), decode_frames(event_raw)):
        if is_known_record_kind(row['kind']):
            event_state.update(frame)
    event_digest = event_state.hexdigest()
    result = []
    for relative, plane, raw in payloads:
        if plane != 'deliveries':
            result.append((relative, plane, raw))
            continue
        # The successor is authored events, then deliveries, then ACKs. A full
        # retained ACK plane could depend on a delivery after this hold.
        ack_digest = VerifiedLedgerCursor._initial_digest(WakeHoldController._ACK_DOMAIN).hexdigest()
        delivery_digest = VerifiedLedgerCursor._initial_digest(WakeHoldController._DELIVERY_DOMAIN)
        frames = []
        for frame, row in zip(raw.splitlines(keepends=True), decode_frames(raw)):
            if row['kind'] == 'wake_hold_receipt':
                row = dict(row, schema_version=3, id='wake-hold-' + uuid7_hex(),
                    event_prefix_digest=event_digest,
                    delivery_prefix_digest=delivery_digest.hexdigest(),
                    acknowledgment_prefix_digest=ack_digest,
                    carried_from={'hold_id': row['id'], 'roll_receipt_id': receipt['id']})
                row.pop('inbound_hold_mask', None)
                row = validate_record(row, receipt['tenant_id'], {'wake_hold_receipt'}, integrity=False)
                frame = encode_frame(row)
            frames.append(frame)
            delivery_digest.update(frame)
        result.append((relative, plane, b''.join(frames)))
    return tuple(result)


def build_carry_plan(root, old_payloads):
    """Retain pending native work and whole, byte-exact dependency closure.

    The transaction supplies selected bytes while owning EX, before acquiring
    WAKE or selected ledger locks. This planner does not write a root.
    """
    from .bus_epoch import _require_owned_epoch_guard
    from .cursor import SparseCursor
    from .events import EVENT_KINDS, validate_event_records
    from .wake_hold import WakeHoldController, project_wake_items
    from .cursor_presentations import CursorPresentationLedger
    _require_owned_epoch_guard(root, exclusive=True)
    payloads, decoded = {}, {}
    for relative, plane, raw in old_payloads:
        coordinate = Path(relative)
        expected_plane = "events" if relative == "events.jsonl" else (
            coordinate.parts[1] if len(coordinate.parts) in (3, 4)
            and coordinate.parts[0] == "receipts" and coordinate.parts[1] in {"deliveries", "acks"}
            and coordinate.suffix == ".jsonl" else None)
        if expected_plane != plane or relative in payloads or coordinate.is_absolute() or ".." in coordinate.parts:
            raise _failure("carry source has an invalid or duplicate selected coordinate")
        payloads[relative] = raw
        kinds = EVENT_KINDS if plane == "events" else WAKE_HOLD_KINDS if plane == "deliveries" else {"ack_receipt"}
        domain = {"events": WakeHoldController._EVENT_DOMAIN, "deliveries": WakeHoldController._DELIVERY_DOMAIN,
                  "acks": WakeHoldController._ACK_DOMAIN}[plane]
        decoded[relative] = _decode(root, relative, raw, kinds, domain, events=plane == "events")
    if "events.jsonl" not in payloads:
        raise _failure("carry source omits its mandatory events coordinate")
    events = decoded["events.jsonl"][0]
    validate_event_records(events)
    event_by_id = {row["id"]: row for row in events}
    envelopes = {row["id"]: row for row in events if row["kind"] == "message_envelope"}
    # Read unselected native metadata under EX, before any selected WAKE/LEDGER
    # phase. The references must join the frozen live source itself; archives
    # cannot supply a missing active-owner dependency.
    from .adhoc_journal import active_dispatch_references
    admitted = set()
    for reference in active_dispatch_references(root):
        envelope = envelopes.get(reference["envelope_id"])
        if envelope is None or envelope.get("worker_session_id") is not None or any(
            envelope[field] != reference[expected] for field, expected in (
                ("sender", "coordinator"), ("recipient", "node_id"), ("repo", "repository"),
                ("sha", "sha"), ("doc", "doc"))):
            raise _failure("active ad-hoc admission does not join its exact frozen live envelope")
        admitted.add(reference["envelope_id"])
        for message_id in reference.get("frozen_envelope_ids", []):
            frozen = envelopes.get(message_id)
            if (frozen is None or frozen["recipient"] != reference["node_id"]
                    or frozen.get("worker_session_id") is not None):
                raise _failure("active ad-hoc startup batch does not join its frozen node envelope")
            admitted.add(message_id)
    resolver = ArchiveProofResolver(root, frozen_payloads=old_payloads,
                                    head=events[0] if events else None)
    pairs = {(row["recipient"], row.get("worker_session_id")) for row in envelopes.values()}
    pending = set()
    for node, worker in sorted(pairs, key=lambda pair: (pair[0], pair[1] or "")):
        source = resolver.source(root.tenant_home, node, worker)
        proofs = verified_hold_proofs(root, source, node, worker, resolver=resolver)
        states = project_wake_items(events=source[0], deliveries=source[2], acknowledgments=source[4],
            recipient=node, worker_session_id=worker, tenant_id=root.tenant_id,
            event_prefix_digests=source[1], delivery_prefix_digests=source[3],
            acknowledgment_prefix_digests=source[5], _hold_proofs=proofs)
        pending.update(item.message["id"] for item in states if item.state in {"fresh", "held"})
    # Every nonempty selected namespace must reproduce an actual event/session
    # pair. An empty stale file is harmless and need not be carried.
    allowed_paths = {"events.jsonl"}
    cursor = SparseCursor(root)
    for node, worker in pairs:
        allowed_paths.add(cursor._relative_path_for(node, worker_session_id=worker).as_posix())
        allowed_paths.add(cursor._delivery_relative_path_for(node, worker_session_id=worker).as_posix())
    if any(raw and relative not in allowed_paths for relative, raw in payloads.items()):
        raise _failure("selected receipt namespace lacks its native event/session source")
    groups = []
    presentation_home = root.tenant_home / "receipts/cursor-presentations"
    if presentation_home.exists():
        if presentation_home.is_symlink() or not presentation_home.is_dir():
            raise _failure("presentation namespace is not ordinary")
        plane = CursorPresentationLedger(root, _epoch_resolver=resolver)
        for path in sorted(presentation_home.glob("*.jsonl")):
            if path.is_symlink() or not path.is_file():
                raise _failure("presentation source is not ordinary")
            node = path.stem
            groups.extend(plane._load(node, materialize=False)[3].values())
    retained = set(pending) | admitted
    resolved_asks = {
        row["reply_to"] for row in events
        if row["kind"] in {"ask_answer", "ask_decline"} and row.get("reply_to")
    }
    retained.update(
        row["id"] for row in events
        if row["kind"] == "ask_question" and row["id"] not in resolved_asks
    )
    latest_execution = {}
    for row in events:
        if row["kind"] == "seat_execution":
            latest_execution[row["invocation_id"]] = row
    open_invocations = {key for key, row in latest_execution.items() if row["event"] == "running"}
    retained.update(row["id"] for row in events if row["kind"] == "seat_execution"
                    and row["invocation_id"] in open_invocations)
    receipts = [(relative, row) for relative, (rows, _prefixes, _physical) in decoded.items()
                if relative != "events.jsonl" for row in rows]
    changed = True
    while changed:
        before = len(retained)
        for batch in groups:
            if retained.intersection(batch[0]["item_ids"]):
                retained.update(batch[0]["item_ids"])
        for _relative, row in receipts:
            if retained.intersection(row["item_ids"]):
                retained.update(row["item_ids"])
                retained.add(row["id"])
        for row in events:
            kind = row["kind"]
            if kind == "message_envelope" and row["id"] in retained and row.get("reply_to"):
                retained.add(row["reply_to"])
            elif kind == "message_retracted" and row["retracted_message_id"] in retained:
                retained.add(row["id"])
            elif kind in {"delivery_claim", "send_readiness_observation", "native_dispatch_launcher_record"}:
                reference_field = {"delivery_claim": "note_ref",
                                   "send_readiness_observation": "envelope_id",
                                   "native_dispatch_launcher_record": "message_id"}[kind]
                dependency = row[reference_field]
                if dependency in retained:
                    retained.add(row["id"])
            elif kind in {"ask_presentation", "ask_answer", "ask_decline", "ask_timeout"} and row.get("reply_to") in retained:
                retained.add(row["id"])
            elif kind == "seat_execution":
                if row["id"] in retained or row.get("envelope_id") in retained:
                    retained.add(row["id"])
                    if row.get("envelope_id") is not None:
                        retained.add(row["envelope_id"])
                    # An execution close must retain its native invocation
                    # continuity when its start is present in this epoch.
                    retained.update(old["id"] for old in events if old["kind"] == "seat_execution"
                                    and old["invocation_id"] == row["invocation_id"])
        changed = before != len(retained)
    if any(identity.startswith("msg-") and identity not in envelopes for identity in retained):
        raise _failure("carry dependency references an absent original envelope")
    result = []
    for relative, plane, _raw in sorted(old_payloads):
        rows, _prefixes, physical = decoded[relative]
        frames = [frame for frame, row in physical if row["kind"] not in {
                    "bus_epoch_roll_receipt", "seat_teardown_epoch_verification", "epoch_carry_verification"}
                  and (row["id"] in retained or row["kind"] not in EVENT_KINDS and plane == "events")]
        raw = b"".join(frames)
        if raw or relative == "events.jsonl":
            result.append((relative, plane, raw))
    # Replay exactly the reduced native contracts, with archived hold admission
    # issued against the old source. No terminal sibling may become fresh.
    return CarryPlan(tuple(result), frozenset(identity for identity in retained if identity in envelopes))
