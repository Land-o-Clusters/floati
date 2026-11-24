"""Closed survivor-query bundle DATA; no native authority or issuer.

Declared coverage and namespace witness cannot be authenticated from cold bytes.
A native issuer must compare them to a fresh complete plan/namespace. Replaying
this bundle never grants imported-hold admission, authoring or payload removal.
"""
import hashlib
import json
import re
from pathlib import PurePosixPath

from .archive_projection_replay import replay_projection_pool
from .archive_source_pool import read_pool
from .archive_survivor_pool import encode_survivor_source_pool
from .archive_survivor_sources import _domain_digest
from .archive_consumption_domain import encode_consumption_domain, verify_consumption_domain
from .archive_execution_domain import encode_execution_domain, verify_execution_domain
from .archive_presentation_domain import encode_presentation_domain, verify_presentation_domain
from .errors import FloatiError, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames
from .root import validate_identifier

MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 128
MAX_BYTES = 1024 * 1024 * 1024
MAX_REFERENCES = 1000000
MAX_WORK = 1000000
MAX_DEPTH = 64
LEAF_DOMAINS = ("consumption", "execution", "presentation")
_DOMAINS = ("consumption", "execution", "presentation", "projection", "roll_retry")
_HEADER = {"schema_version", "domain", "tenant_id", "leaf_domains", "source_chunks",
           "leaf_chunks", "projection_sha256", "coverage", "source_witness"}
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _invalid(detail):
    return IntegrityFailure("archive_survivor_bundle_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_survivor_bundle_unsupported", detail,
        remedy="retain original epoch payload; survivor query bundle exceeds supported bounds")


class _Budget:
    def __init__(self):
        self.work = self.references = self.bytes = 0

    def charge(self, *, work=0, references=0, byte_count=0):
        from .archive_survivor_sources import _query_charge
        _query_charge(work)
        if (self.work + work > MAX_WORK or self.references + references > MAX_REFERENCES
                or self.bytes + byte_count > MAX_BYTES):
            raise _unsupported("aggregate survivor bundle work/reference/byte bound exceeded")
        self.work += work
        self.references += references
        self.bytes += byte_count

    def scan(self, value):
        # Depth-bounded iterator stack, with work reserved before advancing.
        # Native SendReceipt is a reviewed dict subtype; arbitrary objects are
        # not admitted by the shared native canonical coverage helper.
        frames, active = [[iter((value,)), 1, None, 0]], set()
        def children(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise _invalid("bundle object key is not text")
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
                raise _unsupported("survivor bundle nesting bound exceeded")
            self.charge(work=1)
            frames[-1][1] -= 1
            try:
                item = next(iterator)
            except StopIteration as exc:
                raise _invalid("bundle container changed during preflight") from exc
            if isinstance(item, (dict, list, tuple)):
                if id(item) in active:
                    raise _invalid("cyclic survivor bundle DATA")
                width = len(item) * (2 if isinstance(item, dict) else 1)
                if width > MAX_WORK - self.work:
                    raise _unsupported("bundle traversal exceeds remaining work")
                self.charge(byte_count=2 + max(0, len(item) - 1) + (len(item) if isinstance(item, dict) else 0))
                active.add(id(item))
                frames.append([children(item), width, id(item), depth + 1])
            elif item is None or type(item) in (str, int, bool, float):
                if isinstance(item, str) and len(item) > MAX_CHUNK_BYTES:
                    raise _unsupported("bundle scalar exceeds chunk support")
                if type(item) is int and item.bit_length() > MAX_CHUNK_BYTES * 4:
                    raise _unsupported("bundle number exceeds chunk support")
                try:
                    raw = json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
                except (TypeError, ValueError, UnicodeError, OverflowError) as exc:
                    raise _invalid("bundle scalar is not canonical JSON") from exc
                self.charge(byte_count=len(raw))
            else:
                raise _invalid("bundle input is not finite JSON DATA")


def _canonical(value, budget):
    before = budget.bytes
    budget.scan(value)
    if budget.bytes - before + 1 > MAX_CHUNK_BYTES:
        raise _unsupported("survivor manifest exceeds 8 MiB before encoding")
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    parts, size = [], 1
    for token in encoder.iterencode(value):
        raw = token.encode("utf-8")
        size += len(raw)
        if size > MAX_CHUNK_BYTES:
            raise _unsupported("survivor manifest exceeds 8 MiB")
        parts.append(raw)
    return b"".join(parts) + b"\n"


def _chunks(supplied, budget):
    if not isinstance(supplied, (list, tuple)):
        raise _invalid("bundle/source chunks must be finite arrays")
    if not supplied:
        raise _invalid("bundle/source chunk collection is empty")
    if len(supplied) > MAX_CHUNKS:
        raise _unsupported("survivor bundle/source exceeds 128 chunks")
    for raw in supplied:
        if type(raw) is not bytes or not raw:
            raise _invalid("bundle/source chunks must be nonempty bytes")
        if len(raw) > MAX_CHUNK_BYTES:
            raise _unsupported("survivor bundle/source chunk exceeds 8 MiB")
        budget.charge(byte_count=len(raw))
    return tuple(supplied)


def _descriptors(chunks, domain=None):
    return [{**({"domain": domain} if domain is not None else {}), "ordinal": ordinal,
             "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for ordinal, raw in enumerate(chunks)]


def _source_preflight(source, tenant, budget, repetitions):
    # Available without parsing: reserve known repeated raw-byte/record work
    # before the first pool reader or native replay allocates logical copies.
    budget.charge(byte_count=repetitions * sum(map(len, source)),
                  work=repetitions * sum(raw.count(b"\n") for raw in source))
    pool = read_pool(source, tenant)
    refs = 4 * len(pool.by_tag("cut"))
    for key in pool.by_tag("axis"):
        axis = pool.entry(key)
        refs += 1 + len(axis["frames"])
    for key in pool.by_tag("epoch"):
        header = pool.entry(key)
        refs += sum(header[field] is not None for field in ("owner", "anchor", "predecessor"))
    budget.charge(references=repetitions * refs, work=repetitions * refs)
    before_work, before_bytes = budget.work, budget.bytes
    for key in pool.by_tag("frame"):
        budget.scan(pool.entry(key)["record"])
    budget.charge(work=(repetitions - 1) * (budget.work - before_work),
                  byte_count=(repetitions - 1) * (budget.bytes - before_bytes))
    return pool


def _metadata(coverage, witness):
    if not isinstance(coverage, list) or [row.get("domain") for row in coverage if isinstance(row, dict)] != list(_DOMAINS):
        raise _invalid("bundle coverage must declare exactly five ordered domains")
    for row in coverage:
        if (not isinstance(row, dict) or set(row) != {"domain", "scope_count", "sha256"}
                or type(row["scope_count"]) is not int or row["scope_count"] < 0
                or not isinstance(row["sha256"], str) or _HEX.fullmatch(row["sha256"]) is None):
            raise _invalid("malformed declared coverage row")
    if not isinstance(witness, list):
        raise _invalid("source witness must be a finite declared array")
    paths = []
    for row in witness:
        if not isinstance(row, dict) or set(row) != {"path", "identity"} or not isinstance(row["path"], str):
            raise _invalid("source witness has open or malformed fields")
        path = PurePosixPath(row["path"])
        if path.is_absolute() or ".." in path.parts or path.as_posix() != row["path"]:
            raise _invalid("source witness path is not a canonical relative label")
        identity = row["identity"]
        if not isinstance(identity, list) or len(identity) != 7 or any(type(value) is not int or value < 0 for value in identity):
            raise _invalid("source witness lacks seven native integer identity fields")
        paths.append(row["path"])
    if paths != sorted(set(paths)):
        raise _invalid("source witness has duplicate or unordered labels")


def _coverage(plan):
    sources = plan["sources"]
    if not isinstance(sources, dict) or set(sources) != set(_DOMAINS):
        raise _invalid("native survivor plan omits or adds a domain")
    projection, presentation, execution, consumption, retry = (sources[name] for name in
        ("projection", "presentation", "execution", "consumption", "roll_retry"))
    counts = {"projection": len(projection["epochs"]) + len(projection["cuts"]) + len(projection["holds"]),
        "presentation": len(presentation["entries"]) + sum(len(row["groups"]) for row in presentation["entries"]),
        "execution": len(execution["executions"]) + len(execution["worker_records"]) + len(execution["worker_joins"]),
        "consumption": len(consumption["receipts"]) + len(consumption["source_planes"]),
        "roll_retry": len(retry["entries"])}
    return [{"domain": name, "scope_count": counts[name], "sha256": _domain_digest(name, sources[name])}
            for name in _DOMAINS]


def _projection_binding(plan, pure):
    native = plan["sources"]["projection"]
    headers = [{key: row[key] for key in ("epoch_id", "automatic", "roll_receipt", "anchor")} for row in native["epochs"]]
    pairs = [{key: row[key] for key in ("epoch_id", "recipient", "worker_session_id")} for row in native["cuts"]]
    pure_pairs = [{key: row[key] for key in ("epoch_id", "recipient", "worker_session_id")} for row in pure["cuts"]]
    holds = [{"receipt": row["receipt"], "artifact": row["artifact"]} for row in native["holds"]]
    pure_holds = [{"receipt": row["receipt"], "artifact": row["artifact"]} for row in pure["holds"]]
    if _domain_digest("bundle_projection_binding", [headers, pairs, holds]) != _domain_digest(
            "bundle_projection_binding", [pure["epochs"], pure_pairs, pure_holds]):
        raise _invalid("native projection plan differs from independent pool replay")


def _header(tenant, sources, leaves, projection, coverage, witness):
    return {"schema_version": 1, "domain": "survivor_bundle", "tenant_id": tenant,
            "leaf_domains": list(LEAF_DOMAINS), "source_chunks": _descriptors(sources),
            "leaf_chunks": [row for name in LEAF_DOMAINS for row in _descriptors(leaves[name], name)],
            "projection_sha256": _domain_digest("bundle_projection", projection),
            "coverage": coverage, "source_witness": witness}


def _manifest(raw, tenant, budget):
    from .archive_survivor_sources import _query_charge
    _query_charge(len(raw))
    rows = decode_frames(raw)
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise _invalid("survivor bundle requires one manifest record")
    header = rows[0]
    if (set(header) != _HEADER or type(header["schema_version"]) is not int or header["schema_version"] != 1
            or header["domain"] != "survivor_bundle" or header["tenant_id"] != tenant
            or header["leaf_domains"] != list(LEAF_DOMAINS)
            or not isinstance(header["projection_sha256"], str) or _HEX.fullmatch(header["projection_sha256"]) is None):
        raise _invalid("survivor manifest is foreign or has open identity fields")
    _metadata(header["coverage"], header["source_witness"])
    for field in ("source_chunks", "leaf_chunks"):
        if not isinstance(header[field], list) or not header[field]:
            raise _invalid("survivor manifest descriptor collection is absent")
        for row in header[field]:
            fields = {"ordinal", "byte_length", "sha256"} | ({"domain"} if field == "leaf_chunks" else set())
            if (not isinstance(row, dict) or set(row) != fields or type(row["ordinal"]) is not int or row["ordinal"] < 0
                    or type(row["byte_length"]) is not int or not 1 <= row["byte_length"] <= MAX_CHUNK_BYTES
                    or not isinstance(row["sha256"], str) or _HEX.fullmatch(row["sha256"]) is None):
                raise _invalid("survivor manifest descriptor is malformed")
    budget.charge(references=len(header["source_chunks"]) + len(header["leaf_chunks"]))
    if _canonical(header, budget) != raw:
        raise _invalid("survivor manifest is not canonical JSONL")
    return header


def _call(operation):
    try:
        return operation()
    except FloatiError as exc:
        if exc.code.startswith("archive_survivor_bundle_"):
            raise
        if exc.code.endswith("unsupported"):
            raise _unsupported("native domain support refused: " + exc.code) from exc
        raise _invalid("native survivor replay refused: " + exc.code) from exc
    except (FrameError, KeyError, TypeError, ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        raise _invalid("malformed survivor bundle DATA") from exc


def encode_survivor_bundle(source_chunks, tenant_id, native_survivor_plan):
    def encode():
        validate_identifier(tenant_id, "tenant")
        budget = _Budget()
        source = _chunks(source_chunks, budget)
        plan = native_survivor_plan
        if (not isinstance(plan, dict) or set(plan) != {"schema_version", "domain", "tenant_id", "sources", "coverage", "frames", "source_witness"}
                or type(plan["schema_version"]) is not int or plan["schema_version"] != 1
                or plan["domain"] != "survivor_sources" or plan["tenant_id"] != tenant_id):
            raise _invalid("native survivor plan has foreign/open identity fields")
        budget.scan(plan)
        _metadata(plan["coverage"], plan["source_witness"])
        coverage = _coverage(plan)
        if coverage != plan["coverage"]:
            raise _invalid("native survivor coverage differs from recomputed counts/digests")
        _source_preflight(source, tenant_id, budget, 8 + len(plan["sources"]["presentation"]["entries"]))
        if encode_survivor_source_pool(plan) != source:
            raise _invalid("native survivor plan differs from the exact supplied source collection")
        projection = replay_projection_pool(source, tenant_id)
        _projection_binding(plan, projection)
        encoders = (encode_consumption_domain, encode_execution_domain, encode_presentation_domain)
        leaves, pending = {}, []
        for name, encoder in zip(LEAF_DOMAINS, encoders):
            raw = _chunks(encoder(source, tenant_id, plan["sources"][name]), budget)
            if 1 + len(pending) + len(raw) > MAX_CHUNKS:
                raise _unsupported("survivor bundle exceeds 128 total chunks")
            leaves[name] = raw
            pending.extend(raw)
        header = _header(tenant_id, source, leaves, projection, coverage, plan["source_witness"])
        budget.charge(references=len(header["source_chunks"]) + len(header["leaf_chunks"]))
        manifest = _canonical(header, budget)
        budget.charge(byte_count=len(manifest))
        return (manifest,) + tuple(pending)
    return _call(encode)


def verify_survivor_bundle(source_chunks, tenant_id, bundle_chunks):
    def verify():
        validate_identifier(tenant_id, "tenant")
        budget = _Budget()
        source, chunks = _chunks(source_chunks, budget), _chunks(bundle_chunks, budget)
        header = _manifest(chunks[0], tenant_id, budget)
        if header["source_chunks"] != _descriptors(source) or len(chunks) != 1 + len(header["leaf_chunks"]):
            raise _invalid("survivor bundle source/leaf byte collection differs from its manifest")
        leaves = {name: [] for name in LEAF_DOMAINS}
        previous = -1
        for descriptor, raw in zip(header["leaf_chunks"], chunks[1:]):
            name = descriptor["domain"]
            if name not in leaves or LEAF_DOMAINS.index(name) < previous:
                raise _invalid("leaf domains are unknown or noncontiguous/reordered")
            previous = LEAF_DOMAINS.index(name)
            expected = {"domain": name, "ordinal": len(leaves[name]), "byte_length": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest()}
            if descriptor != expected:
                raise _invalid("survivor leaf bytes/ordinal differ from the manifest")
            leaves[name].append(raw)
        if any(not leaves[name] for name in LEAF_DOMAINS):
            raise _invalid("survivor bundle omits an explicit leaf domain")
        # Count declared node entries before their native replayers parse/copy
        # source views; shared pool bytes/work are charged across all leaves.
        node_count = 0
        for raw in leaves["presentation"]:
            from .archive_survivor_sources import _query_charge
            _query_charge(len(raw))
            rows = decode_frames(raw)
            budget.scan(rows)
            node_count += sum(isinstance(row, dict) and row.get("tag") == "node" for row in rows)
        _source_preflight(source, tenant_id, budget, 7 + node_count)
        projection = replay_projection_pool(source, tenant_id)
        if _domain_digest("bundle_projection", projection) != header["projection_sha256"]:
            raise _invalid("survivor projection commitment differs from pure source replay")
        verifiers = (verify_consumption_domain, verify_execution_domain, verify_presentation_domain)
        result = {"schema_version": 1, "domain": "survivor_bundle", "tenant_id": tenant_id, "projection": projection}
        for name, verifier in zip(LEAF_DOMAINS, verifiers):
            result[name] = verifier(source, tenant_id, tuple(leaves[name]))
        budget.scan(result)
        result.update(declared_coverage=header["coverage"], declared_source_witness=header["source_witness"])
        return result
    return _call(verify)
