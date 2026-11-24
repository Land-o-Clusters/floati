"""Invocation-local projection DATA over captured native epoch coordinates.

This adapter never registers a source, history handle or hold proof. Its
temporary native replay objects stay inside the unchanged pure kernel.
"""
from copy import deepcopy
import re

from . import history_query_join, records
from .archive_projection_replay import _projection_replayer
from .archive_survivor_sources import _query_charge, _domain_digest
from .errors import IntegrityFailure
from .framing import encode_frame
from .root import validate_identifier
from .cursor import SparseCursor


def _invalid(detail):
    return IntegrityFailure("epoch_history_query_invalid", detail)


def _selectors(coordinate, node, worker):
    # Validate exact built-in element types BEFORE any equality/hash operation.
    # A coordinate is a local DATA selector, never a registered capability.
    if type(coordinate) is not tuple or not (
        (len(coordinate) == 1 and type(coordinate[0]) is str and coordinate[0] == "live")
        or (len(coordinate) == 3 and type(coordinate[0]) is str
            and coordinate[0] == "archive" and type(coordinate[1]) is str
            and (coordinate[2] is None or type(coordinate[2]) is str))
    ):
        raise _invalid("projection home must be an exact native coordinate")
    if len(coordinate) == 3:
        prefix = records._SPECS["bus_epoch_roll_receipt"][0]
        for identity in coordinate[1:]:
            if identity is not None and (len(identity) != len(prefix) + 32
                    or re.fullmatch(re.escape(prefix) + records._UUID7, identity) is None):
                raise _invalid("projection coordinate has an invalid native roll receipt ID")
    if type(node) is not str or (worker is not None and type(worker) is not str):
        raise _invalid("projection selectors must be closed native strings")
    validate_identifier(node, "node")
    if worker is not None:
        SparseCursor._session_component(worker)


def _cut_data(source):
    _query_charge(11)  # three containers plus eight value/reference slots
    return {"counts": tuple(len(source[index]) for index in (0, 2, 4)),
            "digests": tuple(source[index][-1] for index in (1, 3, 5))}


def _states_data(states):
    # One list; each row adds its list reference, mapping and three value slots.
    _query_charge(5 * len(states) + 1)
    return [{"message": item.message, "state": item.state,
             "presentation_count": item.presentation_count} for item in states]


def _same_receipt(left, right):
    # Measure both canonical frames before allocating either encoding. Their
    # exact bytes (including each newline) are additional repeated work.
    for receipt in (left, right):
        _domain_digest("native_projection_receipt", receipt,
            _on_measure=lambda size, nodes: _query_charge(size + 1))
    return encode_frame(left) == encode_frame(right)


class _NativeView:
    """Four-method kernel adapter; keys are exact owner/anchor coordinates."""
    def __init__(self, root, native, max_result_bytes):
        _query_charge(4 * len(native) + 4)
        self.root, self.native, self.max_result_bytes = root, native, max_result_bytes
        self._epochs = [{"key": epoch.coordinate} for epoch in native]
        self._positions = {epoch.coordinate: index for index, epoch in enumerate(native)}
        self._sources, self._cuts = {}, {}

    def _source(self, epoch, node, worker):
        key = epoch, node, worker
        if key not in self._sources:
            _query_charge(4)
            self._sources[key] = history_query_join.source(self.root,
                self.native[self._positions[epoch]], node, worker,
                work=history_query_join._Work(0), max_result_bytes=self.max_result_bytes)
        return self._sources[key]

    def _cut_key(self, epoch, node, worker, source):
        _query_charge(11)
        key = (epoch, node, worker,
               tuple(len(source[index]) for index in (0, 2, 4)),
               tuple(source[index][-1] for index in (1, 3, 5)))
        self._cuts[key] = source
        return key


def project_native(root, native, coordinate, node, worker, *, max_result_bytes):
    """Return bounded detached DATA; caller must own source admission/guard.

    The registered context calls this driver under the private shared
    allowance. The result grants no native action authority.
    """
    _selectors(coordinate, node, worker)
    view = _NativeView(root, native, max_result_bytes)
    if coordinate not in view._positions:
        raise _invalid("projection home is outside this exact native lineage")
    project, original, origin, holds = _projection_replayer(view, root.tenant_id)
    source = view._source(coordinate, node, worker)
    states = project(source, coordinate, node, worker)
    _query_charge(len(source[2]))
    # Include physical originals as well as imported origins. Merely projecting
    # current states does not ask the kernel to reproduce every local artifact.
    for ordinal, receipt in enumerate(source[2]):
        if receipt["kind"] == "wake_hold_receipt":
            if original(receipt, coordinate, node, worker, source, ordinal) is None:
                origin(receipt, coordinate, node, worker)
    _query_charge(len(holds))  # deterministic original closure before sorting
    _query_charge(8)  # result mapping, its six value slots and origin list
    result = {"coordinate": coordinate, "recipient": node,
              "worker_session_id": worker, "source_cut": _cut_data(source),
              "states": _states_data(states), "origins": []}
    for identity in sorted(holds):
        hold = holds[identity]
        key = hold["before_cut"]
        epoch, _node, _worker, counts, digests = key
        before = view._cuts[key]
        full = view._source(epoch, node, worker)
        ordinal = counts[1]
        receipt = hold["receipt"]
        _query_charge(1)
        if ordinal >= len(full[2]) or not _same_receipt(full[2][ordinal], receipt):
            raise _invalid("original projection does not bind its exact physical receipt")
        # The kernel already located/checked both cuts. Advancing the single
        # original delivery frame needs no second set of twelve source slices.
        # after-cut's three containers/eight slots; origin mapping and its six
        # slots plus its list reference. Other nested DATA reserves itself.
        _query_charge(19)
        after_cut = {"counts": (counts[0], ordinal + 1, counts[2]),
                     "digests": (digests[0], full[3][ordinal + 1], digests[2])}
        original_states = project(before, epoch, node, worker)
        result["origins"].append({"receipt": receipt, "origin_coordinate": epoch,
            "before_cut": _cut_data(before), "after_cut": after_cut,
            "original_states": _states_data(original_states),
            "original_artifact": hold["artifact"]})
    # References above are private operation-local DATA. Measure the complete
    # expanded result BEFORE lending any detached copies to the caller.
    _domain_digest("native_history_projection", result, _max_bytes=max_result_bytes)
    return deepcopy(result)
