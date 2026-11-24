"""Closed consumption DATA; consistency never establishes native authenticity.

An issuer must establish complete pool/source truth separately. No filesystem,
publication, runtime admission, current claim or payload removal is provided.
"""
from copy import deepcopy
from pathlib import PurePosixPath

from .archive_projection_replay import PoolEpochView, _projection_replayer
from .archive_source_pool import read_pool
from .archive_consumption_replay import (_replay_history_data as replay_history,
    _ack_index, automatic_predecessor_open)
from .consumption import WORK_KINDS
from .cursor import SparseCursor
from .errors import FloatiError, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .root import validate_identifier

MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 128
MAX_REFERENCES = 1000000
MAX_WORK = 1000000
MAX_EXPANDED_BYTES = 128 * 8 * 1024 * 1024
MAX_DEPTH = 64
MAX_ENTRIES = 1000000
SEMANTIC_FIELDS = ("receipts", "joins", "source_planes", "work_records", "lineage")
WORK_BATCH_SIZE = 10000
_COMMON = {"schema_version", "domain", "tenant_id", "tag", "ordinal"}
_FIELDS = {
    "epoch": {"epoch", "owner", "anchor", "predecessor"},
    "plane": {"epoch", "recipient", "worker_session_id", "axes", "source_cut"},
    "work_batch": {"frames"},
    "acted": {"receipt", "source_epoch", "source_cut", "envelope", "ack_receipt",
              "matching_acks", "work_item"},
    "readiness": {"message", "source_epoch", "source_cut", "automatic_eligible",
                  "acknowledged", "matching_acks", "projection_state", "already_consumed"},
}


def _invalid(detail):
    return IntegrityFailure("archive_consumption_domain_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_consumption_domain_unsupported", detail,
        remedy="retain original epoch payload; consumption domain exceeds supported bounds")


class _Budget:
    def __init__(self):
        self.work = self.references = self.expanded_bytes = 0

    def charge(self, *, work=0, references=0, expanded_bytes=0):
        from .archive_survivor_sources import _query_charge
        _query_charge(work)
        if (self.work + work > MAX_WORK or self.references + references > MAX_REFERENCES
                or self.expanded_bytes + expanded_bytes > MAX_EXPANDED_BYTES):
            raise _unsupported("aggregate consumption work/reference/expanded byte bound exceeded")
        self.work += work
        self.references += references
        self.expanded_bytes += expanded_bytes

    def scan(self, value):
        # Reserve traversal BEFORE advancing siblings; retain one iterator per
        # active ancestor. Charge shared values at each expanded occurrence.
        stack, active = [[iter((value,)), 1, None, 0]], set()
        def children(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise _invalid("consumption JSON key is not text")
                    yield key
                    yield child
            else:
                yield from item
        while stack:
            iterator, remaining, owner, depth = stack[-1]
            if not remaining:
                stack.pop()
                if owner is not None:
                    active.remove(owner)
                continue
            if depth > MAX_DEPTH:
                raise _unsupported("consumption DATA exceeds its nesting bound")
            self.charge(work=1)
            stack[-1][1] -= 1
            try:
                item = next(iterator)
            except StopIteration as exc:
                raise _invalid("consumption container changed during preflight") from exc
            if isinstance(item, (dict, list, tuple)):
                if id(item) in active:
                    raise _invalid("cyclic consumption DATA")
                width = len(item) * (2 if isinstance(item, dict) else 1)
                if width > MAX_WORK - self.work:
                    raise _unsupported("consumption DATA exceeds its traversal bound")
                self.charge(expanded_bytes=2 + max(0, len(item) - 1) + (len(item) if isinstance(item, dict) else 0))
                active.add(id(item))
                stack.append([children(item), width, id(item), depth + 1])
            elif item is None or type(item) in (bool, int, float, str):
                if isinstance(item, str) and len(item) > MAX_CHUNK_BYTES:
                    raise _unsupported("consumption scalar exceeds a chunk")
                if type(item) is int and item.bit_length() > MAX_CHUNK_BYTES * 4:
                    raise _unsupported("consumption number exceeds a chunk")
                try:
                    raw = encode_frame(item)
                except (TypeError, ValueError, UnicodeError, OverflowError) as exc:
                    raise _invalid("noncanonical consumption scalar") from exc
                self.charge(expanded_bytes=len(raw) - 1)
            else:
                raise _invalid("consumption input is not finite JSON DATA")


def _canonical(value, budget):
    before = budget.expanded_bytes
    budget.scan(value)
    if budget.expanded_bytes - before + 1 > MAX_CHUNK_BYTES:
        raise _unsupported("one consumption domain entry exceeds 8 MiB before encoding")
    raw = encode_frame(value)
    if len(raw) > MAX_CHUNK_BYTES:
        raise _unsupported("one consumption domain entry exceeds 8 MiB")
    return raw


def _chunks(chunks, budget):
    if not isinstance(chunks, (list, tuple)):
        raise _invalid("consumption/source chunks must be finite arrays")
    if len(chunks) > MAX_CHUNKS:
        raise _unsupported("too many consumption/source chunks")
    total = 0
    for chunk in chunks:
        if type(chunk) is not bytes or not chunk:
            raise _invalid("consumption/source chunks must be nonempty bytes")
        if len(chunk) > MAX_CHUNK_BYTES:
            raise _unsupported("consumption/source chunk exceeds its byte bound")
        total += len(chunk)
        if total > MAX_CHUNKS * MAX_CHUNK_BYTES:
            raise _unsupported("aggregate consumption/source byte bound exceeded")
        budget.charge(expanded_bytes=len(chunk))
    return tuple(chunks)


def _sources(chunks, tenant, budget):
    validate_identifier(tenant, "tenant")
    chunks = _chunks(chunks, budget)
    pool = read_pool(chunks, tenant)
    frame_keys, epoch_keys, axis_keys = (pool.by_tag(tag) for tag in ("frame", "epoch", "axis"))
    budget.charge(work=len(frame_keys) + len(epoch_keys), references=len(frame_keys) + len(epoch_keys))
    axes = {}
    for key in axis_keys:
        axis = pool.entry(key)
        budget.charge(work=1 + len(axis["frames"]), references=1 + len(axis["frames"]))
        axes[(axis["epoch"], axis["plane"], axis["recipient"], axis["worker_session_id"])] = axis
    for key in epoch_keys:
        header = pool.entry(key)
        budget.charge(references=sum(header[field] is not None for field in ("owner", "anchor", "predecessor")))
    # Neutral cuts are indexed by the logical view even if no ACT names them.
    # Each has one epoch plus three axis references; charge before that view.
    cut_count = len(pool.by_tag("cut"))
    budget.charge(work=5 * cut_count, references=4 * cut_count)
    records = {key: pool.entry(key)["record"] for key in frame_keys}
    keys = {row["id"]: key for key, row in records.items()}
    sizes = {key: len(encode_frame(row)) for key, row in records.items()}
    work = {key for key, row in records.items() if row["kind"] in WORK_KINDS}
    acts = {key for key, row in records.items() if row["kind"] == "consumption_receipt"}
    contexts = sorted({(node, worker) for epoch, plane, node, worker in axes if plane != "events"},
                      key=lambda pair: (pair[0], pair[1] or ""))
    # Contextual kernels validate all epochs. Reserve repeated event/work
    # traversal and ACK join expansion BEFORE logical view copies or joins.
    event_rows = sum(len(axis["frames"]) for coordinate, axis in axes.items() if coordinate[1] == "events")
    event_bytes = sum(sum(sizes[key] for key in axis["frames"])
                      for coordinate, axis in axes.items() if coordinate[1] == "events")
    budget.charge(work=len(contexts) * (event_rows + len(epoch_keys) + len(work)),
                  references=len(contexts) * (event_rows + len(work)),
                  expanded_bytes=len(contexts) * (event_bytes + sum(sizes[key] for key in work)))
    for coordinate, axis in axes.items():
        if coordinate[1] == "acks":
            for key in axis["frames"]:
                count = len(records[key]["item_ids"])
                budget.charge(work=count, references=count, expanded_bytes=count * sizes[key])
    view = PoolEpochView(chunks, tenant)
    return view, records, keys, axes, work, acts, contexts


def _inventory(refs, expected, detail):
    if not isinstance(refs, list) or len(refs) != len(expected) or set(refs) != expected:
        raise _invalid(detail)


def _prefix(header, records):
    if header["owner"] is None:
        return PurePosixPath(".")
    # This is an original receipt label, never a Path lookup or ownership proof.
    return PurePosixPath(records[header["owner"]]["archive_path"]).name


def _plane(source, prefix, node, worker):
    prefix = PurePosixPath(prefix)
    component = node + ".jsonl" if worker is None else node + "/" + SparseCursor._session_component(worker) + ".jsonl"
    return {"source_prefix": prefix.as_posix(), "recipient": node, "worker_session_id": worker,
        **dict(zip(("events", "event_prefix_digests", "deliveries", "delivery_prefix_digests",
                    "acknowledgments", "acknowledgment_prefix_digests"), source)),
        "events_path": (prefix / "events.jsonl").as_posix(),
        "delivery_path": (prefix / "receipts/deliveries" / component).as_posix(),
        "ack_path": (prefix / "receipts/acks" / component).as_posix()}


def _recompute(tenant, sources, act_refs, work_refs, budget):
    view, records, keys, axes, work_set, act_set, contexts = sources
    _inventory(act_refs, act_set, "ACT transcript must cover exactly all pool consumption frames")
    _inventory(work_refs, work_set, "work transcript must cover exactly all pool work frames")
    headers = view._epochs
    work_rows, receipts = [records[key] for key in work_refs], [records[key] for key in act_refs]
    by_context = {}
    for receipt in receipts:
        by_context.setdefault((receipt["recipient"], receipt["worker_session_id"]), []).append(receipt)
    if any(context not in contexts for context in by_context):
        raise _invalid("ACT transcript names an absent source context")
    project, unused_original, unused_origin, unused_holds = _projection_replayer(view, tenant)
    planes, source_planes, joins, readiness, bound = [], [], {}, [], {}
    receipt_planes = {}
    for node, worker in contexts:
        epoch_sources = [view._source(header["key"], node, worker) for header in headers]
        # No speculative raw ACK checks substitute for actual native projection.
        first, indices, states = {}, {}, {}
        automatic = True
        automatic_indices = set()
        for index, (header, source) in enumerate(zip(headers, epoch_sources)):
            if index and not automatic_predecessor_open(epoch_sources[index - 1][0]):
                automatic = False
            if automatic:
                automatic_indices.add(index)
            coordinate = (header["key"], "deliveries", node, worker)
            if coordinate in axes:
                states[index] = {item.message["id"]: item.state for item in project(source, header["key"], node, worker)}
                indices[index] = _ack_index(tenant, node, worker, source[0], source[2], source[4])
                cut = view._cut_key(header["key"], node, worker, source)
                planes.append({"epoch": header["key"], "recipient": node,
                               "worker_session_id": worker, "source_cut": cut})
            for row in source[0]:
                if row["kind"] == "message_envelope" and row["recipient"] == node and row.get("worker_session_id") == worker:
                    first.setdefault(row["id"], (index, row))
        history = replay_history(tenant, node, worker, epoch_sources, by_context.get((node, worker), []), work_rows)
        for joined in history["joins"]:
            receipt, index = joined["receipt"], joined["source_index"]
            header, source = headers[index], epoch_sources[index]
            key = (index, node, worker)
            if key not in bound:
                bound[key] = _plane(source, _prefix(header, records), node, worker)
            receipt_planes[receipt["id"]] = key
            joins[receipt["id"]] = {"receipt_id": receipt["id"], "source_prefix": bound[key]["source_prefix"],
                "envelope": joined["envelope"], "ack_receipt": joined["ack_receipt"],
                "matching_ack_receipts": joined["matching_ack_receipts"], "work_item": joined["work_item"]}
        consumed_ids = {row["message_id"] for row in by_context.get((node, worker), [])}
        for identity, (index, message) in first.items():
            matching = indices.get(index, {}).get(identity, [])
            retracted = states.get(index, {}).get(identity) == "retracted"
            readiness.append({"message_id": identity, "recipient": node, "worker_session_id": worker,
                "source_epoch": headers[index]["key"],
                "source_cut": view._cut_key(headers[index]["key"], node, worker, epoch_sources[index]),
                "automatic_eligible": index in automatic_indices and not retracted,
                "acknowledged": bool(matching), "matching_ack_receipts": matching,
                "projection_state": states.get(index, {}).get(identity), "already_consumed": identity in consumed_ids})
    # Restore the exact declared native ACT transcript order, not context order.
    seen_planes = set()
    for receipt in receipts:
        key = receipt_planes[receipt["id"]]
        if key not in seen_planes:
            source_planes.append(key)
            seen_planes.add(key)
    result = {"tenant_id": tenant, "receipts": receipts, "joins": [joins[row["id"]] for row in receipts],
        "source_planes": [bound[key] for key in source_planes], "work_records": work_rows,
        "lineage": [records[header["owner"]] for header in headers if header["owner"] is not None],
        "planes": planes, "readiness": readiness}
    # Charge expanded outputs before a final shared-memo detached result.
    budget.scan(result)
    return result


def _entries(tenant, sources, result):
    view, records, keys, axes, unused_work, unused_acts, unused_contexts = sources
    def entry(tag, ordinal, **fields):
        return dict(schema_version=1, domain="consumption", tenant_id=tenant, tag=tag, ordinal=ordinal, **fields)
    for ordinal, header in enumerate(reversed(view._epochs)):
        yield entry("epoch", ordinal, epoch=header["key"], owner=header["owner"],
                    anchor=header["anchor"], predecessor=header["predecessor"])
    for ordinal, plane in enumerate(result["planes"]):
        epoch, node, worker = plane["epoch"], plane["recipient"], plane["worker_session_id"]
        yield entry("plane", ordinal, **plane, axes={name: axes[(epoch, name,
            None if name == "events" else node, None if name == "events" else worker)]["key"]
            for name in ("events", "deliveries", "acks")})
    work_refs = [keys[row["id"]] for row in result["work_records"]]
    for ordinal, offset in enumerate(range(0, max(1, len(work_refs)), WORK_BATCH_SIZE)):
        yield entry("work_batch", ordinal, frames=work_refs[offset:offset + WORK_BATCH_SIZE])
    ready = {row["message_id"]: row for row in result["readiness"]}
    for ordinal, (receipt, join) in enumerate(zip(result["receipts"], result["joins"])):
        source = ready[receipt["message_id"]]
        yield entry("acted", ordinal, receipt=keys[receipt["id"]], source_epoch=source["source_epoch"],
            source_cut=source["source_cut"], envelope=keys[join["envelope"]["id"]],
            ack_receipt=keys[join["ack_receipt"]["id"]],
            matching_acks=[keys[row["id"]] for row in join["matching_ack_receipts"]], work_item=join["work_item"])
    for ordinal, row in enumerate(result["readiness"]):
        yield entry("readiness", ordinal, message=keys[row["message_id"]],
            **{field: row[field] for field in ("source_epoch", "source_cut", "automatic_eligible",
                "acknowledged", "projection_state", "already_consumed")},
            matching_acks=[keys[ack["id"]] for ack in row["matching_ack_receipts"]])


def _ref_count(entry):
    tag = entry["tag"]
    if tag == "epoch":
        return 1 + sum(entry[name] is not None for name in ("owner", "anchor", "predecessor"))
    if tag == "plane":
        return 5
    if tag == "work_batch":
        return len(entry["frames"])
    return (5 if tag == "acted" else 3) + len(entry["matching_acks"])


def _pack(entries, budget):
    chunks, pending, total = [], bytearray(), 0
    for ordinal, entry in enumerate(entries):
        if ordinal >= MAX_ENTRIES:
            raise _unsupported("too many consumption domain entries")
        budget.charge(references=_ref_count(entry))
        raw = _canonical(entry, budget)
        total += len(raw)
        if total > MAX_CHUNKS * MAX_CHUNK_BYTES:
            raise _unsupported("aggregate consumption domain bytes exceeded")
        if pending and len(pending) + len(raw) > MAX_CHUNK_BYTES:
            chunks.append(bytes(pending))
            pending.clear()
            if len(chunks) >= MAX_CHUNKS:
                raise _unsupported("too many consumption domain chunks")
        pending.extend(raw)
    if pending:
        chunks.append(bytes(pending))
    return tuple(chunks)


def _read(chunks, tenant, budget):
    entries = []
    for chunk in _chunks(chunks, budget):
        canonical = bytearray()
        from .archive_survivor_sources import _query_charge
        _query_charge(len(chunk))
        for row in decode_frames(chunk):
            if len(entries) >= MAX_ENTRIES:
                raise _unsupported("too many consumption domain entries")
            if not isinstance(row, dict) or row.get("tag") not in _FIELDS:
                raise _invalid("unknown consumption domain entry")
            if (set(row) != _COMMON | _FIELDS[row["tag"]] or type(row["schema_version"]) is not int
                    or row["schema_version"] != 1 or row["domain"] != "consumption" or row["tenant_id"] != tenant
                    or type(row["ordinal"]) is not int or row["ordinal"] < 0):
                raise _invalid("open, foreign or malformed consumption domain entry")
            budget.charge(references=_ref_count(row))
            canonical.extend(_canonical(row, budget))
            entries.append(row)
        if canonical != chunk:
            raise _invalid("consumption domain is not canonical JSONL")
    return entries


def _call(operation):
    try:
        return operation()
    except FloatiError as exc:
        if exc.code in {"archive_consumption_domain_invalid", "archive_consumption_domain_unsupported"}:
            raise
        raise _invalid("native consumption/projection replay refused: " + exc.code) from exc
    except (FrameError, KeyError, TypeError, ValueError, RecursionError, UnicodeError, OverflowError) as exc:
        raise _invalid("malformed consumption domain DATA") from exc


def encode_consumption_domain(pool_chunks, tenant_id, native_consumption_plan):
    """Encode independently replayed, closed unprivileged consumption DATA."""
    def encode():
        plan = native_consumption_plan
        if (not isinstance(plan, dict) or set(plan) != set(SEMANTIC_FIELDS) | {"state", "domain", "tenant_id", "source_witness"}
                or plan["tenant_id"] != tenant_id or plan["state"] != "planned" or plan["domain"] != "consumption"
                or any(not isinstance(plan[field], list) for field in SEMANTIC_FIELDS)
                or not isinstance(plan["source_witness"], list)):
            raise _invalid("native consumption plan is foreign, open or nonfinite")
        budget = _Budget()
        budget.scan(plan)
        sources = _sources(pool_chunks, tenant_id, budget)
        records, keys = sources[1:3]
        def references(rows):
            refs = []
            for row in rows:
                key = keys[row["id"]]
                if _canonical(row, budget) != encode_frame(records[key]):
                    raise _invalid("native consumption frame differs from original pool bytes")
                refs.append(key)
            return refs
        result = _recompute(tenant_id, sources, references(plan["receipts"]), references(plan["work_records"]), budget)
        for field in SEMANTIC_FIELDS:
            supplied, expected = plan[field], result[field]
            if len(supplied) != len(expected):
                raise _invalid("native consumption plan omits or adds scope: " + field)
            for left, right in zip(supplied, expected):
                if _canonical(left, budget) != encode_frame(right):
                    raise _invalid("native consumption plan differs from independent replay: " + field)
        return _pack(_entries(tenant_id, sources, result), budget)
    return _call(encode)


def verify_consumption_domain(pool_chunks, tenant_id, domain_chunks):
    """Recompute historical joins and future source readiness; no admission."""
    def verify():
        budget = _Budget()
        entries = _read(domain_chunks, tenant_id, budget)
        sources = _sources(pool_chunks, tenant_id, budget)
        acts = [row["receipt"] for row in entries if row["tag"] == "acted"]
        work = [key for row in entries if row["tag"] == "work_batch" for key in row["frames"]]
        result = _recompute(tenant_id, sources, acts, work, budget)
        expected = iter(_entries(tenant_id, sources, result))
        for row in entries:
            wanted = next(expected, None)
            if wanted is None or encode_frame(row) != _canonical(wanted, budget):
                raise _invalid("consumption entry is extra, reordered or differs from recomputation")
        if next(expected, None) is not None:
            raise _invalid("consumption domain omits required source scope or result")
        from .archive_survivor_sources import _query_copy_charge
        _query_copy_charge(result)
        return deepcopy(result)
    return _call(verify)
