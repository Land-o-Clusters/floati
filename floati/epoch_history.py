"""Receipt-owned, identity-bound access to native epoch history."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import stat
from typing import Dict, Tuple
from weakref import WeakKeyDictionary

from .errors import IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames
from .records import validate_record
from .root import FloatiRoot


MAX_QUERY_INPUT_BYTES = 1024 * 1024 * 1024
MAX_QUERY_RESULT_BYTES = 64 * 1024 * 1024
MAX_QUERY_WORK = 1000000
_CONTEXTS = WeakKeyDictionary()
_EPOCHS = WeakKeyDictionary()


@dataclass(frozen=True)
class EpochArchive:
    """A verified legacy archive witness, without retaining its payload bytes."""

    _path: Path
    _receipt: Dict[str, object]
    _facts: Dict[str, object]
    _snapshot: tuple

    def _assert_bound(self) -> None:
        from .bus_epoch import _assert_epoch_archive_snapshot

        try:
            _assert_epoch_archive_snapshot(self._path, self._snapshot)
        except (ProtocolRefusal, OSError) as exc:
            raise IntegrityFailure(
                "epoch_receipt_archive_invalid",
                "completed roll receipt archive is unavailable or invalid",
            ) from exc

    @property
    def epoch_id(self) -> str:
        self._assert_bound()
        return str(self._receipt["epoch_id"])

    @property
    def receipt(self) -> Dict[str, object]:
        self._assert_bound()
        return deepcopy(self._receipt)

    @property
    def facts(self) -> Dict[str, object]:
        self._assert_bound()
        return deepcopy(self._facts)

    @property
    def members(self) -> Tuple[str, ...]:
        self._assert_bound()
        return tuple(name for name, plane, _status in self._snapshot if plane is not None)

    @property
    def legacy_path(self) -> Path:
        self._assert_bound()
        return self._path

    def read_member(self, relative: str) -> bytes | None:
        from .bus_epoch import _read_bound_regular

        if (not isinstance(relative, str) or not relative
                or relative.startswith("/")
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise ValueError("archive member must be a normalized relative path")
        self._assert_bound()
        from .bus_epoch import _CompressedStat
        for name, plane, status in self._snapshot:
            if name == relative and plane is not None:
                try:
                    if isinstance(status, _CompressedStat):
                        payload = status.payload
                    else:
                        payload = _read_bound_regular(
                            self._path / relative, status,
                            code="epoch_receipt_archive_invalid",
                            detail="epoch archive member is unreadable or changed",
                        )
                except (ProtocolRefusal, OSError) as exc:
                    raise IntegrityFailure(
                        "epoch_receipt_archive_invalid",
                        "epoch archive member is unreadable or changed",
                    ) from exc
                self._assert_bound()
                return payload
        return None


def _admit(root: FloatiRoot, receipt: Dict[str, object]) -> EpochArchive:
    from .bus_epoch import (
        _archive_facts_from_payloads, _epoch_receipt_archive_path,
        _read_epoch_archive_payloads,
    )

    archive = _epoch_receipt_archive_path(root, receipt)
    try:
        payloads, witness = _read_epoch_archive_payloads(archive)
    except (ProtocolRefusal, OSError) as exc:
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt archive is unavailable or invalid",
        ) from exc
    facts = _archive_facts_from_payloads(payloads)
    if any(receipt.get(field) != value for field, value in facts.items()):
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt facts do not match its archive",
        )
    return EpochArchive(archive, deepcopy(receipt), facts, witness)


def read_history(
    root: FloatiRoot, *, max_records: int, max_bytes: int,
    legacy_bounded_prefix: bool = False,
) -> Tuple[EpochArchive, ...]:
    """Follow only native receipts; strict readers never return a partial chain."""
    return tuple(archive for _receipt, archive in iter_roll_lineage(
        root, max_records=max_records, max_bytes=max_bytes,
        legacy_bounded_prefix=legacy_bounded_prefix))


def iter_roll_lineage(root, *, max_records, max_bytes, legacy_bounded_prefix=False,
                      report_absent=None):
    """Yield physical roll anchors and their admitted archives, newest first."""
    from .bus_epoch import _epoch_receipt_archive_path, _parse_physical_first_roll_receipt

    events_path = root.resolve_relative("events.jsonl")
    previous = None
    seen = set()
    while True:
        if legacy_bounded_prefix and len(seen) >= max_records:
            break
        try:
            if previous is not None:
                data = previous.read_member("events.jsonl")
            else:
                status = events_path.lstat()
                if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                    break
                data = events_path.read_bytes()
            if len(data) > max_bytes:
                raise IntegrityFailure(
                    "epoch_receipt_chain_invalid",
                    "a receipt-owned epoch event ledger exceeds the read bound",
                )
            first, newline, _tail = data[:65537].partition(b"\n")
            receipt = _parse_physical_first_roll_receipt(first + newline, root.tenant_id)
            if receipt is None:
                break
        except FileNotFoundError as exc:
            if previous is not None:
                raise IntegrityFailure(
                    "epoch_receipt_chain_invalid",
                    "a receipt-owned archive event ledger is unavailable",
                ) from exc
            break
        except OSError as exc:
            raise IntegrityFailure(
                "epoch_receipt_chain_invalid",
                "a claimed receipt-owned epoch event ledger is unavailable",
            ) from exc
        if len(seen) >= max_records:
            raise IntegrityFailure(
                "epoch_receipt_chain_invalid", "roll receipt archive lineage exceeds the read bound",
            )
        path = _epoch_receipt_archive_path(root, receipt)
        if path in seen:
            raise IntegrityFailure(
                "epoch_receipt_chain_invalid", "roll receipt archive lineage contains a cycle",
            )
        if report_absent is not None:
            try:
                path.lstat()
            except FileNotFoundError:
                if report_absent(receipt):
                    yield receipt, None
                    return
            except OSError:
                pass  # Let native archive admission issue the typed refusal.
        previous = _admit(root, receipt)
        seen.add(path)
        yield receipt, previous


def read_archived_bytes(archive: Path, relative: str) -> bytes | None:
    """Read one verified member from a directory archive or its compressed file."""
    from .bus_epoch import _read_epoch_archive_payloads

    if (not isinstance(relative, str) or not relative or relative.startswith("/")
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise ValueError("archive member must be a normalized relative path")
    payloads, _snapshot = _read_epoch_archive_payloads(archive)
    for name, _plane, raw in payloads:
        if name == relative:
            return raw
    return None


def _query_invalid(detail):
    return IntegrityFailure("epoch_history_query_invalid", detail)


def _query_unsupported(detail):
    return ProtocolRefusal("epoch_history_query_unsupported", detail,
        remedy="retain original epoch payloads; this history query exceeds supported admission")


def _query_contract():
    """Bind actual native reader and projection dependencies."""
    from . import bus_epoch, records, framing, epoch_carry, history_query_join, jsonl
    from . import history_query_projection, archive_projection_replay
    from . import wake_hold, cursor as cursor_module, root as root_module
    from .cursor import SparseCursor
    from .archive_survivor_admission import _function_contract, _freeze_contract
    from . import archive_survivor_sources as query_work
    def raw_method(owner, name, *, property_getter=False):
        # Original ordinary class namespaces are trusted anchors. Reading the
        # raw namespace avoids dispatching a newly assigned descriptor while
        # collecting identity. Nonfunctions become the existing typed refusal.
        method = vars(owner).get(name)
        if property_getter:
            return method.fget if type(method) is property else None
        if type(method) in (staticmethod, classmethod):
            method = method.__func__
        return method
    # This is the finite existing reader member census, not arbitrary object
    # introspection. Exact property shape matters as well as getter identity.
    classes = (
        (_ORIGINAL_QUERY_ALLOWANCE, ("__init__", "require", "charge"), ()),
        (_ORIGINAL_QUERY_ARCHIVE, ("_assert_bound", "read_member"),
            ("epoch_id", "receipt", "facts", "members", "legacy_path")),
        (_ORIGINAL_QUERY_CONTEXT, ("__getattribute__", "source", "projection_data"),
            ("epochs", "dependency_token", "generation", "live_coordinate")),
        (_ORIGINAL_QUERY_EPOCH, ("__getattribute__", "_read", "read_member"),
            ("epoch_id", "receipt", "facts", "members", "representation", "coordinate", "boundary")),
        (_ORIGINAL_QUERY_WORK, ("__init__", "charge"), ("remaining",)),
        (_ORIGINAL_QUERY_NATIVE_EPOCH, ("__init__",), ()),
        (_ORIGINAL_QUERY_NATIVE_VIEW, ("__init__", "_source", "_cut_key"), ()),
        (_ORIGINAL_QUERY_PROOFS, ("__init__", "assert_source", "validates", "original"), ()),
        (_ORIGINAL_QUERY_CONTROLLER, ("_artifact",), ()),
        (_ORIGINAL_QUERY_ITEM_STATE, ("__init__",), ()),
        (_ORIGINAL_QUERY_CURSOR, ("__init__", "_session_component",
            "_delivery_relative_path_for", "_relative_path_for"), ()))
    class_functions = tuple(raw_method(owner, name) for owner, methods, properties in classes
                            for name in methods) + tuple(
        raw_method(owner, name, property_getter=True) for owner, methods, properties in classes
        for name in properties)
    # Unwrapping is only for safe function inspection. Instance/static/class
    # bindings of the very same function have different call semantics, so
    # retain each raw member kind independently rather than normalizing it away.
    class_bindings = tuple((owner, name, type(vars(owner).get(name)))
        for owner, methods, properties in classes for name in methods + properties)
    functions = (read_history, _admit, validate_record, decode_frames, open_history, _open_history, _finish_history,
        envelope_membership, archived_item_membership, archived_envelope_membership,
        _prepare_native_state, _native_input_bytes,
        query_work._query_work_scope, query_work._query_charge, query_work._query_require,
        query_work._query_copy_charge, *class_functions,
        _query_contract, _context_state, _context_read, _query_witness, _check_query_readers,
        history_query_join.native_epochs, history_query_join.source,
        history_query_join.envelope_membership, history_query_join._decode_path_records,
        history_query_join.archived_item_membership,
        history_query_join.archived_envelope_membership, history_query_join._membership_batch_selectors,
        history_query_join._membership_plane, history_query_join._membership_delivery_rows,
        history_query_join._membership_contains_item, cursor_module.validate_identifier,
        history_query_join._query_charge,
        history_query_join._domain_digest,
        history_query_join.epoch_carry._decode, history_query_join.validate_event_records,
        history_query_projection.project_native, history_query_projection._selectors,
        history_query_projection._cut_data, history_query_projection._states_data,
        history_query_projection._same_receipt,
        history_query_projection._projection_replayer, history_query_projection._query_charge,
        history_query_projection._domain_digest, history_query_projection.deepcopy,
        history_query_projection.encode_frame, history_query_projection.validate_identifier,
        archive_projection_replay._projection_replayer,
        archive_projection_replay._original_hold_cuts, archive_projection_replay._imported_hold_origins,
        archive_projection_replay._project_origin_data, archive_projection_replay.encode_frame,
        archive_projection_replay.deepcopy,
        epoch_carry._original_hold_cuts, epoch_carry._imported_hold_origins,
        epoch_carry._project_origin_data, epoch_carry._binding, epoch_carry.verify_hold_proof_source,
        wake_hold.project_wake_items, wake_hold.validate_wake_decision_artifact,
        wake_hold.validate_record,
        wake_hold.wake_hold_decision_digest, wake_hold.validate_identifier,
        wake_hold._malformed_event_detail,
        wake_hold._citable_record_id, root_module.validate_identifier,
        epoch_carry.encode_frame,
        epoch_carry._decode_path_records, jsonl._decode_path_records, jsonl._decode_bounded_frames,
        jsonl.validate_record, jsonl.decode_frames, jsonl.is_known_record_kind, jsonl.validate_unknown_record,
        epoch_carry.carry_dependency_witness,
        bus_epoch.epoch_guard, bus_epoch.lock_order_guard,
        bus_epoch._scope_path, bus_epoch._same_lock_domain, bus_epoch._order_stack,
        bus_epoch._epoch_holds, bus_epoch._refresh_thread_state,
        bus_epoch._epoch_descriptor, bus_epoch._acquire_epoch_descriptor,
        bus_epoch._require_owned_epoch_guard,
        bus_epoch._archive_facts_from_payloads, bus_epoch._epoch_receipt_archive_path,
        bus_epoch._parse_physical_first_roll_receipt,
        bus_epoch.decode_frames, bus_epoch.validate_record,
        bus_epoch._read_epoch_archive_payloads, bus_epoch._epoch_archive_snapshot,
        bus_epoch._assert_epoch_archive_snapshot, bus_epoch._read_bound_regular,
        bus_epoch._preflight_selected_members, bus_epoch._selected_epoch_members,
        bus_epoch._read_selected_epoch_payloads,
        bus_epoch._selected_member_identity, records.validate_record, framing.decode_frames)
    return (tuple(_function_contract(function) for function in functions), class_bindings,
            _freeze_contract(records._SPECS, _closed=True),
            _freeze_contract(records._V1_FIELDS, _closed=True),
            records._UUID7, root_module.IDENTIFIER_PATTERN.pattern,
            root_module.IDENTIFIER_PATTERN.flags,
            MAX_QUERY_INPUT_BYTES, MAX_QUERY_RESULT_BYTES, MAX_QUERY_WORK,
            history_query_join.MAX_BATCH_IDS,
            epoch_carry.MAX_DEPENDENCY_WITNESS_ENTRIES, epoch_carry.MAX_DEPENDENCY_WITNESS_DEPTH,
            epoch_carry.MAX_LEDGER_BYTES, epoch_carry.MAX_LEDGER_RECORDS,
            bus_epoch.EPOCH_LOCK_TIMEOUT_SECONDS, bus_epoch.EPOCH_LOCK_POLL_SECONDS,
            bus_epoch.LOCK_ORDER_EPOCH, bus_epoch.LOCK_ORDER_WAKE, bus_epoch.LOCK_ORDER_LEDGER,
            jsonl.MAX_LEDGER_BYTES, jsonl.MAX_LEDGER_RECORDS, jsonl.MAX_RECORD_BYTES,
            EpochArchive, _HistoryContext, _HistoryEpoch, SparseCursor,
            history_query_join._Work, history_query_join._NativeEpoch,
            history_query_join.SparseCursor, history_query_join.WakeHoldController,
            history_query_projection._NativeView, history_query_projection.SparseCursor,
            history_query_projection.history_query_join,
            archive_projection_replay.WakeHoldController, wake_hold.WakeHoldController,
            epoch_carry._VerifiedHoldProofs,
            epoch_carry._PROOF_SEAL, wake_hold.WakeItemState,
            archive_projection_replay.MAX_REPLAY_RECORD_VISITS, archive_projection_replay.MAX_ORIGIN_DEPTH,
            query_work._QUERY_ALLOWANCE, query_work._QueryAllowance, query_work._QueryWorkExhausted,
            history_query_join._QUERY_ALLOWANCE,
            _freeze_contract((vars(_ORIGINAL_QUERY_CONTROLLER).get("_EVENT_DOMAIN"),
                vars(_ORIGINAL_QUERY_CONTROLLER).get("_DELIVERY_DOMAIN"),
                vars(_ORIGINAL_QUERY_CONTROLLER).get("_ACK_DOMAIN"),
                history_query_join.EVENT_KINDS, history_query_join.WAKE_HOLD_KINDS,
                wake_hold.WAKE_EVENT_KINDS, wake_hold.WAKE_HOLD_KINDS,
                wake_hold._UNINTERPRETED_EVENT_KINDS),
                _closed=True))


def _query_witness(root):
    from .epoch_carry import carry_dependency_witness
    witness = carry_dependency_witness(root)
    if witness is None:
        raise _query_invalid("complete history dependency namespace is unavailable")
    if len(witness) > MAX_QUERY_WORK:
        raise _query_unsupported("history dependency namespace exceeds aggregate query work support")
    return witness


@dataclass(frozen=True)
class _HistoryState:
    root: FloatiRoot
    witness: tuple
    archives: tuple
    contract: tuple
    native: tuple


def _context_state(handle):
    if type(handle) is not _HistoryContext or handle not in _CONTEXTS:
        raise _query_invalid("history context is forged, copied or not natively registered")
    return _CONTEXTS[handle]


def _check_query_readers(handle):
    """Check saved reader identities without invoking changed candidates.

    The fixed checker and its code are checked by each calling entry point.
    Private registries/frozen anchors and interpreter mutation are outside this
    in-process provenance boundary, as in native committed DATA admission.
    """
    if (_query_contract is not _ORIGINAL_QUERY_CONTRACT
            or _query_contract.__code__ is not _ORIGINAL_QUERY_CONTRACT_CODE):
        raise _query_invalid("history contract checker changed after admission")
    if type(handle) is _HistoryEpoch and handle in _EPOCHS:
        handle = _EPOCHS[handle][0]
    if type(handle) is not _HistoryContext or handle not in _CONTEXTS:
        raise _query_invalid("history handle is not natively registered")
    if _CONTEXTS[handle].contract != _ORIGINAL_QUERY_CONTRACT():
        raise _query_invalid("history reader contract changed after admission")


def _context_read(handle, operation):
    from .bus_epoch import epoch_guard
    if (_check_query_readers is not _ORIGINAL_QUERY_CHECKER
            or _check_query_readers.__code__ is not _ORIGINAL_QUERY_CHECKER_CODE):
        raise _query_invalid("history reader checker entry point changed")
    _ORIGINAL_QUERY_CHECKER(handle)
    state = _context_state(handle)
    with epoch_guard(state.root):
        def recheck():
            if (_check_query_readers is not _ORIGINAL_QUERY_CHECKER
                    or _check_query_readers.__code__ is not _ORIGINAL_QUERY_CHECKER_CODE):
                raise _query_invalid("history reader checker entry point changed")
            _ORIGINAL_QUERY_CHECKER(handle)
            if _query_witness(state.root) != state.witness:
                raise _query_invalid("history dependency membership or identity changed after admission")
        recheck()
        result = operation(state)
        recheck()
        return result


class _HistoryEpoch:
    """One privately derived retained epoch; never a Path or hold capability."""
    __slots__ = ("__weakref__",)

    def __init__(self):
        raise _query_invalid("history epoch handles require native context admission")

    def __getattribute__(self, name):
        if (_check_query_readers is not _ORIGINAL_QUERY_CHECKER
                or _check_query_readers.__code__ is not _ORIGINAL_QUERY_CHECKER_CODE):
            raise _query_invalid("history reader checker entry point changed")
        _ORIGINAL_QUERY_CHECKER(self)
        return object.__getattribute__(self, name)

    def _read(self, operation):
        if type(self) is not _HistoryEpoch or self not in _EPOCHS:
            raise _query_invalid("history epoch is forged, copied or not natively registered")
        context, ordinal = _EPOCHS[self]
        return _context_read(context, lambda state: operation(state.archives[ordinal]))

    @property
    def epoch_id(self):
        return self._read(lambda archive: archive.epoch_id)

    @property
    def receipt(self):
        return self._read(lambda archive: archive.receipt)

    @property
    def facts(self):
        return self._read(lambda archive: archive.facts)

    @property
    def members(self):
        return self._read(lambda archive: archive.members)

    @property
    def representation(self):
        return self._read(lambda archive: "legacy")

    @property
    def coordinate(self):
        context, ordinal = _EPOCHS[self]
        return _context_read(context, lambda state: state.native[ordinal + 1].coordinate)

    @property
    def boundary(self):
        return self._read(lambda archive: "automatic" if "automatic" in archive.receipt else "manual")

    def read_member(self, relative):
        return self._read(lambda archive: archive.read_member(relative))


class _HistoryContext:
    """Retained native query context; no current action authority."""
    __slots__ = ("__weakref__",)

    def __init__(self):
        raise _query_invalid("history contexts require the root-only native factory")

    def __getattribute__(self, name):
        if (_check_query_readers is not _ORIGINAL_QUERY_CHECKER
                or _check_query_readers.__code__ is not _ORIGINAL_QUERY_CHECKER_CODE):
            raise _query_invalid("history reader checker entry point changed")
        _ORIGINAL_QUERY_CHECKER(self)
        return object.__getattribute__(self, name)

    @property
    def epochs(self):
        def derive(state):
            result = []
            for ordinal in range(len(state.archives)):
                epoch = object.__new__(_HistoryEpoch)
                _EPOCHS[epoch] = (self, ordinal)
                result.append(epoch)
            return tuple(result)
        return _context_read(self, derive)

    @property
    def dependency_token(self):
        # Invalidation DATA only. Equality grants neither reuse nor proof.
        return _context_read(self, lambda state:
            (state.root.tenant_id, str(state.root.tenant_home), None, state.witness))

    @property
    def generation(self):
        return _context_read(self, lambda state: None)

    @property
    def live_coordinate(self):
        return _context_read(self, lambda state: ("live",))

    def source(self, coordinate, node, worker=None):
        # A previously retrieved bound method bypasses __getattribute__ on
        # invocation. Check before calling the replaceable scope/allowance.
        if (_check_query_readers is not _ORIGINAL_QUERY_CHECKER
                or _check_query_readers.__code__ is not _ORIGINAL_QUERY_CHECKER_CODE):
            raise _query_invalid("history reader checker entry point changed")
        _ORIGINAL_QUERY_CHECKER(self)
        def read(state):
            from . import history_query_join, history_query_projection
            history_query_projection._selectors(coordinate, node, worker)
            matches = [epoch for epoch in state.native if epoch.coordinate == coordinate]
            if len(matches) != 1:
                raise _query_invalid("source home is outside this exact native lineage")
            return history_query_join.source(state.root, matches[0], node, worker,
                work=history_query_join._Work(MAX_QUERY_WORK), max_result_bytes=MAX_QUERY_RESULT_BYTES)
        from .archive_survivor_sources import _query_work_scope, _QueryWorkExhausted
        try:
            with _query_work_scope(MAX_QUERY_WORK):
                return _context_read(self, read)
        except _QueryWorkExhausted as exc:
            raise _query_unsupported("source exceeds combined query work support") from exc

    def projection_data(self, coordinate, node, worker=None):
        if (_check_query_readers is not _ORIGINAL_QUERY_CHECKER
                or _check_query_readers.__code__ is not _ORIGINAL_QUERY_CHECKER_CODE):
            raise _query_invalid("history reader checker entry point changed")
        _ORIGINAL_QUERY_CHECKER(self)
        from . import history_query_projection
        from .archive_survivor_sources import _query_work_scope, _QueryWorkExhausted
        try:
            with _query_work_scope(MAX_QUERY_WORK):
                return _context_read(self, lambda state:
                    history_query_projection.project_native(state.root, state.native,
                        coordinate, node, worker, max_result_bytes=MAX_QUERY_RESULT_BYTES))
        except _QueryWorkExhausted as exc:
            raise _query_unsupported("projection exceeds combined query work support") from exc


def open_history(root):
    from .archive_survivor_sources import _query_work_scope, _QueryWorkExhausted
    try:
        with _query_work_scope(MAX_QUERY_WORK):
            return _open_history(root)
    except _QueryWorkExhausted as exc:
        raise _query_unsupported("history exceeds combined query work support") from exc


def envelope_membership(root, item_id):
    """Root-only one-ID DATA lookup; no retained index or action authority."""
    from .archive_survivor_sources import _query_work_scope, _QueryWorkExhausted
    from .bus_epoch import epoch_guard
    from . import history_query_join
    if type(root) is not FloatiRoot:
        raise _query_invalid("envelope membership requires an actual FloatiRoot")
    try:
        with _query_work_scope(MAX_QUERY_WORK):
            with epoch_guard(root):
                context = open_history(root)
                return _context_read(context, lambda state:
                    history_query_join.envelope_membership(state.root, state.native, item_id,
                        work=history_query_join._Work(MAX_QUERY_WORK),
                        max_result_bytes=MAX_QUERY_RESULT_BYTES))
    except _QueryWorkExhausted as exc:
        raise _query_unsupported("membership exceeds combined query work support") from exc


def archived_item_membership(root, item_id, recipient, worker=None):
    """One archive envelope OR exact delivery lookup; boolean DATA only."""
    from .archive_survivor_sources import _query_work_scope, _QueryWorkExhausted
    from .bus_epoch import epoch_guard
    from . import history_query_join
    if (type(root) is not FloatiRoot or type(item_id) is not str or type(recipient) is not str
            or (worker is not None and type(worker) is not str)):
        raise _query_invalid("archive membership requires actual root and exact native string selectors")
    try:
        with _query_work_scope(MAX_QUERY_WORK):
            with epoch_guard(root):
                context = open_history(root)
                # Reader/namespace checks precede all replaceable path helpers
                # and run again before exposing the final immutable boolean.
                return _context_read(context, lambda state:
                    history_query_join.archived_item_membership(state.root, state.native,
                        item_id, recipient, worker, work=history_query_join._Work(MAX_QUERY_WORK),
                        max_result_bytes=MAX_QUERY_RESULT_BYTES))
    except _QueryWorkExhausted as exc:
        raise _query_unsupported("archive membership exceeds combined query work support") from exc


def archived_envelope_membership(root, missing_ids):
    """Root-only batch archived-envelope presence; immutable boolean DATA."""
    from .archive_survivor_sources import _query_work_scope, _QueryWorkExhausted
    from .bus_epoch import epoch_guard
    from . import history_query_join
    if (type(root) is not FloatiRoot or type(missing_ids) is not frozenset
            or not 1 <= len(missing_ids) <= history_query_join.MAX_BATCH_IDS):
        raise _query_invalid("archived envelope membership requires actual root and bounded exact frozenset")
    try:
        with _query_work_scope(MAX_QUERY_WORK):
            with epoch_guard(root):
                context = open_history(root)
                # The driver pays N before selector traversal, after genuine
                # admission's reader check and before any archive comparison.
                return _context_read(context, lambda state:
                    history_query_join.archived_envelope_membership(state.root, state.native,
                        missing_ids, work=history_query_join._Work(MAX_QUERY_WORK),
                        max_result_bytes=MAX_QUERY_RESULT_BYTES))
    except _QueryWorkExhausted as exc:
        raise _query_unsupported("archived envelope batch exceeds combined query work support") from exc


def _open_history(root):
    """Open a root-only retained-legacy query; detached DATA grants no act."""
    from .bus_epoch import epoch_guard, _preflight_selected_members
    from . import history_query_join
    if type(root) is not FloatiRoot:
        raise _query_invalid("history query requires an actual FloatiRoot")
    with epoch_guard(root):
        witness, contract = _query_witness(root), _query_contract()
        # Conservative whole-root accounting includes unselected inputs and
        # two observations of each lineage event ledger before legacy bulk
        # admission. Unsupported roots preserve every original payload.
        native_bytes = _native_input_bytes(witness)
        if native_bytes > MAX_QUERY_INPUT_BYTES:
            raise _query_unsupported("aggregate legacy query input exceeds support before bulk reads")
        work = history_query_join._Work(MAX_QUERY_WORK)
        # Conservatively reserve all linear namespace observations performed
        # by native admission, capture and the final metadata rechecks.
        work.charge(native_bytes + 32 * len(witness))
        # The old Path walker intentionally stops at nonordinary live event
        # inputs. A native query context cannot interpret those as genesis.
        try:
            selected = _preflight_selected_members(root)
        except (ProtocolRefusal, OSError) as exc:
            raise _query_invalid("current selected history source is unavailable or nonordinary") from exc
        return _finish_history(root, witness, contract, work, selected)


def _finish_history(root, witness, contract, work, selected):
    state = _prepare_native_state(root, witness, contract, work, selected)
    handle = object.__new__(_HistoryContext)
    _CONTEXTS[handle] = state
    return handle


def _native_input_bytes(witness):
    # Actual native archive admission, event-chain traversal and joined source
    # capture each read archived event members. Preserve the existing bound.
    return 3 * sum(identity[4] for _relative, identity in witness
        if stat.S_ISREG(identity[2]))


def _prepare_native_state(root, witness, contract, work, selected):
    """Capture receipt-owned history before registering its query handle."""
    from .archive_survivor_sources import _domain_digest, _query_copy_charge
    from .bus_epoch import _read_selected_epoch_payloads
    from .jsonl import MAX_LEDGER_RECORDS, MAX_LEDGER_BYTES
    from . import history_query_join
    archives = read_history(root, max_records=MAX_LEDGER_RECORDS, max_bytes=MAX_LEDGER_BYTES)
    live = _read_selected_epoch_payloads(root, selected)
    try:
        native = history_query_join.native_epochs(root, archives, live, work=work)
    except FrameError as exc:
        raise IntegrityFailure(
            "epoch_receipt_chain_invalid",
            "a claimed receipt-owned epoch event ledger is malformed",
        ) from exc
    metadata = []
    for archive in archives:
        # The inventory includes private stat_result objects, not JSON
        # DATA. Reserve its member-filter slots directly; only receipt
        # and facts undergo expanded JSON copy measurement.
        work.charge(len(archive._snapshot))
        _query_copy_charge((archive._receipt, archive._facts))
        metadata.append({"receipt": archive.receipt, "facts": archive.facts,
                         "members": archive.members})
    _domain_digest("legacy_history_query", metadata,
                   _max_bytes=MAX_QUERY_RESULT_BYTES, _max_nodes=MAX_QUERY_WORK)
    if witness != _query_witness(root) or contract != _query_contract():
        raise _query_invalid("history dependencies or readers changed during admission")
    return _HistoryState(root, witness, archives, contract, native)


_ORIGINAL_QUERY_CONTRACT = _query_contract
_ORIGINAL_QUERY_CONTRACT_CODE = _query_contract.__code__
_ORIGINAL_QUERY_CHECKER = _check_query_readers
_ORIGINAL_QUERY_CHECKER_CODE = _check_query_readers.__code__
# The actual source alias is snapshotted separately. Read domain values only
# from this trusted original class, never from a replacement alias's getters.
from .wake_hold import WakeHoldController as _ORIGINAL_QUERY_CONTROLLER
from .wake_hold import WakeItemState as _ORIGINAL_QUERY_ITEM_STATE
from .epoch_carry import _VerifiedHoldProofs as _ORIGINAL_QUERY_PROOFS
from .history_query_projection import _NativeView as _ORIGINAL_QUERY_NATIVE_VIEW
from .history_query_join import _Work as _ORIGINAL_QUERY_WORK
from .history_query_join import _NativeEpoch as _ORIGINAL_QUERY_NATIVE_EPOCH
from .archive_survivor_sources import _QueryAllowance as _ORIGINAL_QUERY_ALLOWANCE
from .cursor import SparseCursor as _ORIGINAL_QUERY_CURSOR
_ORIGINAL_QUERY_ARCHIVE = EpochArchive
_ORIGINAL_QUERY_CONTEXT = _HistoryContext
_ORIGINAL_QUERY_EPOCH = _HistoryEpoch
