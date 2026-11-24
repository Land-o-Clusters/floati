"""Native history commitments and identity-bound chunk reads.

This layer verifies committed bytes. It does not issue survivor proof, grant
maintenance authority, authorize retirement, or fall back to a catalog pointer.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
from types import MappingProxyType
from weakref import WeakKeyDictionary

from .errors import IntegrityFailure, ProtocolRefusal
from .root import FloatiRoot

KIND = "epoch_history_commit"
LEDGER = "receipts/epoch-history-commits.jsonl"
CHUNK_DIRECTORY = "state/epoch-history/v1/chunks"
MAX_CHUNK_BYTES = 8 * 1024 * 1024
_FORMAT_CHUNK_BYTES = 8 * 1024 * 1024
_MAX_CHUNKS = 100000
_GENERATION_DOMAIN = b"floati-epoch-history-generation-v1\0"
_SOURCES_DOMAIN = b"floati-epoch-history-sources-v1\0"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_DOMAINS = frozenset({"catalog", "sources", "survivor"})
_TRIGGERS = frozenset({"send", "present", "drain", "retract", "ack", "stop_poll"})
_MAX_CAPTURE_COORDINATES = 1000000
_MAX_CAPTURE_DEPTH = 64
_READ_BLOCK = 64 * 1024
_CAPTURE_DATA = WeakKeyDictionary()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def generation_digest(record):
    """Bind the complete generation, excluding its physical receipt stamp."""
    value = {key: member for key, member in record.items()
             if key not in {"id", "timestamp", "kind", "schema_version", "generation"}}
    return hashlib.sha256(_GENERATION_DOMAIN + _canonical(value)).hexdigest()


def validate_commit_fields(record, refuse):
    """Validate this new family's fields; common fields belong to records."""
    from .records import _json_integer
    def invalid(detail):
        refuse("epoch_history_commit_invalid", detail,
               "preserve the native history ledger and reconcile the exact committed generation")

    def digest(value):
        return isinstance(value, str) and _DIGEST.fullmatch(value) is not None

    def integer(value, low, high):
        normalized = _json_integer(value)
        return normalized is not None and low <= normalized <= high

    if _json_integer(record["verifier_version"]) != 1:
        invalid("history verifier version must be integer one")
    if not isinstance(record["trigger"], str) or record["trigger"] not in _TRIGGERS:
        invalid("history maintenance trigger is unknown")
    policy = record["policy"]
    if (not isinstance(policy, dict) or set(policy) != {"schema_version", "keep_forever"}
            or _json_integer(policy["schema_version"]) != 1
            or type(policy["keep_forever"]) is not bool):
        invalid("history policy must be the closed version-one keep-forever decision")
    previous = record["previous_generation"]
    if (previous is not None and not digest(previous)) or not digest(record["generation"]):
        invalid("history generation identities must be exact SHA-256 values")
    anchor = record["live_anchor"]
    if (not isinstance(anchor, dict) or set(anchor) != {"byte_length", "sha256"}
            or not integer(anchor["byte_length"], 0, 2**63 - 1) or not digest(anchor["sha256"])):
        invalid("history generation must bind the exact live event bytes")
    source = record["source_commitment"]
    if (not isinstance(source, dict) or set(source) != {"chunk_count", "sha256"}
            or not integer(source["chunk_count"], 1, _MAX_CHUNKS) or not digest(source["sha256"])):
        invalid("history source commitment is malformed")
    chunks = record["chunks"]
    if not isinstance(chunks, list) or not 2 <= len(chunks) <= _MAX_CHUNKS:
        invalid("history chunks must include bounded catalog and source collections")
    next_ordinal = {}
    prior = None
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {"domain", "ordinal", "byte_length", "sha256"}:
            invalid("history chunk descriptor has an open or incomplete shape")
        domain = chunk["domain"]
        if (not isinstance(domain, str) or domain not in _DOMAINS
                or not integer(chunk["ordinal"], 0, _MAX_CHUNKS - 1)
                or not integer(chunk["byte_length"], 1, _FORMAT_CHUNK_BYTES)
                or not digest(chunk["sha256"])):
            invalid("history chunk descriptor is outside its format bounds")
        coordinate = (domain, chunk["ordinal"])
        if ((prior is not None and coordinate <= prior)
                or chunk["ordinal"] != next_ordinal.get(domain, 0)):
            invalid("history chunk coordinates must be unique, ordered and contiguous")
        prior = coordinate
        next_ordinal[domain] = chunk["ordinal"] + 1
    if ("catalog" not in next_ordinal or "sources" not in next_ordinal
            or source["chunk_count"] != next_ordinal["sources"]):
        invalid("history commitment omits its complete catalog or source collection")
    if record["generation"] != generation_digest(record):
        invalid("history generation digest differs from its complete commitment")


def _failure(detail):
    return IntegrityFailure("epoch_history_invalid", detail)


def _owned_path(root, relative):
    path = root.tenant_home / relative
    try:
        if root.resolve_relative(relative) != path:
            raise _failure("history source contains a path alias")
    except (ProtocolRefusal, OSError) as failure:
        raise _failure("history source is outside its owned namespace") from failure
    return path


def _identity(status):
    from .bus_epoch import _selected_member_identity
    return _selected_member_identity(status)


def _coordinate(relative):
    if (type(relative) is not str or not relative or len(relative) > 4096
            or "\x00" in relative or relative.startswith("/")):
        raise _failure("history capture coordinate is invalid")
    parts = () if relative == "." else tuple(relative.split("/"))
    if len(parts) > _MAX_CAPTURE_DEPTH or any(part in {"", ".", ".."} for part in parts):
        raise _failure("history capture coordinate is not normalized and bounded")
    return parts


class _CapturedIdentities:
    """Immutable shape-validated DATA; no reader or execution authority."""
    __slots__ = ("__weakref__",)

    def __init__(self, *args, **kwargs):
        raise _failure("capture DATA is created only by finite shape validation")

    def __len__(self):
        return len(_capture_data(self))

    def __iter__(self):
        return iter(_capture_data(self))

    def __getitem__(self, key):
        return _capture_data(self)[key]

    def __contains__(self, key):
        return key in _capture_data(self)

    def keys(self):
        return _capture_data(self).keys()

    def items(self):
        return _capture_data(self).items()


def _capture_data(value):
    if type(value) is not _CapturedIdentities or value not in _CAPTURE_DATA:
        raise _failure("capture DATA is forged or not shape validated")
    return _CAPTURE_DATA[value][0]


def _capture_map(value):
    # Only finite ordinary detached DATA. A map restricts reads; it never
    # supplies a reader, registers proof, or refreshes an earlier observation.
    limits = (_MAX_CAPTURE_COORDINATES, _MAX_CAPTURE_DEPTH)
    if type(value) is _CapturedIdentities:
        data = _capture_data(value)
        if _CAPTURE_DATA[value][1] == limits:
            return value
        value = data
    if type(value) not in (dict, MappingProxyType) or not 1 <= len(value) <= _MAX_CAPTURE_COORDINATES:
        raise _failure("history capture map is not a bounded ordinary mapping")
    result = {}
    for key, identity in value.items():
        _coordinate(key)
        if (type(identity) is not tuple or len(identity) != 7
                or any(type(member) is not int for member in identity)):
            raise _failure("history capture identity is malformed")
        result[key] = identity
    if "." not in result:
        raise _failure("history capture omits its physical root")
    for key in result:
        parts = _coordinate(key)
        if any("/".join(parts[:index]) not in result for index in range(1, len(parts))):
            raise _failure("history capture omits a parent coordinate")
    captured = object.__new__(_CapturedIdentities)
    _CAPTURE_DATA[captured] = (MappingProxyType(result), limits)
    return captured


@contextmanager
def _opened_capture(root, relative, expected, *, strict=True, directory=False):
    from .bus_epoch import _require_owned_epoch_guard
    held = _require_owned_epoch_guard(root)
    parts = _coordinate(relative)
    descriptors, links, observed = [], [], {}
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    def compare(key, status):
        actual = _identity(status)
        if expected is not None:
            if key not in expected:
                if strict:
                    raise _failure("history capture lacks a required coordinate")
            elif actual != expected[key]:
                raise _failure("history source differs from its retained capture")
        observed[key] = actual
    try:
        root_status = os.fstat(held.descriptor)
        compare(".", root_status)
        if _identity(root.tenant_home.lstat()) != _identity(root_status):
            raise _failure("history root changed before descriptor capture")
        descriptor = os.open(".", flags | os.O_DIRECTORY, dir_fd=held.descriptor)
        descriptors.append(descriptor)
        if _identity(os.fstat(descriptor)) != _identity(root_status):
            raise _failure("history root descriptor changed")
        status = root_status
        for index, name in enumerate(parts):
            key = "/".join(parts[:index + 1])
            parent = descriptor
            status = os.stat(name, dir_fd=parent, follow_symlinks=False)
            is_directory = index < len(parts) - 1 or directory
            if ((is_directory and not stat.S_ISDIR(status.st_mode))
                    or (not is_directory and (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1))):
                raise _failure("history source is a link, alias or nonordinary node")
            compare(key, status)
            descriptor = os.open(name, flags | (os.O_DIRECTORY if is_directory else 0), dir_fd=parent)
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            if _identity(opened) != _identity(status):
                raise _failure("history source changed between stat and open")
            links.append((parent, name, descriptor, _identity(status)))
        yield descriptor, status, observed
        for parent, name, child, identity in reversed(links):
            if (_identity(os.fstat(child)) != identity or
                    _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity):
                raise _failure("history source changed during descriptor capture")
        if (_identity(os.fstat(descriptors[0])) != _identity(root_status)
                or _identity(root.tenant_home.lstat()) != _identity(root_status)):
            raise _failure("history root changed during descriptor capture")
    except (OSError, ProtocolRefusal) as failure:
        raise _failure("committed history source is unavailable or invalid") from failure
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _capture_identities(root, relatives, *, previous=None):
    """Capture fixed native coordinates; extend only if all old ones still match."""
    if type(relatives) not in (tuple, list) or len(relatives) > _MAX_CAPTURE_COORDINATES:
        raise _failure("history capture paths must be a finite ordinary sequence")
    prior = None if previous is None else _capture_map(previous)
    coordinates = dict(prior or {})
    for relative in relatives:
        _coordinate(relative)
    requests = dict.fromkeys(relatives)
    # Recheck every old coordinate without adopting a replacement, even when
    # the requested extension does not happen to traverse that old leaf.
    for relative in tuple(coordinates) + tuple(requests):
        is_directory = relative == "." or (relative in coordinates and stat.S_ISDIR(coordinates[relative][2]))
        with _opened_capture(root, relative, prior, strict=False, directory=is_directory) as (_fd, _status, seen):
            for key, identity in seen.items():
                if key in coordinates and coordinates[key] != identity:
                    raise _failure("history capture changed while extending its census")
                if key not in coordinates and len(coordinates) >= _MAX_CAPTURE_COORDINATES:
                    raise _failure("history capture coordinate count exceeds support")
                coordinates[key] = identity
    if not coordinates:
        with _opened_capture(root, ".", prior, strict=False, directory=True) as (_fd, _status, seen):
            coordinates.update(seen)
    return _capture_map(coordinates)


def _read(root, relative, *, limit, captured=None, _expected_identities=None):
    if type(limit) is not int or limit < 0:
        raise _failure("history read limit must be a nonnegative integer")
    expected = (_capture_identities(root, (relative,)) if _expected_identities is None
                else _capture_map(_expected_identities))
    with _opened_capture(root, relative, expected) as (descriptor, status, _seen):
        if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_size > limit
                or (captured is not None and _identity(status) != _identity(captured))):
            raise _failure("history source is changed, nonregular or over its read bound")
        chunks, total = [], 0
        while True:
            block = os.read(descriptor, min(_READ_BLOCK, status.st_size + 1 - total))
            if not block:
                break
            total += len(block)
            if total > status.st_size:
                raise _failure("history source grew beyond its captured size")
            chunks.append(block)
        if total != status.st_size:
            raise _failure("history source ended before its captured size")
        data = b"".join(chunks)
    return data, status


def _read_ledger(root, *, _expected_identities=None):
    """Read actual whole ledger; only actual matching intent can bound a tail."""
    from .jsonl import MAX_LEDGER_BYTES
    from . import archive_publication_journal as publication
    expected = (_capture_identities(root, (LEDGER,)) if _expected_identities is None
                else _capture_map(_expected_identities))
    raw, status = _read(root, LEDGER, limit=MAX_LEDGER_BYTES, _expected_identities=expected)
    observation = None
    if raw and not raw.endswith(b"\n"):
        observation = publication.observe(root, _expected_identities=expected)
        if observation is None:
            raise _failure("incomplete history tail lacks exact native pending intent")
        classification, prefix = publication.classify(root, raw, status, observation)
        if classification != "rolled_back":
            raise _failure("incomplete history tail is outside its exact pending frame")
        raw = prefix
        # Alias identities constrain the actual intent read above. Ordinary
        # chunk traversal retains only ordinary leaves; nlink2 intent aliases
        # have their own exact observation and must not enter generic capture.
        expected = _capture_map({key: value for key, value in expected.items()
            if key not in observation.aliases})
    return raw, status, expected, observation


def _assert_pending_absence(root, status, expected, pending):
    """Absence is valid only for the actual empty native prefix and live intent."""
    from .jsonl import MAX_LEDGER_BYTES
    from . import archive_publication_journal as publication
    if pending.document["prior"]["generation"] is not None:
        raise _failure("empty native prefix cannot claim a previous generation")
    physical, current = _read(root, LEDGER, limit=MAX_LEDGER_BYTES, captured=status,
        _expected_identities=expected)
    classification, prefix = publication.classify(root, physical, current, pending)
    if classification != "rolled_back" or prefix:
        raise _failure("pending absence is not an actual incomplete first generation")
    publication.assert_bound(root, pending)
    _capture_identities(root, (), previous=expected)


@dataclass(frozen=True)
class CommittedGeneration:
    """Verified bytes, deliberately not semantic survivor admission."""
    _root: FloatiRoot
    _commit: dict
    _ledger_status: object
    _chunks: tuple
    _dependencies: tuple
    _expected_identities: object = None
    _publication: object = None

    def _assert_bound(self):
        from .jsonl import MAX_LEDGER_BYTES
        if self._expected_identities is not None:
            _capture_identities(self._root, (), previous=self._expected_identities)
        raw, status = _read(self._root, LEDGER, limit=MAX_LEDGER_BYTES, captured=self._ledger_status,
              _expected_identities=self._expected_identities)
        if self._publication is not None:
            from . import archive_publication_journal as publication
            observed = publication.observe(self._root, _expected_identities=self._publication.identities)
            if (observed is None or observed.raw != self._publication.raw
                    or observed.aliases != self._publication.aliases
                    or dict(observed.identities) != dict(self._publication.identities)):
                raise _failure("pending publication changed after prior-prefix admission")
            classification, _prefix = publication.classify(self._root, raw, status, observed)
            if classification != "rolled_back":
                raise _failure("pending publication physical tail changed after admission")
        for relative, status in self._dependencies:
            path = _owned_path(self._root, relative)
            try:
                if _identity(path.lstat()) != _identity(status):
                    raise _failure("committed history chunk changed after admission")
            except OSError as failure:
                raise _failure("committed history chunk is unavailable") from failure

    @property
    def commit(self):
        from .bus_epoch import epoch_guard
        with epoch_guard(self._root):
            self._assert_bound()
            return deepcopy(self._commit)

    def read_chunk(self, domain, ordinal):
        from .bus_epoch import epoch_guard
        with epoch_guard(self._root):
            self._assert_bound()
            for descriptor, status in self._chunks:
                if (descriptor["domain"], descriptor["ordinal"]) != (domain, ordinal):
                    continue
                raw, _status = _read(self._root,
                    CHUNK_DIRECTORY + "/" + descriptor["sha256"] + ".jsonl",
                    limit=min(MAX_CHUNK_BYTES, int(descriptor["byte_length"])), captured=status,
                    _expected_identities=self._expected_identities)
                if len(raw) != descriptor["byte_length"] or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]:
                    raise _failure("committed history chunk differs from its byte commitment")
                self._assert_bound()
                return raw
            return None


def read_committed_generation(root, *, _expected_identities=None):
    """Verify the native chain and every referenced chunk; ignore derived pointers."""
    from .bus_epoch import epoch_guard
    from .framing import encode_frame
    from .jsonl import MAX_LEDGER_BYTES, _decode_path_records
    if not isinstance(root, FloatiRoot):
        raise TypeError("history commit reader requires a FloatiRoot")
    with epoch_guard(root):
        path = _owned_path(root, LEDGER)
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as failure:
            raise _failure("native history commit ledger is unavailable") from failure
        raw, ledger_status, expected, publication = _read_ledger(root, _expected_identities=_expected_identities)
        rows = _decode_path_records(path, root.tenant_id, frozenset({KIND}), raw)
        if b"".join(encode_frame(row) for row in rows) != raw:
            raise _failure("native history commit ledger is not canonical framing")
        if not rows:
            if publication is not None:
                _assert_pending_absence(root, ledger_status, expected, publication)
            return None
        relatives = tuple(dict.fromkeys(CHUNK_DIRECTORY + "/" + descriptor["sha256"] + ".jsonl"
            for row in rows for descriptor in row["chunks"]))
        if _expected_identities is None:
            expected = _capture_identities(root, relatives, previous=expected)
        else:
            if any(relative not in expected for relative in relatives):
                raise _failure("retained capture omits a committed ancestor chunk")
            _capture_identities(root, (), previous=expected)
        previous = None
        generations = set()
        final_chunks = ()
        dependencies = {}
        for row in rows:
            if row["previous_generation"] != previous or row["generation"] in generations:
                raise _failure("native history generation predecessor is missing or duplicated")
            sources = hashlib.sha256(_SOURCES_DOMAIN)
            chunks = []
            for descriptor in row["chunks"]:
                relative = CHUNK_DIRECTORY + "/" + descriptor["sha256"] + ".jsonl"
                payload, status = _read(root, relative,
                    limit=min(MAX_CHUNK_BYTES, int(descriptor["byte_length"])), _expected_identities=expected)
                if relative in dependencies and _identity(status) != _identity(dependencies[relative]):
                    raise _failure("history chunk changed while its predecessor chain was admitted")
                dependencies[relative] = status
                if (len(payload) != descriptor["byte_length"]
                        or hashlib.sha256(payload).hexdigest() != descriptor["sha256"]):
                    raise _failure("committed history chunk differs from its exact byte commitment")
                if descriptor["domain"] == "sources":
                    sources.update(payload)
                chunks.append((deepcopy(descriptor), status))
            if sources.hexdigest() != row["source_commitment"]["sha256"]:
                raise _failure("history source collection differs from its complete commitment")
            previous = row["generation"]
            generations.add(previous)
            final_chunks = tuple(chunks)
        if publication is not None and previous != publication.document["prior"]["generation"]:
            raise _failure("pending intent's prior generation differs from the actual native prefix")
        result = CommittedGeneration(root, deepcopy(rows[-1]), ledger_status, final_chunks,
                                     tuple(dependencies.items()), expected, publication)
        result._assert_bound()
        return result
