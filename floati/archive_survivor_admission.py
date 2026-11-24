"""Cold committed semantic queries; never hold/write/removal authority.

Declared coverage and namespace witnesses do not authenticate current native
census. Publication must establish original source truth through its issuer.
"""
from dataclasses import dataclass
import hashlib
import inspect
import json
import os
import re
import stat
from weakref import WeakKeyDictionary

from . import archive_catalog as commits, archive_catalog_entries as catalogs
from . import archive_source_pool as pools, archive_survivor_bundle as bundles
from . import archive_consumption_domain, archive_execution_domain, archive_presentation_domain
from . import archive_consumption_replay, archive_execution_replay, archive_presentation_replay
from . import archive_projection_replay, cursor_presentations, epoch_carry, wake_hold, records, jsonl, framing
from . import epoch_prefix, bus_epoch
from . import archive_publication_journal as publication
from . import root as root_module, events, consumption, workers, worker_bootstrap_protocol
from . import worker_errors, readiness_contract, wake_control, epoch_teardown
from . import archive_container, archive_survivor_sources, seat_teardown_activity, cursor
from .archive_survivor_sources import (_domain_digest, _query_charge, _query_work_scope,
    _QUERY_ALLOWANCE, _QueryWorkExhausted)
from .bus_epoch import epoch_guard, _require_owned_epoch_guard, _selected_member_identity
from .errors import FloatiError, IntegrityFailure, ProtocolRefusal
from .root import FloatiRoot

MAX_COMMIT_RECORDS = 1024
MAX_REFERENCES = 1000000
MAX_UNIQUE_CHUNKS = 384
MAX_INPUT_BYTES = 1024 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
MAX_WORK = 1000000
_DOMAINS = {"catalog", "sources", "survivor"}
_REGISTERED = WeakKeyDictionary()


def _invalid(detail):
    return IntegrityFailure("archive_survivor_admission_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_survivor_admission_unsupported", detail,
        remedy="retain original epoch payload; committed queries exceed cold admission support")


def _semantic(operation):
    try:
        return operation()
    except FloatiError as exc:
        if exc.code.startswith("archive_survivor_admission_"):
            raise
        if exc.code.endswith("unsupported"):
            raise _unsupported("cold semantic support refused: " + exc.code) from exc
        raise _invalid("committed semantic evidence refused: " + exc.code) from exc
    except (OSError, TypeError, ValueError, KeyError, AttributeError, RecursionError, UnicodeError) as exc:
        raise _invalid("committed survivor evidence is unavailable or malformed") from exc


_ORIGINAL_SEMANTIC = _semantic
_ORIGINAL_SEMANTIC_CODE = _semantic.__code__


def _freeze_contract(value, *, _depth=0, _work=None, _closed=False):
    if _work is None:
        _work = [0, 0]
    _work[0] += 1
    if _depth > 64 or _work[0] > 1000000:
        raise _invalid("native verifier defaults exceed contract snapshot support")
    kind = type(value)
    if kind is re.Pattern:
        return (kind, _freeze_contract(value.pattern, _depth=_depth + 1,
                _work=_work, _closed=_closed), value.flags)
    if kind in (dict, tuple, list, set, frozenset):
        if len(value) > 1000000 - _work[0]:
            raise _invalid("native verifier defaults exceed contract snapshot support")
        if kind is dict:
            return (kind, tuple((_freeze_contract(key, _depth=_depth + 1, _work=_work, _closed=_closed),
                                _freeze_contract(child, _depth=_depth + 1, _work=_work, _closed=_closed))
                               for key, child in value.items()))
        children = tuple(_freeze_contract(child, _depth=_depth + 1, _work=_work, _closed=_closed) for child in value)
        return (kind, frozenset(children) if kind in (set, frozenset) else children)
    if _closed and kind not in (str, bytes, int, bool, float, type(None)):
        raise _invalid("native closed grammar contains nonordinary data")
    if kind in (str, bytes):
        _work[1] += len(value)
        if _work[1] > 8 * 1024 * 1024:
            raise _invalid("native verifier contract exceeds bounded scalar support")
    return (kind, value)


def _function_contract(value, *, _depth=0, _path=(), _budget=None):
    if not inspect.isfunction(value):
        raise _invalid("native verifier dependency is no longer a function")
    if _budget is None:
        _budget = [0]
    _budget[0] += 1
    if _depth > 64 or _budget[0] > 10000 or value in _path:
        raise _invalid("native decorated callable graph exceeds bounded contract support")
    path = _path + (value,)
    def dependency(function):
        return _function_contract(function, _depth=_depth + 1, _path=path, _budget=_budget)
    wrapped = getattr(value, "__wrapped__", None)
    wrapped_contract = None if wrapped is None else dependency(wrapped)
    # Bind the callable actually captured by a standard contextmanager's
    # closure as well as its public __wrapped__ chain. Never inspect mutable
    # closure caches/locks or treat caller callable objects as readers.
    callables = []
    for index, cell in enumerate(value.__closure__ or ()):
        try:
            member = cell.cell_contents
        except ValueError:
            continue
        if inspect.isfunction(member):
            callables.append((index, dependency(member)))
    return (value, value.__code__, _freeze_contract(value.__defaults__),
            _freeze_contract(value.__kwdefaults__), wrapped_contract, tuple(callables))


def _native_grammar_constants(work):
    # Only these two native grammar modules, only uppercase ordinary closed
    # constants, with the controller's known runtime globals explicitly out.
    # Regex grammars are detached to their immutable pattern/flag values.
    kinds = (str, bytes, int, bool, float, type(None), dict, tuple, list, set, frozenset, re.Pattern)
    return tuple((module.__name__, name, _freeze_contract(value, _work=work, _closed=True))
        for module in (records, wake_hold) for name, value in sorted(vars(module).items())
        if name.isupper() and name != "_WAKE_HOLD_CONTROLLER_GLOBALS" and type(value) in kinds)


def _closed_grammars():
    # Explicit format grammars only; no arbitrary mutable-global census and
    # no retained aliases to nested sets/maps. One bounded snapshot budget.
    named = ((commits, ("_DOMAINS", "_TRIGGERS")), (publication, ("_FIELDS",)),
        (catalogs, ("_FIELDS", "_FACTS")), (pools, ("_COMMON", "_FIELDS")),
        (bundles, ("_HEADER", "_DOMAINS", "LEAF_DOMAINS")),
        (archive_consumption_domain, ("_COMMON", "_FIELDS", "SEMANTIC_FIELDS")),
        (archive_execution_domain, ("_COMMON", "_FIELDS", "SEMANTIC_FIELDS")),
        (archive_presentation_domain, ("_COMMON", "_FIELDS", "_NODE_FIELDS", "_WITNESS")),
        (cursor_presentations, ("_IMMUTABLE", "KINDS")),
        (archive_presentation_replay, ("KINDS",)),
        (events, ("EVENT_KINDS",)), (consumption, ("WORK_KINDS",)),
        (workers, ("WORKER_KINDS",)),
        (archive_execution_replay, ("EVENT_KINDS", "WORKER_KINDS", "WORKER_OUTCOME_CODES")),
        (archive_consumption_domain, ("WORK_KINDS",)),
        (wake_control, ("_RESERVED", "_SESSION")),
        (readiness_contract, ("MECHANISMS", "READINESS_REASONS")),
        (epoch_teardown, ("_SEED_FIELDS",)),
        (workers.WorkerReceipts, ("_NEXT", "_STATES")),
        (wake_hold.WakeHoldController, ("_EVENT_DOMAIN", "_DELIVERY_DOMAIN", "_ACK_DOMAIN")),
        (epoch_prefix, ("_HEREDITARY_EVENTS",)),
        (records, ("_SPECS", "_V1_FIELDS", "_COMMON", "_MESSAGE_ENVELOPE_FIELDS",
                   "_MESSAGE_ENVELOPE_FIELD_SHAPES", "WAKE_HOLD_KINDS", "WORKER_OUTCOME_CODES")))
    work = [0, 0]
    return (tuple((module.__name__, name, _freeze_contract(vars(module)[name],
                  _work=work, _closed=True)) for module, names in named for name in names),
            _native_grammar_constants(work))


def _contract():
    # Bind actual verifier functions/code and subordinate class methods.
    # No serialized contract or caller dictionary can register/rebase a handle.
    modules = (commits, publication, catalogs, pools, bundles, archive_consumption_domain,
        archive_execution_domain, archive_presentation_domain, archive_consumption_replay,
        archive_execution_replay, archive_presentation_replay, archive_projection_replay,
        cursor_presentations, epoch_carry, wake_hold, records, jsonl, framing, epoch_prefix, bus_epoch,
        root_module, events, consumption, workers, worker_bootstrap_protocol, worker_errors,
        readiness_contract, wake_control, epoch_teardown, archive_container, archive_survivor_sources)
    result = []
    for module in modules:
        for name, value in sorted(vars(module).items()):
            if inspect.isfunction(value):
                result.append((module.__name__, name, _function_contract(value)))
            elif inspect.isclass(value) and value.__module__ == module.__name__:
                methods = []
                for key, method in sorted(vars(value).items()):
                    if isinstance(method, (staticmethod, classmethod)):
                        method = method.__func__
                    if isinstance(method, property):
                        method = method.fget
                    if inspect.isfunction(method):
                        methods.append((key, _function_contract(method)))
                result.append((module.__name__, name, value, tuple(methods)))
            elif inspect.isclass(value) and value.__module__.startswith("floati."):
                # Imported native class binding identity matters even when
                # its method code is snapshotted only in the actual owner.
                result.append((module.__name__, name, value))
            elif name.isupper() and type(value) in (str, bytes, int, bool, float, type(None)):
                result.append((module.__name__, name, value))
            elif type(value) is re.Pattern:
                result.append((module.__name__, name, _freeze_contract(value, _closed=True)))
    own = (admit_committed_survivors, _admission_identity, _preflight, _verify,
           _bound, _state, _root_identity, _semantic, _domain_digest, _contract,
           _function_contract, _freeze_contract, _closed_grammars, _native_grammar_constants,
           _check_contract, _encode_query_data, _query_input_charge, _query_data_charge,
           _query_charge, _query_work_scope,
           _verify_groups, _discard_query_admission, _cold_read_reservation,
           _query_row_costs, _charge_ledger_bytes, _measure_ledger_rows,
           _Admission.read, _Admission.generation.fget)
    limits = (MAX_COMMIT_RECORDS, MAX_REFERENCES, MAX_UNIQUE_CHUNKS, MAX_INPUT_BYTES,
              MAX_RESULT_BYTES, MAX_WORK, frozenset(_DOMAINS), _QUERY_ALLOWANCE)
    dynamic = tuple(_function_contract(getattr(bus_epoch, name)) for name in
        ("_selected_member_identity", "_require_owned_epoch_guard", "epoch_guard"))
    sparse = vars(cursor.SparseCursor)["_session_component"]
    sparse = sparse.__func__ if isinstance(sparse, (staticmethod, classmethod)) else sparse
    initializer = inspect.getattr_static(_State, "__init__")
    allocator = inspect.getattr_static(_State, "__new__")
    constructor = (_State, _function_contract(initializer), allocator)
    return (tuple(_function_contract(function) for function in own), tuple(result), limits,
            _closed_grammars(), dynamic, cursor.SparseCursor, _function_contract(sparse),
            _function_contract(seat_teardown_activity._unknown), seat_teardown_activity._MAX_PROCESSES,
            constructor)


_ORIGINAL_CONTRACT = _contract
_ORIGINAL_CONTRACT_CODE = _contract.__code__


def _root_identity(root):
    if type(root) is not FloatiRoot:
        raise _invalid("cold survivor admission requires an actual FloatiRoot")
    held = _require_owned_epoch_guard(root)
    opened = os.fstat(held.descriptor)
    return (root.tenant_home, root.tenant_id, opened.st_dev, opened.st_ino)


def _preflight(root):
    path = commits._owned_path(root, commits.LEDGER)
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    dependencies = commits._capture_identities(root, (commits.LEDGER,))
    if dependencies[commits.LEDGER][4] > min(jsonl.MAX_LEDGER_BYTES, MAX_INPUT_BYTES):
        raise _unsupported("native history ledger exceeds cold input support before read")
    _charge_ledger_bytes(dependencies[commits.LEDGER][4])
    # _read_ledger may parse the actual intent to classify an incomplete tail.
    # Reserve that possible metadata pass before entering its reader.
    try:
        _query_charge(commits._owned_path(root, publication.PENDING).lstat().st_size)
    except FileNotFoundError:
        pass
    raw, status, dependencies, pending = commits._read_ledger(root, _expected_identities=dependencies)
    base_input = 3 * len(raw) if pending is None else 4 * status.st_size + 4 * len(pending.raw)
    if base_input > MAX_INPUT_BYTES:
        raise _unsupported("physical ledger and pending observations exceed cold input support")
    if raw.count(b"\n") > MAX_COMMIT_RECORDS:
        raise _unsupported("native history ancestry exceeds cold commit-count support")
    rows = jsonl._decode_path_records(path, root.tenant_id, frozenset({commits.KIND}), raw)
    if not rows:
        if pending is not None:
            _query_charge(dependencies[commits.LEDGER][4] + len(pending.raw))
            commits._assert_pending_absence(root, status, dependencies, pending)
        return None
    if b"".join(framing.encode_frame(row) for row in rows) != raw:
        raise _invalid("native commit ledger is not canonical")
    # Charge every descriptor occurrence across all ancestors, even repeated
    # references to one physical file: the byte reader rereads every occurrence.
    # Initial observation, byte reader's ledger parse, and its final witness
    # reread are all real work. The semantic phase uses our validated row.
    # Partial-prefix admission verifies the whole physical ledger three times
    # through the byte reader plus once at registration, and observes actual
    # journal aliases at each point. Full canonical ledgers retain old costs.
    references, declared = 0, base_input
    unique = {}
    for row in rows:
        references += len(row["chunks"])
        declared += sum(int(item["byte_length"]) for item in row["chunks"])
        if references > MAX_REFERENCES or declared > MAX_INPUT_BYTES:
            raise _unsupported("repeated ancestry refs/bytes exceed support before bulk read")
        for item in row["chunks"]:
            relative = commits.CHUNK_DIRECTORY + "/" + item["sha256"] + ".jsonl"
            if relative in unique and unique[relative] != int(item["byte_length"]):
                raise _invalid("one chunk identity has inconsistent exact lengths")
            unique[relative] = int(item["byte_length"])
            if len(unique) > MAX_UNIQUE_CHUNKS:
                raise _unsupported("unique ancestry chunk census exceeds support")
    declared += sum(int(item["byte_length"]) for item in rows[-1]["chunks"])
    if declared > MAX_INPUT_BYTES:
        raise _unsupported("ancestry and semantic chunk rereads exceed cold input support")
    _measure_ledger_rows(rows)
    dependencies = commits._capture_identities(root, tuple(unique), previous=dependencies)
    for relative, length in unique.items():
        info = dependencies[relative]
        if not stat.S_ISREG(info[2]) or info[3] != 1 or info[4] != length:
            raise _invalid("committed chunk is aliased, missing or has a different exact length")
    return raw, rows, dependencies, pending


def _bound(root, scope, dependencies, pending=None):
    if _root_identity(root) != scope:
        raise _invalid("survivor admission belongs to a different physical root")
    commits._capture_identities(root, (), previous=dict(dependencies))
    if pending is not None:
        publication.assert_bound(root, pending)
        physical, status = commits._read(root, commits.LEDGER, limit=jsonl.MAX_LEDGER_BYTES,
            _expected_identities=commits._capture_map(dict(dependencies)))
        classification, _prefix = publication.classify(root, physical, status, pending)
        if classification != "rolled_back":
            raise _invalid("cold prior-prefix physical tail changed after admission")


def _charge_ledger_bytes(length):
    """Reserve the observed/planned physical metadata pass before decoding."""
    _query_charge(length)


def _measure_ledger_rows(rows):
    """Same bounded expanded-row charge for real and planned metadata passes."""
    _domain_digest("survivor_admission_ledger", rows,
                   _max_bytes=MAX_INPUT_BYTES, _max_nodes=MAX_WORK)


def _query_row_costs(logical_length, rows, metadata):
    """Pure occurrence arithmetic; no identities, reads or admission authority.

    The real reader supplies its observed complete/pending metadata amount;
    issuance supplies3*L only for its future completed endpoint. Descriptor
    occurrences remain distinct even when their physical chunk path is reused.
    """
    chunks = sum(int(item["byte_length"]) for row in rows for item in row["chunks"])
    latest = sum(int(item["byte_length"]) for item in rows[-1]["chunks"])
    references = sum(len(row["chunks"]) for row in rows)
    return metadata + chunks + latest, 2 * metadata + chunks + latest, 8 * (logical_length + references)


def _query_input_charge(root):
    """Root-only metadata preflight DATA, never prepared admission authority.

    A joined query calls normal cold admission independently afterward. Charge
    this extra metadata pass as well as every eventual cold read, including
    repeated ancestry descriptors. Work uses a conservative wire-byte bound on
    the bounded ledger's decoded nodes and repeated reference bookkeeping.
    """
    with epoch_guard(root):
        prepared = _preflight(root)
        if prepared is None:
            # Empty pending-first-publication observations are still real work.
            physical = 0
            for relative in (commits.LEDGER, publication.PENDING):
                try:
                    physical += commits._owned_path(root, relative).lstat().st_size
                except FileNotFoundError:
                    pass
            return 16 * physical, 16 * physical
        raw, rows, _dependencies, pending = prepared
        metadata = (3 * len(raw) if pending is None else
                    4 * _dependencies[commits.LEDGER][4] + 4 * len(pending.raw))
        _standalone, context, work = _query_row_costs(len(raw), rows, metadata)
        return context, work


def _query_data_charge(root, handle):
    """Bound a registered detached read before copying its JSON result."""
    _admission_identity(root, handle)
    state = _state(handle)
    # Every JSON node consumes at least one wire byte. This deliberately
    # conservative work reservation avoids parsing merely to budget a parse.
    return len(state.data), len(state.data)


def _encode_query_data(commit, catalog, sources, survivors):
    """Bound complete detached query DATA; this helper grants no admission.

    Publication preflights this same result before any output. Source entries
    come from the independently verified native pool, not a caller query view.
    Precharge all source bytes before decoding/copying their rows, then charge
    each row's expanded nodes before retaining it in the detached result.
    """
    result = {"generation": commit["generation"], "commit": commit,
              "catalog": catalog, "survivors": survivors, "sources": []}
    measured = []
    _domain_digest("survivor_admission_result", result,
        _max_bytes=MAX_RESULT_BYTES, _max_nodes=MAX_WORK,
        _on_measure=lambda size, nodes: measured.extend((size, nodes)))
    size, nodes = measured
    raw_entries = sources.entries
    extra = sum(len(raw) - 1 for raw in raw_entries.values()) + max(0, len(raw_entries) - 1)
    if size + extra > MAX_RESULT_BYTES or nodes + len(raw_entries) > MAX_WORK:
        raise _unsupported("complete query source DATA exceeds aggregate result support before copy")
    for raw in raw_entries.values():
        # PoolData retains exactly canonical newline-terminated rows. Its
        # aggregate wire size has been charged above before the first decode.
        _query_charge(len(raw))
        entry = json.loads(raw)
        measured = []
        _domain_digest("survivor_admission_source", entry,
            _max_bytes=MAX_RESULT_BYTES - size, _max_nodes=MAX_WORK - nodes,
            _on_measure=lambda entry_size, entry_nodes: measured.extend((entry_size, entry_nodes)))
        entry_size, entry_nodes = measured
        size += entry_size + bool(result["sources"])
        nodes += entry_nodes
        result["sources"].append(entry)
    _domain_digest("survivor_admission_result", result,
                   _max_bytes=MAX_RESULT_BYTES, _max_nodes=MAX_WORK)
    # Final domain measurement already debits the expanded serialization graph.
    return json.dumps(result, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify(root, prepared):
    raw, rows, dependencies, pending = prepared
    initial = dict(dependencies)
    if pending is not None:
        initial.update({key: pending.identities[key] for key in pending.aliases})
    # Reserve every physical ancestry read plus the latest semantic reread
    # before the first bulk reader; duplicate descriptors still cost reads.
    _query_charge(_cold_read_reservation(rows, dependencies[commits.LEDGER][4], pending))
    generation = commits.read_committed_generation(root, _expected_identities=commits._capture_map(initial))
    if type(generation) is not commits.CommittedGeneration or framing.encode_frame(generation._commit) != framing.encode_frame(rows[-1]):
        raise _invalid("byte generation differs from directly observed latest native commit")
    latest = rows[-1]
    groups = {domain: [] for domain in _DOMAINS}
    if {item["domain"] for item in latest["chunks"]} != _DOMAINS:
        raise _invalid("semantic generation must contain catalog, sources and survivor domains")
    for item in latest["chunks"]:
        relative = commits.CHUNK_DIRECTORY + "/" + item["sha256"] + ".jsonl"
        payload, status = commits._read(root, relative, limit=int(item["byte_length"]),
                                       _expected_identities=dependencies)
        if (_selected_member_identity(status) != dependencies[relative]
                or len(payload) != item["byte_length"] or hashlib.sha256(payload).hexdigest() != item["sha256"]):
            raise _invalid("semantic chunk differs from initially bound native bytes")
        groups[item["domain"]].append(payload)
    return _verify_groups(root.tenant_id, latest, groups)


def _cold_read_reservation(rows, ledger_size, pending=None):
    # The preflight reserved its metadata decode already. Remaining native
    # byte verification observes the ledger twice, then rereads latest chunks
    # for semantics. Every ancestor descriptor occurrence is a distinct read.
    metadata = 2 * ledger_size if pending is None else 3 * ledger_size + 4 * len(pending.raw)
    return (metadata + sum(int(item["byte_length"]) for row in rows for item in row["chunks"])
        + sum(int(item["byte_length"]) for item in rows[-1]["chunks"]))


def _verify_groups(tenant_id, latest, groups):
    """Pure semantic DATA preflight shared by issuer and actual cold admission."""
    sources = pools.read_pool(tuple(groups["sources"]), tenant_id)
    catalog = catalogs.read_catalog(tuple(groups["catalog"]), tenant_id, sources)
    entries = [catalog.entry(key) for key in catalog.keys]
    original = {key for key in sources.by_tag("frame")
                if sources.entry(key)["record"]["kind"] == "bus_epoch_roll_receipt"}
    if {entry["roll_frame"] for entry in entries} != original or len(entries) != len(original):
        raise _invalid("catalog omits or duplicates original source roll/retry testimony")
    survivors = bundles.verify_survivor_bundle(tuple(groups["sources"]), tenant_id,
                                               tuple(groups["survivor"]))
    projection, presentation = survivors["projection"], survivors["presentation"]
    execution, consumption = survivors["execution"], survivors["consumption"]
    counts = {"projection": len(projection["epochs"]) + len(projection["cuts"]) + len(projection["holds"]),
        "presentation": len(presentation["entries"]) + sum(len(entry["replay"]["groups"]) for entry in presentation["entries"]),
        "execution": len(execution["executions"]) + len(execution["worker_records"]) + len(execution["worker_joins"]),
        "consumption": len(consumption["receipts"]) + len(consumption["source_planes"]), "roll_retry": len(entries)}
    if any(entry["scope_count"] != counts[entry["domain"]] for entry in survivors["declared_coverage"]):
        raise _invalid("declared coverage counts disagree with recomputed committed domains")
    return _encode_query_data(latest, entries, sources, survivors)


def _discard_query_admission(handle):
    """Remove only the exact provisional registration owned by a failed join."""
    if type(handle) is _Admission:
        _REGISTERED.pop(handle, None)


@dataclass(frozen=True)
class _State:
    root: object
    scope: tuple
    generation: str
    dependencies: tuple
    contract: tuple
    data: bytes
    publication: object = None


_ORIGINAL_STATE_TYPE = _State
_ORIGINAL_STATE_INITIALIZER = inspect.getattr_static(_State, "__init__")
_ORIGINAL_STATE_INITIALIZER_CODE = _ORIGINAL_STATE_INITIALIZER.__code__
_ORIGINAL_STATE_ALLOCATOR = inspect.getattr_static(_State, "__new__")


class _Admission:
    __slots__ = ("__weakref__",)

    def __init__(self, *args, **kwargs):
        raise _invalid("survivor handles can only be privately registered by cold native admission")

    @property
    def generation(self):
        if _state is not _ORIGINAL_STATE or _state.__code__ is not _ORIGINAL_STATE_CODE:
            raise _invalid("private registry accessor was replaced")
        state = _state(self)
        if (_check_contract is not _ORIGINAL_CHECKER
                or _check_contract.__code__ is not _ORIGINAL_CHECKER_CODE):
            raise _invalid("cached contract checker was replaced")
        _check_contract(state)
        _admission_identity(state.root, self)
        if (_check_contract is not _ORIGINAL_CHECKER
                or _check_contract.__code__ is not _ORIGINAL_CHECKER_CODE):
            raise _invalid("cached contract checker was replaced")
        _check_contract(state)
        return state.generation

    def read(self):
        if _state is not _ORIGINAL_STATE or _state.__code__ is not _ORIGINAL_STATE_CODE:
            raise _invalid("private registry accessor was replaced")
        state = _state(self)
        def detached():
            if (_check_contract is not _ORIGINAL_CHECKER
                    or _check_contract.__code__ is not _ORIGINAL_CHECKER_CODE):
                raise _invalid("cached contract checker was replaced")
            _check_contract(state)
            with epoch_guard(state.root):
                _admission_identity(state.root, self)
                _query_charge(len(state.data))
                result = json.loads(state.data)
                _admission_identity(state.root, self)
            if (_check_contract is not _ORIGINAL_CHECKER
                    or _check_contract.__code__ is not _ORIGINAL_CHECKER_CODE):
                raise _invalid("cached contract checker was replaced")
            _check_contract(state)
            return result
        if _semantic is not _ORIGINAL_SEMANTIC or _semantic.__code__ is not _ORIGINAL_SEMANTIC_CODE:
            raise _invalid("native semantic entry trampoline was replaced")
        return _semantic(detached)


def _state(handle):
    if type(handle) is not _Admission or handle not in _REGISTERED:
        raise _invalid("survivor handle is forged, copied or not privately registered")
    return _REGISTERED[handle]


_ORIGINAL_STATE = _state
_ORIGINAL_STATE_CODE = _state.__code__


def _check_contract(state):
    if _contract is not _ORIGINAL_CONTRACT or _contract.__code__ is not _ORIGINAL_CONTRACT_CODE:
        raise _invalid("cold contract verifier was replaced")
    if state.contract != _contract():
        raise _invalid("survivor verifier contract changed after admission")


_ORIGINAL_CHECKER = _check_contract
_ORIGINAL_CHECKER_CODE = _check_contract.__code__


def _admission_identity(root, handle):
    """Recheck semantic query identity; returned DATA grants no act."""
    def require():
        if _state is not _ORIGINAL_STATE or _state.__code__ is not _ORIGINAL_STATE_CODE:
            raise _invalid("private registry accessor was replaced")
        state = _state(handle)
        if type(root) is not FloatiRoot or root.tenant_home != state.scope[0] or root.tenant_id != state.scope[1]:
            raise _invalid("survivor handle belongs to a different root")
        if (_check_contract is not _ORIGINAL_CHECKER
                or _check_contract.__code__ is not _ORIGINAL_CHECKER_CODE):
            raise _invalid("cached contract checker was replaced")
        _check_contract(state)
        with epoch_guard(root):
            if (_check_contract is not _ORIGINAL_CHECKER
                    or _check_contract.__code__ is not _ORIGINAL_CHECKER_CODE):
                raise _invalid("cached contract checker was replaced")
            _check_contract(state)
            _bound(root, state.scope, state.dependencies, state.publication)
            result = (state.scope[0], state.scope[1], state.scope[2], state.scope[3], state.generation)
        if (_check_contract is not _ORIGINAL_CHECKER
                or _check_contract.__code__ is not _ORIGINAL_CHECKER_CODE):
            raise _invalid("cached contract checker was replaced")
        _check_contract(state)
        return result
    if _semantic is not _ORIGINAL_SEMANTIC or _semantic.__code__ is not _ORIGINAL_SEMANTIC_CODE:
        raise _invalid("native semantic entry trampoline was replaced")
    return _semantic(require)


def admit_committed_survivors(root):
    """Admit cold queries from this root's actual committed generation only.

    No caller plan, PoolData, CommittedGeneration or serialized handle is an
    input. Registration binds semantic reads and exact physical commitments;
    it grants no current census, hold, mutation or removal authority.
    """
    def admit():
        if type(root) is not FloatiRoot:
            raise _invalid("cold survivor admission requires an actual FloatiRoot")
        with epoch_guard(root):
            if _contract is not _ORIGINAL_CONTRACT or _contract.__code__ is not _ORIGINAL_CONTRACT_CODE:
                raise _invalid("cold contract verifier was replaced")
            scope, contract = _root_identity(root), _contract()
            prepared = _preflight(root)
            if prepared is None:
                return None
            data = _verify(root, prepared)
            dependencies = tuple(prepared[2].items())
            _bound(root, scope, dependencies, prepared[3])
            if _contract is not _ORIGINAL_CONTRACT or _contract.__code__ is not _ORIGINAL_CONTRACT_CODE:
                raise _invalid("cold contract verifier was replaced during admission")
            if contract != _contract():
                raise _invalid("survivor verifier contract changed during admission")
            if (_State is not _ORIGINAL_STATE_TYPE
                    or inspect.getattr_static(_State, "__init__") is not _ORIGINAL_STATE_INITIALIZER
                    or _ORIGINAL_STATE_INITIALIZER.__code__ is not _ORIGINAL_STATE_INITIALIZER_CODE
                    or inspect.getattr_static(_State, "__new__") is not _ORIGINAL_STATE_ALLOCATOR):
                raise _invalid("private state constructor was replaced")
            handle = object.__new__(_Admission)
            _REGISTERED[handle] = _State(root, scope, prepared[1][-1]["generation"], dependencies, contract, data, prepared[3])
            return handle
    if _semantic is not _ORIGINAL_SEMANTIC or _semantic.__code__ is not _ORIGINAL_SEMANTIC_CODE:
        raise _invalid("native semantic entry trampoline was replaced")
    active = _QUERY_ALLOWANCE.get()
    try:
        with _query_work_scope(MAX_WORK):
            return _semantic(admit)
    except _QueryWorkExhausted as exc:
        if active is not None:
            raise
        raise _unsupported("cold query exceeds aggregate work support") from exc
