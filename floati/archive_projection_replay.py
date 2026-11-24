"""Pure survivor projection replay; results are data, never live admission."""
from copy import deepcopy

from .archive_source_pool import read_pool
from .epoch_carry import (_imported_hold_origins, _original_hold_cuts,
                          _project_origin_data)
from .errors import IntegrityFailure, ProtocolRefusal
from .framing import encode_frame
from .jsonl import VerifiedLedgerCursor
from .wake_hold import WakeHoldController, original_wake_selection


MAX_REPLAY_RECORD_VISITS = 1000000
MAX_ORIGIN_DEPTH = 64


def _failure(detail):
    return IntegrityFailure("archive_projection_replay_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_projection_replay_unsupported", detail,
        remedy="retain the original epoch payload; do not retire this unsupported dependency")


class PoolEpochView:
    """Logical epochs and cuts over immutable pool DATA, with no Path API.

    Construction verifies the pool's data contract, not native issuance. Public
    results are detached. No method exposes a runtime imported-hold capability.
    """
    def __init__(self, chunks, tenant_id):
        from .archive_survivor_sources import _query_charge
        self.tenant_id = tenant_id
        self._pool = read_pool(chunks, tenant_id)
        headers = {key: self._pool.entry(key) for key in self._pool.by_tag("epoch")}
        live = next(key for key, row in headers.items() if row["owner"] is None)
        self._epochs = []
        while live is not None:
            self._epochs.append(headers[live])
            live = headers[live]["predecessor"]
        self._positions = {row["key"]: index for index, row in enumerate(self._epochs)}
        self._frames = {key: self._pool.entry(key)["record"] for key in self._pool.by_tag("frame")}
        self._axes, self._sources, self._cuts = {}, {}, {}
        for key in self._pool.by_tag("axis"):
            axis = self._pool.entry(key)
            coordinate = (axis["epoch"], axis["plane"], axis["recipient"], axis["worker_session_id"])
            if coordinate in self._axes:
                raise _failure("projection source repeats a contextual axis")
            _query_charge(2 * len(axis["frames"]) + 1)  # rows and prefix slots
            rows = [self._frames[reference] for reference in axis["frames"]]
            digest = VerifiedLedgerCursor._initial_digest(axis["digest_domain"])
            prefixes = [digest.hexdigest()]
            for row in rows:
                digest.update(encode_frame(row))
                prefixes.append(digest.hexdigest())
            self._axes[coordinate] = (key, rows, prefixes)
        self._pair_sets, self._pairs = {}, {}
        for header in self._epochs:
            epoch = header["key"]
            rows = self._axes[(epoch, "events", None, None)][1]
            _query_charge(2 * len(rows))  # scope census and sorted pair materialization
            pairs = frozenset((row["recipient"], row.get("worker_session_id"))
                              for row in rows if row["kind"] == "message_envelope")
            self._pair_sets[epoch] = pairs
            self._pairs[epoch] = tuple(sorted(pairs, key=lambda pair: (pair[0], pair[1] or "")))
        if any(plane != "events" and (node, worker) not in self._pair_sets[epoch]
               for epoch, plane, node, worker in self._axes):
            raise _failure("projection source contains an extra contextual axis outside its native envelope scopes")
        for key in self._pool.by_tag("cut"):
            cut = self._pool.entry(key)
            coordinate = (cut["epoch"], cut["recipient"], cut["worker_session_id"],
                          tuple((cut["axes"][plane]["axis"], cut["axes"][plane]["count"],
                                 cut["axes"][plane]["digest"]) for plane in ("events", "deliveries", "acks")))
            self._cuts[coordinate] = key

    def epochs(self):
        from .archive_survivor_sources import _query_copy_charge
        _query_copy_charge(self._epochs)
        return deepcopy(self._epochs)

    def frame(self, key):
        from .archive_survivor_sources import _query_copy_charge
        _query_copy_charge(self._frames[key])
        return deepcopy(self._frames[key])

    def pairs(self, epoch):
        return self._pairs[epoch]

    def _source(self, epoch, node, worker):
        coordinate = (epoch, node, worker)
        if coordinate not in self._sources:
            result = []
            required = (node, worker) in self._pair_sets[epoch]
            for plane, domain in (("events", WakeHoldController._EVENT_DOMAIN),
                                  ("deliveries", WakeHoldController._DELIVERY_DOMAIN),
                                  ("acks", WakeHoldController._ACK_DOMAIN)):
                context = (epoch, plane, None if plane == "events" else node,
                           None if plane == "events" else worker)
                axis = self._axes.get(context)
                if axis is None:
                    if plane == "events" or required:
                        raise _failure("complete epoch projection omits its contextual source axis")
                    result.extend(([], [VerifiedLedgerCursor._initial_digest(domain).hexdigest()]))
                else:
                    result.extend((axis[1], axis[2]))
            self._sources[coordinate] = tuple(result)
        return self._sources[coordinate]

    def source(self, epoch, node, worker=None):
        from .archive_survivor_sources import _query_copy_charge
        source = self._source(epoch, node, worker)
        _query_copy_charge(source)
        return deepcopy(source)

    def cut(self, key):
        cut = self._pool.entry(key)
        if cut["tag"] != "cut":
            raise _failure("projection cut reference has the wrong type")
        source = self._source(cut["epoch"], cut["recipient"], cut["worker_session_id"])
        result = []
        for ordinal, plane in enumerate(("events", "deliveries", "acks")):
            count = cut["axes"][plane]["count"]
            from .archive_survivor_sources import _query_charge
            _query_charge(2 * count + 1)  # slices before their allocation
            result.extend((source[ordinal * 2][:count], source[ordinal * 2 + 1][:count + 1]))
        from .archive_survivor_sources import _query_copy_charge
        _query_copy_charge(result)
        return deepcopy(tuple(result))

    def _cut_key(self, epoch, node, worker, source):
        return self._cut_key_counts(epoch, node, worker,
            tuple(len(source[index]) for index in (0, 2, 4)),
            tuple(source[index][-1] for index in (1, 3, 5)))

    def _cut_key_counts(self, epoch, node, worker, counts, digests):
        """Resolve an exact existing cut without allocating source prefixes."""
        axes = []
        for ordinal, plane in enumerate(("events", "deliveries", "acks")):
            coordinate = (epoch, plane, None if plane == "events" else node,
                          None if plane == "events" else worker)
            if coordinate not in self._axes:
                raise _failure("original projection cut lacks a complete source axis")
            axes.append((self._axes[coordinate][0], counts[ordinal], digests[ordinal]))
        key = self._cuts.get((epoch, node, worker, tuple(axes)))
        if key is None:
            raise _failure("source pool omits an exact original or complete projection cut")
        return key


def _projection_replayer(view, tenant_id):
    holds, origins, resolving = {}, {}, set()
    projections, original_results, located_origins = {}, {}, {}
    record_visits = 0

    def charge(visits):
        from .archive_survivor_sources import _query_charge
        _query_charge(visits)
        nonlocal record_visits
        if record_visits + visits > MAX_REPLAY_RECORD_VISITS:
            raise _unsupported("native projection replay exceeds its aggregate record-visit support bound")
        record_visits += visits

    def project(source, epoch, node, worker):
        key = (epoch, node, worker, tuple((len(source[index]), source[index + 1][-1]) for index in (0, 2, 4)))
        if key in projections:
            return projections[key]
        charge(sum(len(source[index]) for index in (0, 2, 4)))
        from .archive_survivor_sources import _query_copy_charge
        _query_copy_charge(source)  # expanded projection inputs before states/artifacts
        imported = _imported_hold_origins(source, node, worker, tenant_id,
            lambda receipt: origin(receipt, epoch, node, worker))
        states = _project_origin_data(source, node, worker, tenant_id, imported)
        projections[key] = states
        return states

    def original(receipt, epoch, node, worker, source, ordinal):
        identity = (epoch, node, worker, encode_frame(receipt))
        if identity in original_results:
            return original_results[identity]
        bound = _original_hold_cuts(source, receipt, ordinal)
        if bound is None:
            return None
        before, after = bound
        project(after, epoch, node, worker)
        states = project(before, epoch, node, worker)
        fresh, held = original_wake_selection(states, receipt)
        if not fresh and [item.message["id"] for item in held[:receipt["limit"]]] == receipt["item_ids"]:
            raise ProtocolRefusal("archive_checkpoint_unsupported",
                "held redelivery lacks historical holder-release decision testimony",
                remedy="retain the original epoch payload; do not retire this unsupported dependency")
        if [item.message["id"] for item in fresh[:receipt["limit"]]] != receipt["item_ids"]:
            raise _failure("hold does not reproduce its exact original semantic selection")
        from .archive_survivor_sources import _query_copy_charge
        _query_copy_charge((receipt, source))
        artifact = WakeHoldController._artifact(tenant_id=tenant_id, recipient=node,
            worker_session_id=worker, limit=receipt["limit"], fresh=fresh, held=held, receipt=receipt,
            event_digest=receipt["event_prefix_digest"], delivery_digest=receipt["delivery_prefix_digest"],
            acknowledgment_digest=receipt["acknowledgment_prefix_digest"])
        _query_copy_charge((receipt, artifact))
        result = {"receipt": deepcopy(receipt), "before_cut": view._cut_key(epoch, node, worker, before),
                  "artifact": deepcopy(artifact)}
        prior = holds.get(receipt["id"])
        if prior is not None and prior != result:
            raise _failure("one hold has conflicting original projection results")
        holds[receipt["id"]] = result
        native_result = encode_frame(receipt), before, states
        original_results[identity] = native_result
        located_origins[(node, worker, encode_frame(receipt))] = (view._positions[epoch], native_result)
        return native_result

    def origin(receipt, epoch, node, worker):
        key = (epoch, node, worker, encode_frame(receipt))
        if key in origins:
            return origins[key]
        located = located_origins.get((node, worker, key[-1]))
        if located is not None and located[0] > view._positions[epoch]:
            return located[1]
        if key in resolving:
            raise _failure("imported survivor origin contains a cycle")
        if len(resolving) >= MAX_ORIGIN_DEPTH:
            raise _unsupported("native projection origin traversal exceeds its depth support bound")
        resolving.add(key)
        try:
            for header in view._epochs[view._positions[epoch] + 1:]:
                charge(1)
                old_epoch = header["key"]
                source = view._source(old_epoch, node, worker)
                for ordinal, old in enumerate(source[2]):
                    charge(1)
                    if old["id"] != receipt["id"]:
                        continue
                    if encode_frame(old) != encode_frame(receipt):
                        raise _failure("imported hold ID has conflicting original testimony")
                    result = original(old, old_epoch, node, worker, source, ordinal)
                    if result is not None:
                        origins[key] = result
                        return result
            raise _failure("imported hold lacks its original source cut in the complete epoch graph")
        except RecursionError as exc:
            raise _unsupported("native projection origin traversal exceeds the runtime recursion bound") from exc
        finally:
            resolving.remove(key)

    return project, original, origin, holds


def replay_projection_pool(chunks, tenant_id):
    """Reproduce native projection and original hold artifacts without I/O.

    This verifier accepts unprivileged bytes and returns unprivileged data.
    Committed-generation admission and current-source proof issuance remain
    separate native boundaries. No proof object escapes this replay.
    """
    view = PoolEpochView(chunks, tenant_id)
    project, original, origin, holds = _projection_replayer(view, tenant_id)
    cuts, headers = [], []
    for header in view._epochs:
        epoch = header["key"]
        owner = view._frames[header["owner"]] if header["owner"] is not None else None
        anchor = view._frames[header["anchor"]]["id"] if header["anchor"] is not None else None
        from .archive_survivor_sources import _query_copy_charge
        _query_copy_charge(owner)
        headers.append({"epoch_id": header["epoch_id"], "automatic": "automatic" in owner if owner else None,
                        "roll_receipt": deepcopy(owner), "anchor": anchor})
        for node, worker in view.pairs(epoch):
            source = view._source(epoch, node, worker)
            states = project(source, epoch, node, worker)
            _query_copy_charge(source[0])  # every state contains one source envelope
            cuts.append({"epoch_id": header["epoch_id"], "recipient": node, "worker_session_id": worker,
                         "source_cut": view._cut_key(epoch, node, worker, source),
                         "states": [{"message": deepcopy(item.message), "state": item.state,
                                     "presentation_count": item.presentation_count} for item in states]})
            for ordinal, receipt in enumerate(source[2]):
                if receipt["kind"] == "wake_hold_receipt":
                    if original(receipt, epoch, node, worker, source, ordinal) is None:
                        origin(receipt, epoch, node, worker)
    return {"schema_version": 1, "domain": "projection_replay", "tenant_id": tenant_id,
            "epochs": headers, "cuts": cuts, "holds": [holds[key] for key in sorted(holds)]}
