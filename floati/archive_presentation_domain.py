"""Closed bounded presentation DATA; source/namespace authenticity is not issued."""
import hashlib
import json
import re

from .archive_presentation_replay import replay_presentation_history as _replay
from .archive_projection_replay import PoolEpochView
from .archive_source_pool import read_pool, MAX_CHUNK_BYTES as POOL_CHUNK_BYTES
from .archive_survivor_sources import _domain_digest
from .cursor_presentations import KIND
from .errors import FloatiError, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES
from .root import validate_identifier

MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 128
MAX_REFERENCES = 1000000
MAX_WORK = 1000000
MAX_DEPTH = 64
_COMMON = {"schema_version", "domain", "tenant_id", "tag"}
_FIELDS = {
    "scope": {"nodes"},
    "node": {"node_id", "witness", "physical_byte_length", "physical_sha256",
             "records", "consents", "claims", "uncertain"},
    "group": {"node_id", "ordinal", "records", "consent", "claim", "source_cut", "following_delivery", "result"},
}
_NODE_FIELDS = {"recipient", "witness", "physical_byte_length", "physical_sha256", "records", "groups"}
_WITNESS = {"schema_version", "tenant_id", "node_id", "genesis_id", "before_bytes", "before_digest", "reserved_record"}
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _invalid(detail):
    return IntegrityFailure("archive_presentation_domain_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_presentation_domain_unsupported", detail,
        remedy="retain original presentation payload; this DATA representation is unsupported")


class _Budget:
    def __init__(self):
        self.work, self.refs = 0, 0

    def charge(self, work=0, refs=0):
        from .archive_survivor_sources import _query_charge
        _query_charge(work)
        if self.work + work > MAX_WORK or self.refs + refs > MAX_REFERENCES:
            raise _unsupported("aggregate presentation reference/work bound exceeded")
        self.work += work
        self.refs += refs

    def preflight(self, value):
        # The shared helper bounds expanded JSON, repeated shared objects,
        # scalars and cycles before serialization/copy. Then charge aggregate
        # traversal using depth-bounded iterators, never queued sibling tuples.
        _domain_digest("presentation_domain_preflight", value)
        frames = [[iter((value,)), 1, 0]]
        def children(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    yield key
                    yield child
            else:
                yield from item
        while frames:
            iterator, remaining, depth = frames[-1]
            if remaining == 0:
                frames.pop()
                continue
            if depth > MAX_DEPTH:
                raise _unsupported("presentation nesting bound exceeded")
            self.charge(work=1)
            frames[-1][1] -= 1
            try:
                item = next(iterator)
            except StopIteration as exc:
                raise _invalid("presentation input changed during preflight") from exc
            if isinstance(item, (dict, list, tuple)):
                width = len(item) * (2 if isinstance(item, dict) else 1)
                if width > MAX_WORK - self.work:
                    raise _unsupported("presentation traversal exceeds remaining support")
                frames.append([children(item), width, depth + 1])


def _canonical(value, budget):
    budget.preflight(value)
    # Preflight bounds each scalar; iterencode avoids allocating a complete
    # container before its much narrower entry allowance has been enforced.
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    chunks, size = [], 1  # reserve the native final newline
    for token in encoder.iterencode(value):
        raw = token.encode("utf-8")
        size += len(raw)
        if size > MAX_CHUNK_BYTES:
            raise _unsupported("one presentation entry exceeds 8 MiB")
        chunks.append(raw)
    return b"".join(chunks) + b"\n"


def _chunks(supplied, limit):
    result = []
    for ordinal, chunk in enumerate(supplied):
        if ordinal >= MAX_CHUNKS or type(chunk) is bytes and len(chunk) > limit:
            raise _unsupported("presentation/source chunk bound exceeded")
        if type(chunk) is not bytes or not chunk:
            raise _invalid("chunks must be nonempty bytes")
        result.append(chunk)
    return tuple(result)


def _sources(pool_chunks, tenant_id, node_count, budget):
    chunks = _chunks(pool_chunks, POOL_CHUNK_BYTES)
    # This bound needs no decoded membership. Refuse before the initial pool
    # parser or any detached axis/epoch copying, as well as later native views.
    if (node_count + 2) * sum(map(len, chunks)) > MAX_CHUNKS * MAX_CHUNK_BYTES:
        raise _unsupported("aggregate native presentation replay byte work exceeded")
    pool = read_pool(chunks, tenant_id)
    refs = 0
    for key in pool.by_tag("axis"):
        axis_refs = 1 + len(pool.entry(key)["frames"])
        refs += axis_refs
        budget.charge(refs=axis_refs)
    for key in pool.by_tag("epoch"):
        epoch = pool.entry(key)
        budget.charge(refs=sum(epoch[field] is not None for field in ("owner", "anchor", "predecessor")))
    budget.charge(refs=4 * len(pool.by_tag("cut")))
    # Each native node replay creates a fresh logical view. Charge all those
    # visits/bytes BEFORE any view/replay or eager source-result copying.
    cost = len(pool.by_tag("frame")) + len(pool.by_tag("epoch")) + len(pool.by_tag("cut")) + refs
    budget.charge(work=(node_count + 2) * cost)
    records = {key: pool.entry(key)["record"] for key in pool.by_tag("frame")}
    keys = {row["id"]: key for key, row in records.items()}
    census = {}
    for key, row in records.items():
        if row["kind"] == KIND:
            census.setdefault(row["node_id"], set()).add(key)
    return chunks, pool, records, keys, census


def _scope(nodes, census):
    if not isinstance(nodes, list):
        raise _invalid("presentation scope must be an ordered node array")
    for node in nodes:
        validate_identifier(node, "node")
    if nodes != sorted(set(nodes)) or not set(census) <= set(nodes):
        raise _invalid("presentation scope is duplicate, unordered or omits a framed node")


def _physical(rows, witness, length, digest, *, encoding):
    if (type(length) is not int or not 0 <= length <= MAX_LEDGER_BYTES
            or not isinstance(digest, str) or _HEX.fullmatch(digest) is None):
        raise _invalid("presentation physical facts are malformed")
    if len(rows) > MAX_LEDGER_RECORDS:
        raise _unsupported("presentation effective row count exceeds native support")
    frames, size = [], 0
    for row in rows:
        frame = encode_frame(row)
        size += len(frame)
        if len(frame) > MAX_RECORD_BYTES or size > MAX_LEDGER_BYTES:
            raise _unsupported("presentation canonical physical reconstruction exceeds native support")
        frames.append(frame)
    count = len(frames)
    full_hash = hashlib.sha256()
    for frame in frames:
        full_hash.update(frame)
    if size == length and full_hash.hexdigest() == digest:
        return b"".join(frames)
    reserved = witness["reserved_record"]
    if (reserved is not None and reserved["phase"] == "prepared" and rows
            and frames[-1] == encode_frame(reserved)):
        before_hash = hashlib.sha256()
        for frame in frames[:-1]:
            before_hash.update(frame)
        if size - len(frames[-1]) == length and before_hash.hexdigest() == digest:
            return b"".join(frames[:-1])
    if encoding:
        raise _unsupported("collected physical bytes cannot be reproduced exactly from canonical testimony")
    raise _invalid("serialized physical facts differ from exact canonical reconstruction")


def _refs(rows, records, keys, budget):
    result = []
    for row in rows:
        budget.charge(refs=1)
        key = keys[row["id"]]
        if encode_frame(row) != encode_frame(records[key]):
            raise _invalid("presentation row differs from its original pool frame")
        result.append(key)
    return result


def _node(node, witness, length, digest, row_refs, consent_refs, claim_refs, sources, budget, *, encoding):
    chunks, pool, records, keys, census = sources
    validate_identifier(node, "node")
    if not isinstance(witness, dict) or set(witness) != _WITNESS:
        raise _invalid("presentation witness fields are open or missing")
    if len(set(row_refs)) != len(row_refs) or len(set(consent_refs)) != len(consent_refs) or len(set(claim_refs)) != len(claim_refs):
        raise _invalid("presentation node repeats a frame reference")
    rows = [records[key] for key in row_refs]
    reserved = witness["reserved_record"]
    covered = set(row_refs)
    if reserved is not None:
        covered.update(_refs([reserved], records, keys, budget))
    if covered != census.get(node, set()) or any(row["kind"] != KIND or row["node_id"] != node for row in rows):
        raise _invalid("presentation node does not cover its exact retained frame census")
    if any(records[key]["kind"] != kind for refs, kind in (
            (consent_refs, "codex_wait_consent_receipt"), (claim_refs, "codex_wait_session_receipt")) for key in refs):
        raise _invalid("presentation authority reference has the wrong native family")
    if not covered and (rows or reserved is not None or length != 0 or digest != hashlib.sha256(b"").hexdigest()):
        raise _invalid("declared empty presentation scope has nonempty testimony")
    physical = _physical(rows, witness, length, digest, encoding=encoding)
    replay = _replay(chunks, tenant_id=witness["tenant_id"], node_id=node, witness=witness,
        physical_bytes=physical, consents=[records[key] for key in consent_refs], claims=[records[key] for key in claim_refs])
    if encode_frame(replay["records"]) != encode_frame(rows):
        raise _invalid("presentation effective rows differ from native witness replay")
    return {"recipient": node, "witness": witness, "physical_byte_length": length,
            "physical_sha256": digest, "replay": replay}


def _entries(tenant_id, nodes, sources, budget):
    chunks, pool, records, keys, census = sources
    def entry(tag, **fields):
        return dict(schema_version=1, domain="presentation", tenant_id=tenant_id, tag=tag, **fields)
    yield entry("scope", nodes=[node["recipient"] for node in nodes])
    for node in nodes:
        replay = node["replay"]
        witness = dict(node["witness"])
        reserved = witness["reserved_record"]
        witness["reserved_record"] = keys[reserved["id"]] if reserved is not None else None
        consents = {group["consent"]["id"]: group["consent"] for group in replay["groups"]}
        claims = {group["claim"]["id"]: group["claim"] for group in replay["groups"] if group["claim"] is not None}
        yield entry("node", node_id=node["recipient"], witness=witness,
            physical_byte_length=node["physical_byte_length"], physical_sha256=node["physical_sha256"],
            records=[keys[row["id"]] for row in replay["records"]],
            consents=[keys[row["id"]] for row in consents.values()],
            claims=[keys[row["id"]] for row in claims.values()], uncertain=replay["uncertain"])
        for ordinal, group in enumerate(replay["groups"]):
            yield entry("group", node_id=node["recipient"], ordinal=ordinal,
                records=[keys[row["id"]] for row in group["records"]],
                consent=keys[group["consent"]["id"]], claim=keys[group["claim"]["id"]] if group["claim"] is not None else None,
                source_cut=group["source_cut"], following_delivery=keys[group["following_delivery"]["id"]] if group["following_delivery"] is not None else None,
                result=group)


def _ref_count(row):
    if row["tag"] == "scope":
        return len(row["nodes"])
    if row["tag"] == "node":
        return sum(len(row[field]) for field in ("records", "consents", "claims")) + int(row["witness"]["reserved_record"] is not None)
    return len(row["records"]) + 2 + int(row["claim"] is not None) + int(row["following_delivery"] is not None)


def _pack(entries, budget):
    chunks, pending, total = [], bytearray(), 0
    for row in entries:
        budget.charge(refs=_ref_count(row))
        raw = _canonical(row, budget)
        total += len(raw)
        if total > MAX_CHUNKS * MAX_CHUNK_BYTES:
            raise _unsupported("aggregate presentation domain byte bound exceeded")
        if pending and len(pending) + len(raw) > MAX_CHUNK_BYTES:
            chunks.append(bytes(pending))
            pending.clear()
            if len(chunks) >= MAX_CHUNKS:
                raise _unsupported("too many presentation domain chunks")
        pending.extend(raw)
    if pending:
        chunks.append(bytes(pending))
    return tuple(chunks)


def _read(chunks, tenant_id, budget):
    rows = []
    for chunk in _chunks(chunks, MAX_CHUNK_BYTES):
        canonical = bytearray()
        from .archive_survivor_sources import _query_charge
        _query_charge(len(chunk))
        for row in decode_frames(chunk):
            if (not isinstance(row, dict) or not isinstance(row.get("tag"), str) or row["tag"] not in _FIELDS
                    or set(row) != _COMMON | _FIELDS[row["tag"]] or type(row["schema_version"]) is not int
                    or row["schema_version"] != 1 or row["domain"] != "presentation" or row["tenant_id"] != tenant_id):
                raise _invalid("presentation entry has foreign/open identity or fields")
            budget.charge(refs=_ref_count(row))
            canonical.extend(_canonical(row, budget))
            rows.append(row)
        if canonical != chunk:
            raise _invalid("presentation domain JSONL is not canonical")
    return rows


def _call(operation):
    try:
        return operation()
    except FloatiError as exc:
        if exc.code.startswith("archive_presentation_domain_"):
            raise
        if exc.code.endswith("unsupported"):
            raise _unsupported("native presentation support refused: " + exc.code) from exc
        raise _invalid("native presentation replay refused: " + exc.code) from exc
    except (FrameError, KeyError, TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise _invalid("malformed presentation domain data") from exc


def encode_presentation_domain(pool_chunks, tenant_id, native_presentation_plan):
    """Encode independently replayed native planning DATA, never its authority."""
    def encode():
        budget = _Budget()
        plan = native_presentation_plan
        budget.preflight(plan)
        if (not isinstance(plan, dict) or set(plan) != {"schema_version", "domain", "tenant_id", "entries"}
                or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
                or plan["domain"] != "presentation_sources" or plan["tenant_id"] != tenant_id or not isinstance(plan["entries"], list)):
            raise _invalid("presentation source plan has foreign/open fields")
        sources = _sources(pool_chunks, tenant_id, len(plan["entries"]), budget)
        chunks, pool, records, keys, census = sources
        _scope([node["recipient"] for node in plan["entries"]], census)
        view = PoolEpochView(chunks, tenant_id)
        full_sources, output = {}, []
        for supplied in plan["entries"]:
            if set(supplied) != _NODE_FIELDS:
                raise _invalid("native presentation node fields are open or missing")
            consents = {group["consent"]["id"]: group["consent"] for group in supplied["groups"]}
            claims = {group["claim"]["id"]: group["claim"] for group in supplied["groups"] if group["claim"] is not None}
            node = _node(supplied["recipient"], supplied["witness"], supplied["physical_byte_length"], supplied["physical_sha256"],
                _refs(supplied["records"], records, keys, budget), _refs(consents.values(), records, keys, budget),
                _refs(claims.values(), records, keys, budget), sources, budget, encoding=True)
            groups = node["replay"]["groups"]
            if len(supplied["groups"]) != len(groups):
                raise _invalid("native presentation plan has missing/extra groups")
            for saved, computed in zip(supplied["groups"], groups):
                if set(saved) != {"records", "source", "counts", "eligible_ids", "consent", "claim"}:
                    raise _invalid("native presentation group fields are open or missing")
                selected = {field: computed[field] for field in ("records", "counts", "eligible_ids", "consent", "claim")}
                if encode_frame(selected) != encode_frame({field: saved[field] for field in selected}):
                    raise _invalid("native presentation group differs from independent replay")
                cut = pool.entry(computed["source_cut"])
                coordinate = (cut["epoch"], supplied["recipient"])
                if coordinate not in full_sources:
                    full_sources[coordinate] = view.source(cut["epoch"], supplied["recipient"], None)
                if _domain_digest("presentation_full_source", saved["source"]) != _domain_digest("presentation_full_source", full_sources[coordinate]):
                    raise _invalid("native presentation source differs from exact pool epoch axes")
            output.append(node)
        return _pack(_entries(tenant_id, output, sources, budget), budget)
    return _call(encode)


def verify_presentation_domain(pool_chunks, tenant_id, domain_chunks):
    """Recompute closed frame/cut/physical testimony and return unprivileged DATA."""
    def verify():
        budget = _Budget()
        rows = _read(domain_chunks, tenant_id, budget)
        if not rows or rows[0]["tag"] != "scope" or sum(row["tag"] == "scope" for row in rows) != 1:
            raise _invalid("presentation domain lacks its one explicit scope header")
        scopes = rows[0]["nodes"]
        sources = _sources(pool_chunks, tenant_id, len(scopes), budget)
        chunks, pool, records, keys, census = sources
        _scope(scopes, census)
        declared = [row for row in rows if row["tag"] == "node"]
        if [row["node_id"] for row in declared] != scopes:
            raise _invalid("presentation node entries differ from the complete declared scope")
        output = []
        for node in declared:
            witness = dict(node["witness"])
            if witness["reserved_record"] is not None:
                witness["reserved_record"] = records[witness["reserved_record"]]
            output.append(_node(node["node_id"], witness, node["physical_byte_length"], node["physical_sha256"],
                node["records"], node["consents"], node["claims"], sources, budget, encoding=False))
        expected = iter(_entries(tenant_id, output, sources, budget))
        for row in rows:
            wanted = next(expected, None)
            if wanted is None or encode_frame(row) != encode_frame(wanted):
                raise _invalid("presentation entry is duplicate, extra, reordered or differs from recomputation")
        if next(expected, None) is not None:
            raise _invalid("presentation domain omits required group results")
        return {"schema_version": 1, "domain": "presentation_domain", "tenant_id": tenant_id, "entries": output}
    return _call(verify)
