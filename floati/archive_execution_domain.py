"""Closed bounded execution/PID replay DATA; no native admission or authority."""
import re

from .archive_projection_replay import PoolEpochView
from .archive_source_pool import read_pool, MAX_CHUNK_BYTES as POOL_CHUNK_BYTES
from .archive_execution_replay import (_execution_history, _execution_result,
    _project_worker_records, _worker_pid_join, worker_audit_pid)
from .errors import FloatiError, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .workers import WorkerReceipts

MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 128
MAX_REFERENCES = 1000000
MAX_WORK = 1000000
MAX_DEPTH = 64
MAX_ENTRIES = 1000000
WORKER_BATCH_SIZE = 10000
SEMANTIC_FIELDS = ("executions", "acceptance_keys", "replays", "notice_rows",
                   "worker_records", "worker_joins", "pid_receipts")
_COMMON = {"schema_version", "domain", "tenant_id", "tag"}
_FIELDS = {
    "event_batch": {"ordinal", "epoch", "axis", "frames"},
    "worker_batch": {"ordinal", "frames"},
    "replay": {"ordinal", "execution", "acceptance_key", "notice_frames", "result", "worker_join"},
    "pid": {"node_id", "pid", "executions"},
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _invalid(detail):
    return IntegrityFailure("archive_execution_domain_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_execution_domain_unsupported", detail,
        remedy="retain original epoch payload; execution domain exceeds supported bounds")


class _Budget:
    def __init__(self):
        self.references, self.work = 0, 0

    def charge(self, *, references=0, work=0):
        from .archive_survivor_sources import _query_charge
        _query_charge(work)
        if self.references + references > MAX_REFERENCES or self.work + work > MAX_WORK:
            raise _unsupported("aggregate execution reference/work bound exceeded")
        self.references += references
        self.work += work

    def scan(self, value):
        # One iterator per active ancestor, never a queue of all siblings.
        # A frame's exact remaining count lets us reserve each node BEFORE
        # advancing the iterator, without over-reading to discover overflow.
        frames, active, byte_floor = [[iter((value,)), 1, None, 0]], set(), 0
        def children(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise _invalid("execution JSON key is not text")
                    yield key
                    yield child
            else:
                yield from item
        while frames:
            iterator, remaining, owner, depth = frames[-1]
            if remaining == 0:
                frames.pop()
                if owner is not None:
                    active.remove(owner)
                continue
            if depth > MAX_DEPTH:
                raise _unsupported("execution data nesting bound exceeded")
            self.charge(work=1)
            frames[-1][1] -= 1
            try:
                item = next(iterator)
            except StopIteration as exc:
                raise _invalid("execution container changed during preflight") from exc
            if isinstance(item, (dict, list, tuple)):
                if id(item) in active:
                    raise _invalid("cyclic execution domain data")
                width = len(item) * (2 if isinstance(item, dict) else 1)
                if width > MAX_WORK - self.work:
                    raise _unsupported("execution data traversal bound exceeded")
                active.add(id(item))
                frames.append([children(item), width, id(item), depth + 1])
            elif isinstance(item, str):
                byte_floor += len(item)
                if byte_floor > MAX_CHUNK_BYTES:
                    raise _unsupported("one execution entry exceeds the chunk bound")
            elif item is not None and type(item) not in (int, bool, float):
                raise _invalid("execution value is not JSON data")


def _canonical(value, budget):
    budget.scan(value)
    try:
        raw = encode_frame(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("execution entry is not canonical JSON data") from exc
    if len(raw) > MAX_CHUNK_BYTES:
        raise _unsupported("one execution entry exceeds 8 MiB")
    return raw


def _chunks(chunks, limit):
    result, total = [], 0
    for ordinal, chunk in enumerate(chunks):
        if ordinal >= MAX_CHUNKS:
            raise _unsupported("too many execution/source chunks")
        if type(chunk) is not bytes or not chunk:
            raise _invalid("chunks must be nonempty bytes")
        if len(chunk) > limit:
            raise _unsupported("execution/source chunk exceeds its byte bound")
        total += len(chunk)
        if total > MAX_CHUNKS * limit:
            raise _unsupported("aggregate execution/source byte bound exceeded")
        result.append(chunk)
    return tuple(result)


def _sources(pool_chunks, tenant_id, budget):
    chunks = _chunks(pool_chunks, POOL_CHUNK_BYTES)
    pool = read_pool(chunks, tenant_id)
    # Charge membership and refs before PoolEpochView materializes/copies data.
    budget.charge(work=len(pool.by_tag("frame")) + len(pool.by_tag("epoch")))
    axes = {}
    for key in pool.by_tag("axis"):
        axis = pool.entry(key)
        budget.charge(references=1 + len(axis["frames"]), work=1 + len(axis["frames"]))
        if axis["plane"] == "events":
            axes[axis["epoch"]] = axis
    records = {key: pool.entry(key)["record"] for key in pool.by_tag("frame")}
    keys = {row["id"]: key for key, row in records.items()}
    worker_keys = {key for key, row in records.items() if row["kind"] == "worker_receipt"}
    budget.charge(references=len(worker_keys), work=len(records))
    view = PoolEpochView(chunks, tenant_id)
    headers = list(reversed(view.epochs()))
    return records, keys, worker_keys, [(header, axes[header["key"]]) for header in headers]


def _recompute(tenant_id, sources, worker_refs):
    records, keys, worker_keys, epochs = sources
    if len(worker_refs) != len(worker_keys) or set(worker_refs) != worker_keys:
        raise _invalid("worker transcript does not cover exactly all pool worker frames")
    workers = [records[key] for key in worker_refs]
    history = _execution_history([[records[key] for key in axis["frames"]] for header, axis in epochs])
    _project_worker_records(workers, WorkerReceipts._NEXT, WorkerReceipts._STATES)
    by_worker_id = {}
    for row in workers:
        if row["id"] in by_worker_id:
            raise _invalid("duplicate worker source identity")
        by_worker_id[row["id"]] = row
    executions = [row for row in history if row["kind"] == "seat_execution"]
    envelope_ids = {row["envelope_id"] for row in executions if row["envelope_id"] is not None}
    notices, by_envelope = [], {}
    for row in history:
        envelope = row["id"] if row["kind"] == "message_envelope" else (
            row["envelope_id"] if row["kind"] == "send_readiness_observation" else None)
        if envelope in envelope_ids:
            notices.append(row)
            by_envelope.setdefault(envelope, []).append(row)
    replays, joins, terminal = [], [], {}
    for row in executions:
        # Keep every matching readiness row ordered: the original first value
        # and native fallback survive, without a last-writer index overwrite.
        result = _execution_result(row, by_envelope.get(row["envelope_id"], []))
        replays.append({"execution_id": row["id"], "result": result})
        if row["source_kind"] == "worker" and row["event"] in {"ended", "unknown"}:
            source = by_worker_id.get(row["source_receipt_id"])
            joins.append({"execution_id": row["id"], "source_receipt_id": row["source_receipt_id"],
                          "source": source, "audit_pid": worker_audit_pid(row) if source is not None else None})
            terminal.setdefault(row["node_id"], []).append(row)
    pids = []
    # Validate globally before filtering. Filter by exact source ID, not node,
    # so contradictory foreign-node spawn evidence cannot disappear here.
    for node in sorted(terminal):
        rows = terminal[node]
        source_ids = {row["source_receipt_id"] for row in rows}
        source_rows = [by_worker_id[key] for key in source_ids if key in by_worker_id]
        joined = _worker_pid_join(rows, source_rows, node)
        pids.extend({"node_id": node, "pid": pid, "execution_ids": ids}
                    for pid, ids in sorted(joined.items()))
    return dict(tenant_id=tenant_id, executions=executions,
        acceptance_keys=[{"key": row["idempotency_key"], "execution_id": row["id"]} for row in executions],
        replays=replays, notice_rows=notices, worker_records=workers, worker_joins=joins, pid_receipts=pids)


def _entries(tenant_id, sources, worker_refs, result):
    records, keys, unused_workers, epochs = sources
    def entry(tag, **fields):
        return dict(schema_version=1, domain="execution", tenant_id=tenant_id, tag=tag, **fields)
    for ordinal, (header, axis) in enumerate(epochs):
        yield entry("event_batch", ordinal=ordinal, epoch=header["key"], axis=axis["key"], frames=axis["frames"])
    for ordinal, offset in enumerate(range(0, max(1, len(worker_refs)), WORKER_BATCH_SIZE)):
        yield entry("worker_batch", ordinal=ordinal, frames=worker_refs[offset:offset + WORKER_BATCH_SIZE])
    notices = {}
    for row in result["notice_rows"]:
        envelope = row["id"] if row["kind"] == "message_envelope" else row["envelope_id"]
        notices.setdefault(envelope, []).append(keys[row["id"]])
    joins = {row["execution_id"]: row for row in result["worker_joins"]}
    for ordinal, replay in enumerate(result["replays"]):
        row = replay["result"]["execution"]
        join = joins.get(row["id"])
        yield entry("replay", ordinal=ordinal, execution=keys[row["id"]],
            acceptance_key=row["idempotency_key"], notice_frames=notices.get(row["envelope_id"], []),
            result=replay["result"], worker_join=(None if join is None else {
                "source": keys[join["source"]["id"]] if join["source"] is not None else None,
                "audit_pid": join["audit_pid"]}))
    for group in result["pid_receipts"]:
        yield entry("pid", node_id=group["node_id"], pid=group["pid"],
                    executions=[keys[identity] for identity in group["execution_ids"]])


def _ref_count(entry):
    if entry["tag"] in {"event_batch", "worker_batch"}:
        return len(entry["frames"]) + (2 if entry["tag"] == "event_batch" else 0)
    if entry["tag"] == "replay":
        return 1 + len(entry["notice_frames"]) + int(entry["worker_join"] is not None and entry["worker_join"]["source"] is not None)
    return len(entry["executions"])


def _pack(entries, budget):
    chunks, pending, total = [], bytearray(), 0
    for ordinal, entry in enumerate(entries):
        if ordinal >= MAX_ENTRIES:
            raise _unsupported("too many execution domain entries")
        budget.charge(references=_ref_count(entry))
        raw = _canonical(entry, budget)
        total += len(raw)
        if total > MAX_CHUNKS * MAX_CHUNK_BYTES:
            raise _unsupported("aggregate execution domain byte bound exceeded")
        if pending and len(pending) + len(raw) > MAX_CHUNK_BYTES:
            chunks.append(bytes(pending))
            pending.clear()
            if len(chunks) >= MAX_CHUNKS:
                raise _unsupported("too many execution domain chunks")
        pending.extend(raw)
    if pending:
        chunks.append(bytes(pending))
    return tuple(chunks)


def _read_domain(chunks, tenant_id, budget):
    entries = []
    for chunk in _chunks(chunks, MAX_CHUNK_BYTES):
        canonical = bytearray()
        from .archive_survivor_sources import _query_charge
        _query_charge(len(chunk))
        for row in decode_frames(chunk):
            if len(entries) >= MAX_ENTRIES:
                raise _unsupported("too many execution domain entries")
            if not isinstance(row, dict) or not isinstance(row.get("tag"), str) or row["tag"] not in _FIELDS:
                raise _invalid("unknown execution domain entry")
            tag = row["tag"]
            if (set(row) != _COMMON | _FIELDS[tag] or type(row["schema_version"]) is not int
                    or row["schema_version"] != 1 or row["domain"] != "execution" or row["tenant_id"] != tenant_id):
                raise _invalid("execution domain has open fields or foreign version/domain/tenant")
            refs = row["frames"] if tag in {"event_batch", "worker_batch"} else (
                row["notice_frames"] if tag == "replay" else row["executions"])
            if not isinstance(refs, list) or any(not isinstance(key, str) or _HEX.fullmatch(key) is None for key in refs):
                raise _invalid("malformed ordered execution frame references")
            if tag in {"event_batch", "worker_batch", "replay"} and (
                    type(row["ordinal"]) is not int or row["ordinal"] < 0):
                raise _invalid("invalid execution entry ordinal")
            if tag == "replay":
                join = row["worker_join"]
                if not isinstance(row["result"], dict) or (join is not None and (
                        not isinstance(join, dict) or set(join) != {"source", "audit_pid"})):
                    raise _invalid("invalid execution result or worker join shape")
            budget.charge(references=_ref_count(row))
            canonical.extend(_canonical(row, budget))
            entries.append(row)
        if canonical != chunk:
            raise _invalid("execution domain JSONL is not canonical")
    return entries


def _semantic_call(operation):
    try:
        return operation()
    except FloatiError as exc:
        if exc.code in {"archive_execution_domain_invalid", "archive_execution_domain_unsupported"}:
            raise
        raise _invalid("native execution replay refused: " + exc.code) from exc
    except (FrameError, KeyError, TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise _invalid("malformed execution domain data") from exc


def encode_execution_domain(pool_chunks, tenant_id, native_execution_plan):
    """Independently replay a collected native plan; caller results prove nothing."""
    def encode():
        plan = native_execution_plan
        fields = set(SEMANTIC_FIELDS) | {"tenant_id", "state", "domain", "source_witness"}
        if (not isinstance(plan, dict) or set(plan) != fields or plan["tenant_id"] != tenant_id
                or plan["state"] != "planned" or plan["domain"] != "execution"):
            raise _invalid("native execution planning data has foreign/open fields")
        budget = _Budget()
        sources = _sources(pool_chunks, tenant_id, budget)
        records, keys, worker_keys, unused_epochs = sources
        workers = plan["worker_records"]
        if not isinstance(workers, list) or len(workers) != len(worker_keys):
            raise _invalid("collected workers do not cover the pool transcript")
        refs = []
        for row in workers:
            key = keys[row["id"]]
            if _canonical(row, budget) != encode_frame(records[key]):
                raise _invalid("collected worker frame differs from the pool")
            refs.append(key)
        result = _recompute(tenant_id, sources, refs)
        for field in SEMANTIC_FIELDS:
            supplied, expected = plan[field], result[field]
            if not isinstance(supplied, list) or len(supplied) != len(expected):
                raise _invalid("collected execution results have missing/extra scope: " + field)
            for left, right in zip(supplied, expected):
                if _canonical(left, budget) != encode_frame(right):
                    raise _invalid("collected execution result differs from independent replay: " + field)
        return _pack(_entries(tenant_id, sources, refs, result), budget)
    return _semantic_call(encode)


def verify_execution_domain(pool_chunks, tenant_id, domain_chunks):
    """Reconstruct complete closed domain refs and recompute native DATA only."""
    def verify():
        budget = _Budget()
        entries = _read_domain(domain_chunks, tenant_id, budget)
        sources = _sources(pool_chunks, tenant_id, budget)
        refs = [key for row in entries if row["tag"] == "worker_batch" for key in row["frames"]]
        result = _recompute(tenant_id, sources, refs)
        expected = iter(_entries(tenant_id, sources, refs, result))
        for row in entries:
            wanted = next(expected, None)
            if wanted is None or encode_frame(row) != encode_frame(wanted):
                raise _invalid("execution domain entry is extra, reordered or differs from native recomputation")
        if next(expected, None) is not None:
            raise _invalid("execution domain omits required native results or source scopes")
        return result
    return _semantic_call(verify)
