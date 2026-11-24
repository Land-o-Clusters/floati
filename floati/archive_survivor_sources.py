"""Complete native source-plan composition; returned data is not authority."""

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import hashlib
import json

from .archive_catalog import _canonical
from .errors import IntegrityFailure, ProtocolRefusal
from .framing import encode_frame
from .records import validate_record
from .events import SendReceipt


# Bounds apply to expanded JSON, including repeated shared native objects.
MAX_DOMAIN_CANONICAL_BYTES = 1024 * 1024 * 1024
MAX_DOMAIN_EXPANDED_NODES = 1000000
MAX_DOMAIN_DEPTH = 64


class _QueryWorkExhausted(Exception):
    """Private sticky support signal; foreign error wrappers must not recast it."""


class _QueryAllowance:
    def __init__(self, limit):
        self.remaining = limit
        self.owner = threading.get_ident()
        self.exhausted = False

    def require(self, amount):
        if self.owner != threading.get_ident():
            raise _QueryWorkExhausted("query allowance belongs to another thread")
        if type(amount) is not int or amount < 0 or self.exhausted or amount > self.remaining:
            self.exhausted = True
            raise _QueryWorkExhausted("combined query work support exhausted")

    def charge(self, amount):
        self.require(amount)
        self.remaining -= amount


_QUERY_ALLOWANCE = ContextVar("floati_private_query_allowance", default=None)


@contextmanager
def _query_work_scope(limit):
    """Private accounting only: never a source, prepared proof or admission."""
    current = _QUERY_ALLOWANCE.get()
    if current is not None:
        current.require(0)
        try:
            yield current
        finally:
            current.require(0)
        return
    state = _QueryAllowance(limit)
    token = _QUERY_ALLOWANCE.set(state)
    try:
        yield state
        state.require(0)
    finally:
        _QUERY_ALLOWANCE.reset(token)


def _query_charge(amount):
    state = _QUERY_ALLOWANCE.get()
    if state is not None:
        state.charge(amount)


def _query_require(amount):
    state = _QUERY_ALLOWANCE.get()
    if state is not None:
        state.require(amount)


def _query_copy_charge(value):
    """Charge expanded nodes before a copy, including repeated shared values."""
    if _QUERY_ALLOWANCE.get() is not None:
        _domain_digest("query_copy", value)


def _failure(detail):
    return IntegrityFailure("archive_survivor_sources_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_survivor_sources_unsupported", detail,
        remedy="retain the original epoch payload; do not retire this unsupported dependency")


def _domain_digest(name, value, *, _max_bytes=None, _max_nodes=None, _on_measure=None):
    """Bound expanded work before streaming the unchanged v1 canonical bytes.

    Sharing reduces preflight work, but its expanded cost is charged at every
    occurrence. A small shared graph therefore cannot authorize enormous JSON.
    No whole-container str/bytes allocation is used for coverage hashing.
    """
    from .jsonl import MAX_RECORD_BYTES
    memo, active = {}, set()
    byte_limit = MAX_DOMAIN_CANONICAL_BYTES if _max_bytes is None else min(_max_bytes, MAX_DOMAIN_CANONICAL_BYTES)
    node_limit = MAX_DOMAIN_EXPANDED_NODES if _max_nodes is None else min(_max_nodes, MAX_DOMAIN_EXPANDED_NODES)
    def within(size, nodes):
        if size > byte_limit or nodes > node_limit:
            raise _unsupported("native domain coverage exceeds expanded byte/work support bounds")
    def scalar(member):
        if type(member) not in (type(None), bool, int, float, str):
            raise _unsupported("native domain contains non-closed JSON data")
        if isinstance(member, str) and len(member) > MAX_RECORD_BYTES:
            raise _unsupported("native domain scalar exceeds its framing support bound")
        if type(member) is int and member.bit_length() > MAX_RECORD_BYTES * 4:
            raise _unsupported("native domain numeric scalar exceeds its framing support bound")
        try:
            raw = _canonical(member)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _unsupported("native domain contains unsupported JSON data") from exc
        if len(raw) > MAX_RECORD_BYTES:
            raise _unsupported("native domain scalar exceeds its encoded framing support bound")
        return len(raw)
    def measure(member, depth=0):
        _query_charge(1)
        if depth > MAX_DOMAIN_DEPTH:
            raise _unsupported("native domain coverage exceeds its nesting support bound")
        # Native execution notices retain this exact compatibility dict type.
        # Its inherited items are the same closed JSON keys encoded by json.
        if type(member) not in (dict, SendReceipt, list, tuple):
            size = scalar(member)
            within(size, 1)
            return size, 1, 0
        identity = id(member)
        if identity in active:
            raise _failure("native domain coverage contains a cycle")
        if identity in memo:
            if depth + memo[identity][2] > MAX_DOMAIN_DEPTH:
                raise _unsupported("native domain coverage exceeds its nesting support bound")
            _query_charge(memo[identity][1] - 1)
            return memo[identity]
        size, nodes, height = 2, 1, 0
        within(size, nodes)
        if len(member) > node_limit:
            raise _unsupported("native domain container exceeds its expanded work support bound")
        _query_require(len(member) * (2 if type(member) in (dict, SendReceipt) else 1))
        active.add(identity)
        try:
            if type(member) in (dict, SendReceipt):
                if any(type(key) is not str for key in member):
                    raise _failure("native domain JSON object has a non-string key")
                children = ((key, member[key]) for key in sorted(member))
            else:
                children = ((None, child) for child in member)
            for ordinal, (key, child) in enumerate(children):
                if ordinal:
                    size += 1
                if key is not None:
                    _query_charge(1)
                    size += scalar(key) + 1
                    nodes += 1
                child_size, child_nodes, child_height = measure(child, depth + 1)
                height = max(height, child_height + 1)
                size += child_size
                nodes += child_nodes
                within(size, nodes)
            memo[identity] = size, nodes, height
            return size, nodes, height
        finally:
            active.remove(identity)
    expected, _nodes, _height = measure(value)
    if _on_measure is not None:
        _on_measure(expected, _nodes)
    digest = hashlib.sha256(b"floati-native-survivor-domain-v1\0" + name.encode("ascii") + b"\0")
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    observed = 0
    try:
        for token in encoder.iterencode(value):
            raw = token.encode("utf-8")
            observed += len(raw)
            if observed > expected:
                raise _failure("native domain changed after bounded coverage preflight")
            digest.update(raw)
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise _failure("native domain changed or cannot reproduce its canonical coverage") from exc
    if observed != expected:
        raise _failure("native domain changed after bounded coverage preflight")
    return digest.hexdigest()


def collect_survivor_sources(root):
    """Collect every native domain and recheck one common dependency view.

    Coverage is derived here from native namespace collection, including explicit
    empty domains. It is planning data: neither a caller-edited dictionary nor
    its digest grants semantic admission or durable history authoring authority.
    The eventual native issuer must invoke and revalidate this collection itself.
    """
    from . import archive_checkpoint
    from .archive_consumption_checkpoint import plan_consumption_checkpoint
    from .archive_execution_checkpoint import plan_execution_checkpoint
    from .bus_epoch import _require_owned_epoch_guard
    from .epoch_carry import carry_dependency_witness
    _require_owned_epoch_guard(root, exclusive=True)
    before = carry_dependency_witness(root)
    if before is None:
        raise _failure("complete native dependency namespace cannot be captured")
    projection = archive_checkpoint.collect_projection_sources(root)
    presentation = archive_checkpoint.collect_presentation_sources(root)
    execution = plan_execution_checkpoint(root)
    consumption = plan_consumption_checkpoint(root)
    retry = archive_checkpoint.collect_roll_retry_sources(root)
    sources = {"projection": projection, "presentation": presentation,
               "execution": execution, "consumption": consumption, "roll_retry": retry}
    # Bound expanded slot traversal before validating/encoding repeated frames.
    digests = {name: _domain_digest(name, source) for name, source in sources.items()}
    frames = {}

    def remember(rows):
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("kind"), str):
                raise _failure("native domain returned an invalid record source")
            checked = validate_record(dict(row), root.tenant_id,
                                      frozenset({row["kind"]}), integrity=True)
            frame = encode_frame(checked)
            if encode_frame(row) != frame:
                raise _failure("native source frame differs from its canonical contract")
            identity = checked["id"]
            if identity in frames and frames[identity][0] != frame:
                raise _failure("one native ID has conflicting testimony across survivor domains")
            frames[identity] = (frame, checked)

    def source_axes(source):
        for axis in (0, 2, 4):
            remember(source[axis])

    # Enumerate only declared native slots. Arbitrary record-shaped dictionaries
    # inside source bindings/payloads are data and never become native records.
    for epoch in projection["epochs"]:
        remember(epoch["event_source"][0])
        if epoch["roll_receipt"] is not None:
            remember([epoch["roll_receipt"]])
    for cut in projection["cuts"]:
        source_axes(cut["source"])
    for hold in projection["holds"]:
        remember([hold["receipt"]])
        source_axes(hold["before"])
    for entry in presentation["entries"]:
        remember(entry["records"])
        if entry["witness"]["reserved_record"] is not None:
            remember([entry["witness"]["reserved_record"]])
        for group in entry["groups"]:
            source_axes(group["source"])
            remember([group["consent"]])
            if group["claim"] is not None:
                remember([group["claim"]])
    remember(execution["executions"])
    remember(execution["notice_rows"])
    remember(execution["worker_records"])
    remember(consumption["receipts"])
    remember(consumption["work_records"])
    remember(consumption["lineage"])
    for plane in consumption["source_planes"]:
        remember(plane["events"])
        remember(plane["deliveries"])
        remember(plane["acknowledgments"])
    for entry in retry["entries"]:
        remember([entry["receipt"]])
    counts = {
        "projection": len(projection["epochs"]) + len(projection["cuts"]) + len(projection["holds"]),
        "presentation": len(presentation["entries"]) + sum(len(row["groups"]) for row in presentation["entries"]),
        "execution": len(execution["executions"]) + len(execution["worker_records"]) + len(execution["worker_joins"]),
        "consumption": len(consumption["receipts"]) + len(consumption["source_planes"]),
        "roll_retry": len(retry["entries"]),
    }
    coverage = [{"domain": name, "scope_count": counts[name],
                 "sha256": digests[name]}
                for name in sorted(sources)]
    _require_owned_epoch_guard(root, exclusive=True)
    if carry_dependency_witness(root) != before:
        raise _failure("native dependencies changed between survivor domain collections")
    return {"schema_version": 1, "domain": "survivor_sources",
            "tenant_id": root.tenant_id, "sources": deepcopy(sources),
            "coverage": coverage, "frames": [deepcopy(frames[key][1]) for key in sorted(frames)],
            "source_witness": [{"path": name, "identity": list(identity)} for name, identity in before]}
