"""Read-only planning for ALL already-authored unselected ACTED receipts.

Historical validation follows complete receipt-owned lineage across manual
boundaries. This module never resolves/authorizes new ACT or issues durable proof.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re
import stat

from .bus_epoch import _require_owned_epoch_guard, _read_bound_regular, _selected_member_identity
from .consumption import ConsumptionLedger
from .epoch_carry import ArchiveProofResolver, carry_dependency_witness
from .errors import ProtocolRefusal
from .jsonl import MAX_LEDGER_BYTES
from .message_consumption import MessageConsumption
from .root import validate_identifier
from .snapshot import epoch_history

_NAMESPACE_BOUND = 100000
MAX_CAPTURED_BYTES = 128 * 8 * 1024 * 1024
MAX_CAPTURED_RECORDS = 1000000
MAX_DERIVED_REFERENCES = 1000000
MAX_DERIVED_BYTES = 128 * 8 * 1024 * 1024


def _charge_capture(raw, budget):
    size, count = len(raw), raw.count(b"\n")
    if budget[0] + size > MAX_CAPTURED_BYTES or budget[1] + count > MAX_CAPTURED_RECORDS:
        raise _blocked("consumption captured source byte/record budget exceeded")
    budget[0] += size
    budget[1] += count


def _blocked(detail):
    return ProtocolRefusal("archive_consumption_checkpoint_blocked", detail,
                           remedy="preserve archive payloads until complete native consumption closure is verifiable")


def _bound_consumed_bytes(parent, name, expected):
    """Read only the captured regular file through its admitted parent."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        def bound(info):
            return (_selected_member_identity(info) == _selected_member_identity(expected)
                    and stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        if not bound(os.fstat(descriptor)):
            raise _blocked("consumption member changed before its bound read")
        blocks, length = [], 0
        while True:
            block = os.read(descriptor, min(65536, MAX_LEDGER_BYTES - length + 1))
            if not block:
                break
            length += len(block)
            if length > MAX_LEDGER_BYTES:
                raise _blocked("consumption member exceeds the native ledger read bound")
            blocks.append(block)
        if (not bound(os.fstat(descriptor))
                or not bound(os.stat(name, dir_fd=parent, follow_symlinks=False))):
            raise _blocked("consumption member identity changed during its bound read")
        return b"".join(blocks)
    finally:
        os.close(descriptor)


def _consumed_sources(root, *, read=True, budget=None):
    """Bind every directory before listing, then capture exact native sources."""
    descriptors, sources, count = [], {}, 0
    def opened(descriptor, expected):
        if (not stat.S_ISDIR(expected.st_mode)
                or _selected_member_identity(os.fstat(descriptor)) != _selected_member_identity(expected)):
            raise _blocked("consumption directory identity changed before listing")
    def listing(descriptor, expected):
        nonlocal count
        opened(descriptor, expected)
        result = []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                count += 1
                if count > _NAMESPACE_BOUND:
                    raise _blocked("consumption namespace exceeds the supported membership bound")
                result.append((entry.name, entry.stat(follow_symlinks=False)))
        opened(descriptor, expected)
        return sorted(result)
    def ordinary(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _blocked("consumption namespace contains a nonordinary or aliased file")
    def member(node, relative, descriptor, parent_info, name, status):
        sources.setdefault(node, [])
        if read and name.endswith(".jsonl"):
            opened(descriptor, parent_info)
            if budget is not None and budget[0] + status.st_size > MAX_CAPTURED_BYTES:
                raise _blocked("consumption captured source byte budget exceeded before read")
            raw = _bound_consumed_bytes(descriptor, name, status)
            opened(descriptor, parent_info)
            if budget is not None:
                _charge_capture(raw, budget)
            sources[node].append((relative, raw))
    try:
        held = _require_owned_epoch_guard(root, exclusive=True)
        info = os.fstat(held.descriptor)
        descriptor = os.dup(held.descriptor)
        descriptors.append(descriptor)
        opened(descriptor, info)
        for component in ("receipts", "consumed"):
            try:
                info = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return {}
            if not stat.S_ISDIR(info.st_mode):
                raise _blocked("consumption ancestor is not an ordinary directory")
            descriptor = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=descriptor)
            descriptors.append(descriptor)
            opened(descriptor, info)
        base_info = info
        for name, info in listing(descriptor, base_info):
            if stat.S_ISDIR(info.st_mode):
                node = validate_identifier(name, "node")
                sources.setdefault(node, [])
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                descriptors.append(child)
                opened(child, info)
                for filename, status in listing(child, info):
                    ordinary(status)
                    if re.fullmatch(r"[0-9a-f]{64}\.jsonl(?:\.lock)?", filename) is None:
                        raise _blocked("unsupported member in worker consumption namespace")
                    member(node, Path("receipts/consumed") / node / filename,
                           child, info, filename, status)
            else:
                ordinary(info)
                suffix = ".jsonl.lock" if name.endswith(".jsonl.lock") else ".jsonl"
                if not name.endswith(suffix):
                    raise _blocked("unsupported member in consumption namespace")
                node = validate_identifier(name[:-len(suffix)], "node")
                member(node, Path("receipts/consumed") / name,
                       descriptor, base_info, name, info)
        return {node: tuple(sorted(rows, key=lambda row: row[0].as_posix()))
                for node, rows in sorted(sources.items())}
    except OSError as exc:
        raise _blocked("consumption namespace is unavailable, symlinked or changed") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def plan_consumption_checkpoint(root):
    """Return caller-owned planning data under existing EX; no issuance authority."""
    _require_owned_epoch_guard(root, exclusive=True)
    before = carry_dependency_witness(root)
    if before is None:
        raise _blocked("consumption dependency witness is unavailable or unsupported")
    archives = epoch_history(root)
    from .archive_consumption_replay import _ack_index
    from .framing import encode_frame
    captured = [0, 0]
    derived = [0, 0]
    planes, indices, frame_sizes, event_sizes = {}, {}, {}, {}
    resolver = None

    def charge_derived(references, size):
        if derived[0] + references > MAX_DERIVED_REFERENCES or derived[1] + size > MAX_DERIVED_BYTES:
            raise _blocked("consumption derived reference/expanded byte budget exceeded")
        derived[0] += references
        derived[1] += size

    def source_plane(node, message, events, prefix):
        nonlocal resolver
        worker = message.get("worker_session_id")
        key = (prefix.as_posix(), node, worker)
        if key in planes:
            return key
        if resolver is None:
            resolver = ArchiveProofResolver(root, frozen_payloads=(), head=frames[0] if frames else None)
        ack_relative = reader.cursor._relative_path_for(node, worker_session_id=worker)
        delivery_relative = reader.cursor._delivery_relative_path_for(node, worker_session_id=worker)
        if prefix == Path():
            for relative in (Path("events.jsonl"), delivery_relative, ack_relative):
                name = relative.as_posix()
                if name in resolver.frozen:
                    continue
                path = root.tenant_home / relative
                try:
                    status = path.lstat()
                except FileNotFoundError:
                    raw = b""
                else:
                    if captured[0] + status.st_size > MAX_CAPTURED_BYTES:
                        raise _blocked("consumption captured source byte budget exceeded before read")
                    raw = _read_bound_regular(path, status, code="archive_consumption_checkpoint_blocked",
                                             detail="live consumption source changed during bound read")
                _charge_capture(raw, captured)
                resolver.frozen[name] = raw
        source = resolver.source(root.tenant_home / prefix, node, worker)
        event_key = prefix.as_posix()
        if event_key not in event_sizes:
            if tuple(map(encode_frame, events)) != tuple(map(encode_frame, source[0])):
                raise _blocked("consumption event snapshot changed before ACK join")
            event_sizes[event_key] = sum(len(encode_frame(row)) for row in source[0])
        plane_size = event_sizes[event_key] + sum(len(encode_frame(row)) for axis in (2, 4) for row in source[axis])
        charge_derived(sum(len(source[axis]) for axis in (0, 2, 4)), plane_size)
        if prefix != Path():
            for axis in (0, 2, 4):
                for row in source[axis]:
                    _charge_capture(encode_frame(row), captured)
        for axis in (0, 2, 4):
            for row in source[axis]:
                # Budget actual captured objects, without assuming a later
                # global native-ID consistency check has already run.
                frame_sizes.setdefault(id(row), len(encode_frame(row)))
        # Bound index references and eventual expanded ACK testimony BEFORE
        # constructing recipient->ACK lists or cloning any joined output.
        for ack in source[4]:
            charge_derived(len(ack["item_ids"]), len(ack["item_ids"]) * frame_sizes[id(ack)])
        if prefix == Path():
            reader.cursor.state_for(node, [row for row in events if row["kind"] == "message_envelope"],
                                    worker_session_id=worker, snapshot=True)
        indices[key] = _ack_index(root.tenant_id, node, worker, source[0], source[2], source[4])
        planes[key] = {"source_prefix": prefix.as_posix(), "recipient": node, "worker_session_id": worker,
                       "events": source[0], "event_prefix_digests": source[1], "deliveries": source[2],
                       "delivery_prefix_digests": source[3], "acknowledgments": source[4],
                       "acknowledgment_prefix_digests": source[5], "events_path": (prefix / "events.jsonl").as_posix(),
                       "delivery_path": (prefix / delivery_relative).as_posix(), "ack_path": (prefix / ack_relative).as_posix()}
        return key

    class SnapshotReader(MessageConsumption):
        def _ack_reference(self, node, message, events, *, prefix=Path(), snapshot=False):
            key = source_plane(node, message, events, prefix)
            return indices[key].get(message["id"], [])

    reader = SnapshotReader(root)
    frames = reader._frames()
    sources = _consumed_sources(root, budget=captured)
    receipts = []
    for node, raw_sources in sources.items():
        # Validates ALL old receipts; its current-only result is deliberately not
        # used as a retirement census. No automatic-only lookup is invoked.
        reader._validated_history(node, frames, snapshot=True, raw_sources=raw_sources)
        receipts.extend(reader._raw(node, sources=raw_sources))

    work_records, work = [], {}
    if any(row["source"] == "work_complete" for row in receipts):
        ledger = ConsumptionLedger(root)
        work_records = ledger.records()
        for row in work_records:
            _charge_capture(encode_frame(row), captured)
        work = ledger.project(work_records)
    batches = [(Path(), frames)]
    batches.extend((archive.legacy_path.relative_to(root.tenant_home),
                    reader._frames(archive.legacy_path.relative_to(root.tenant_home)))
                   for archive in archives)
    first = {}
    for prefix, events in batches:
        for row in events:
            if row["kind"] == "message_envelope":
                first.setdefault(row["id"], (prefix, events, row))
    joins = []
    for receipt in receipts:
        source = first.get(receipt["message_id"])
        if source is None:
            raise _blocked("a previously validated consumption source disappeared")
        prefix, events, message = source
        node, worker = receipt["recipient"], receipt["worker_session_id"]
        key = source_plane(node, message, events, prefix)
        matching = reader._ack_reference(node, message, events, prefix=prefix, snapshot=True)
        ack = next((row for row in matching
                    if row["id"] == receipt["ack_receipt_id"]), None)
        if ack is None:
            raise _blocked("a previously validated exact ACK source disappeared")
        work_item = work.get(receipt["work_item_id"]) if receipt["source"] == "work_complete" else None
        charge_derived(len(matching) + 3 + int(work_item is not None),
                       sum(frame_sizes[id(row)] for row in matching) + len(encode_frame(receipt))
                       + len(encode_frame(message)) + (len(encode_frame(work_item)) if work_item is not None else 0))
        joins.append({"receipt_id": receipt["id"], "source_prefix": prefix.as_posix(),
                      "envelope": message, "ack_receipt": ack,
                      "matching_ack_receipts": matching,
                      "work_item": work_item})
    lineage = [archive.receipt for archive in archives]
    _consumed_sources(root, read=False)
    _require_owned_epoch_guard(root, exclusive=True)
    after = carry_dependency_witness(root)
    if after is None or after != before:
        raise _blocked("consumption source or dependency membership/identity changed during planning")
    return deepcopy({"state": "planned", "domain": "consumption", "tenant_id": root.tenant_id,
                     "receipts": receipts, "joins": joins, "source_planes": list(planes.values()),
                     "work_records": work_records, "lineage": lineage,
                     "source_witness": [{"path": name, "identity": list(identity)}
                                        for name, identity in before]})
