"""Invocation-owned hereditary projections of a private native archive source.

This is an optimization subset, not an acceptance vocabulary. One complete
unsealed native projection admits only backward-dependent event kinds and
physically bound holds. Any unsupported source, failed speculation or missing
cut dependency returns None so the resolver preserves native per-cut replay.
"""
from bisect import bisect_left, bisect_right

from .errors import IntegrityFailure, ProtocolRefusal


_HEREDITARY_EVENTS = frozenset({"message_envelope", "message_retracted",
    "send_readiness_observation", "delivery_claim"})


def _projector_arguments(source, node, worker, tenant):
    return dict(events=source[0], event_prefix_digests=source[1],
        deliveries=source[2], delivery_prefix_digests=source[3],
        acknowledgments=source[4], acknowledgment_prefix_digests=source[5],
        recipient=node, worker_session_id=worker, tenant_id=tenant)


def _prefix_positions(prefixes):
    positions = {}
    for ordinal, digest in enumerate(prefixes):
        positions.setdefault(digest, []).append(ordinal)
    return positions


def _grammar_contract():
    """Value snapshots, never aliases to mutable validator vocabularies."""
    from . import records
    def frozen(value):
        if isinstance(value, dict):
            return tuple(sorted((key, frozen(child)) for key, child in value.items()))
        if isinstance(value, (set, frozenset)):
            return frozenset(frozen(child) for child in value)
        if isinstance(value, (tuple, list)):
            return tuple(frozen(child) for child in value)
        return value
    return (records.validate_record, frozen(records._SPECS), frozen(records._V1_FIELDS),
        frozen(getattr(records, "_MESSAGE_ENVELOPE_FIELDS", None)),
        frozen(getattr(records, "_MESSAGE_ENVELOPE_FIELD_SHAPES", None)),
        getattr(records, "_message_envelope_field_shapes", None))


class _ArchivePrefixProjection:
    """Borrow only the resolver's private full source for one outer proof scope."""

    def __init__(self, source, node, worker, tenant, *, native_projector):
        self._ready = False
        try:
            self._admit(source, node, worker, tenant, native_projector)
        except (IntegrityFailure, ProtocolRefusal, KeyError, IndexError, TypeError, ValueError):
            # A schema-valid later delivery/ACK may invalidate the complete
            # source while an earlier requested cut is valid. Speculation must
            # never replace that cut's result or native failure precedence.
            pass

    def _admit(self, source, node, worker, tenant, native_projector):
        events, ep, deliveries, dp, acknowledgments, ap = source
        for ordinal, row in enumerate(events):
            if row.get("kind") in _HEREDITARY_EVENTS:
                continue
            if (ordinal == 0 and row.get("kind") == "bus_epoch_roll_receipt"
                    and "teardown_carry" not in row.get("automatic", {})):
                continue
            return
        event_prefixes, ack_prefixes = _prefix_positions(ep), _prefix_positions(ap)
        for ordinal, row in enumerate(deliveries):
            if row.get("kind") == "wake_hold_receipt" and (
                    row.get("delivery_prefix_digest") != dp[ordinal]
                    or row.get("event_prefix_digest") not in event_prefixes
                    or row.get("acknowledgment_prefix_digest") not in ack_prefixes):
                # Imported hashes require the resolver's sealed recursive proof.
                # Never translate an imported seal onto these physical axes.
                return
        admitted = native_projector(**_projector_arguments(source, node, worker, tenant))
        self._index(source, node, worker, tenant, admitted)

    def _index(self, source, node, worker, tenant, admitted):
        events, ep, deliveries, dp, acknowledgments, ap = source
        event_prefixes, ack_prefixes = _prefix_positions(ep), _prefix_positions(ap)
        positions, retractions = {}, {}
        for ordinal, row in enumerate(events, 1):
            if row["kind"] == "message_envelope":
                positions[row["id"]] = ordinal
            elif row["kind"] == "message_retracted":
                retractions[row["retracted_message_id"]] = ordinal
        message_rows = tuple(row.message for row in admitted)
        matching_positions = tuple(positions[row["id"]] for row in message_rows)
        delivery_ordinals = {row["id"]: [] for row in message_rows}
        first_presented, first_delivered, first_acknowledged = {}, {}, {}
        delivery_events, delivery_acks, ack_deliveries = [0], [0], [0]
        for ordinal, row in enumerate(deliveries, 1):
            ids = row["item_ids"]
            event_bound = max((positions[item] for item in ids), default=0)
            ack_bound = 0
            if row["kind"] == "wake_hold_receipt":
                occurrences = event_prefixes[row["event_prefix_digest"]]
                # Coverage accepts ANY sufficient occurrence. A later duplicate
                # must not raise the dependency above an earlier valid match.
                event_bound = occurrences[bisect_left(occurrences, event_bound)]
                ack_bound = ack_prefixes[row["acknowledgment_prefix_digest"]][0]
            delivery_events.append(max(delivery_events[-1], event_bound))
            delivery_acks.append(max(delivery_acks[-1], ack_bound))
            for item in ids:
                delivery_ordinals[item].append(ordinal)
                first_delivered.setdefault(item, ordinal)
                if row["kind"] != "delivered_unpresented":
                    first_presented.setdefault(item, ordinal)
        for ordinal, row in enumerate(acknowledgments, 1):
            bound = max(first_delivered[item] for item in row["item_ids"])
            ack_deliveries.append(max(ack_deliveries[-1], bound))
            for item in row["item_ids"]:
                first_acknowledged.setdefault(item, ordinal)
        self._limits = tuple(len(source[plane]) for plane in (0, 2, 4))
        self._message_rows = message_rows
        self._matching_positions = matching_positions
        self._retractions = retractions
        self._delivery_ordinals = delivery_ordinals
        self._first_presented = first_presented
        self._first_acknowledged = first_acknowledged
        self._delivery_events = tuple(delivery_events)
        self._delivery_acks = tuple(delivery_acks)
        self._ack_deliveries = tuple(ack_deliveries)
        self._source = source
        self._identity = node, worker, tenant
        self._positions = positions
        self._first_delivered = first_delivered
        self._ready = True

    def extend(self, source, *, previous_counts):
        """Admit only protected append suffixes; uncertainty keeps native replay.

        The owner has proved physical prefix identity and decoded the suffix.
        This stage proves joint delivery/ACK dependencies, then extends the
        existing ordinal indexes without replaying any earlier native row.
        Imported holds or unsupported suffix vocabulary never enter this path.
        """
        if not self._ready or previous_counts != self._limits:
            return False
        from copy import deepcopy
        candidate = deepcopy(self)
        try:
            if not candidate._extend(source):
                return False
        except (IntegrityFailure, ProtocolRefusal, KeyError, IndexError, TypeError, ValueError):
            return False
        self.__dict__.update(candidate.__dict__)
        return True

    def _extend(self, source):
        from .records import validate_record
        from .events import EVENT_KINDS
        node, worker, tenant = self._identity
        old = self._limits
        counts = tuple(len(source[axis]) for axis in (0, 2, 4))
        if any(new < prior for new, prior in zip(counts, old)):
            return False
        for axis, count in zip((0, 2, 4), old):
            if (tuple(source[axis + 1][:count + 1]) != tuple(self._source[axis + 1])
                    or source[axis][:count] != self._source[axis]):
                return False
        events, ep, deliveries, dp, acknowledgments, ap = source
        added_events = events[old[0]:]
        if any(row.get("kind") not in _HEREDITARY_EVENTS for row in added_events):
            return False
        for row in added_events:
            validate_record(dict(row), tenant, EVENT_KINDS, integrity=True)
        # The private owner has already applied native whole-event semantics
        # (including reverse-party replies and worker retractions). This helper
        # extends only that admitted source; it is not a second public reader.
        messages, matching = list(self._message_rows), list(self._matching_positions)
        for ordinal, row in enumerate(added_events, old[0] + 1):
            if row["kind"] == "message_envelope":
                if row["id"] in self._positions:
                    return False
                self._positions[row["id"]] = ordinal
                if row.get("recipient") == node and row.get("worker_session_id") == worker:
                    messages.append(row)
                    matching.append(ordinal)
                    self._delivery_ordinals[row["id"]] = []
            elif row["kind"] == "message_retracted":
                item = row["retracted_message_id"]
                if item not in self._positions or item in self._retractions:
                    return False
                self._retractions[item] = ordinal
        delivery_events, delivery_acks = list(self._delivery_events), list(self._delivery_acks)
        for ordinal, row in enumerate(deliveries[old[1]:], old[1] + 1):
            # New holds need their exact prefix/seal verifier. Keep this subset
            # to ordinary native delivery records; no imported-axis translation.
            if row.get("kind") not in {"delivery_receipt", "delivered_unpresented"}:
                return False
            validate_record(dict(row), tenant, {row["kind"]}, integrity=True)
            ids = row["item_ids"]
            if row["recipient"] != node or len(set(ids)) != len(ids) or (
                    row["kind"] != "delivery_receipt" and not ids):
                return False
            if any(item not in self._delivery_ordinals for item in ids) or any(
                    self._positions[a] >= self._positions[b] for a, b in zip(ids, ids[1:])):
                return False
            delivery_events.append(max(delivery_events[-1], max((self._positions[item] for item in ids), default=0)))
            delivery_acks.append(delivery_acks[-1])
            for item in ids:
                self._delivery_ordinals[item].append(ordinal)
                self._first_delivered.setdefault(item, ordinal)
                if row["kind"] != "delivered_unpresented":
                    self._first_presented.setdefault(item, ordinal)
        ack_deliveries = list(self._ack_deliveries)
        for ordinal, row in enumerate(acknowledgments[old[2]:], old[2] + 1):
            validate_record(dict(row), tenant, {"ack_receipt"}, integrity=True)
            ids = row["item_ids"]
            if row["recipient"] != node or not ids or len(set(ids)) != len(ids) or any(
                    item not in self._first_delivered for item in ids):
                return False
            ack_deliveries.append(max(ack_deliveries[-1], max(self._first_delivered[item] for item in ids)))
            for item in ids:
                self._first_acknowledged.setdefault(item, ordinal)
        self._source, self._limits = source, counts
        self._message_rows, self._matching_positions = tuple(messages), tuple(matching)
        self._delivery_events, self._delivery_acks = tuple(delivery_events), tuple(delivery_acks)
        self._ack_deliveries = tuple(ack_deliveries)
        return True

    def project(self, counts):
        """Derive an admitted cut or defer its exact result/refusal to native."""
        if not self._ready or not isinstance(counts, tuple) or len(counts) != 3:
            return None
        if any(type(count) is not int or not 0 <= count <= limit
                for count, limit in zip(counts, self._limits)):
            return None
        event_count, delivery_count, ack_count = counts
        if (self._delivery_events[delivery_count] > event_count
                or self._delivery_acks[delivery_count] > ack_count
                or self._ack_deliveries[ack_count] > delivery_count):
            return None
        from .wake_hold import WakeItemState
        result = []
        matched = bisect_right(self._matching_positions, event_count)
        for message in self._message_rows[:matched]:
            item = message["id"]
            if self._retractions.get(item, event_count + 1) <= event_count:
                state = "retracted"
            elif self._first_acknowledged.get(item, ack_count + 1) <= ack_count:
                state = "acknowledged"
            elif self._first_presented.get(item, delivery_count + 1) <= delivery_count:
                state = "held"
            else:
                state = "fresh"
            result.append(WakeItemState(message, state,
                bisect_right(self._delivery_ordinals[item], delivery_count)))
        return tuple(result)


def _projection_contract():
    """Bind every new semantic owner before any archive/origin cache hit."""
    from .wake_hold import WakeItemState
    return (_ArchivePrefixProjection, _ArchivePrefixProjection.__init__,
        _ArchivePrefixProjection._admit, _ArchivePrefixProjection.project,
        _ArchivePrefixProjection._index, _ArchivePrefixProjection.extend,
        _ArchivePrefixProjection._extend, _grammar_contract, _grammar_contract(),
        _projector_arguments, _prefix_positions, _projection_contract,
        _HEREDITARY_EVENTS, bisect_left, bisect_right, WakeItemState)
