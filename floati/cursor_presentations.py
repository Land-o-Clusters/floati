"""Native conversation publication, independent of routing and acknowledgment.

Prepared means one automatic attempt is reserved, not that output was seen.
Flushed means the exact hook body was flushed, not that a person acted.
All methods are private publication seams under the native epoch/node guards.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from bisect import bisect_left
from contextlib import contextmanager
from pathlib import Path

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .events import EventLog, EVENT_KINDS, validate_event_records
from .framing import encode_frame, decode_frames
from .ids import uuid7_hex
from .inbound_hold import require_presentable, withhold_unapproved_selection
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES, VerifiedLedgerCursor, _decode_path_records, read_records_snapshot
from .records import validate_record
from .stop_hold import StopHoldLedger
from .wake_hold import WakeHoldController, WAKE_HOLD_KINDS, project_wake_items

KIND = "cursor_presentation_receipt"
KINDS = frozenset({KIND})
OUTPUT_BYTES = 262144
_IMMUTABLE = frozenset({
    "schema_version", "kind", "tenant_id", "batch_id", "node_id", "acting_session_id",
    "workspace", "harness", "consent_receipt_id", "claim_receipt_id", "epoch_anchor",
    "item_ids", "event_prefix_digest", "delivery_prefix_digest",
    "acknowledgment_prefix_digest", "output_digest",
})
_NATIVE_IMMUTABLE_TYPES = frozenset({type(None), bool, int, float, str})


def _failure(detail):
    return IntegrityFailure("presentation_history_unavailable", detail)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _clone_native(value):
    """Detach native JSON trees, retaining generic copying for other types."""
    kind = type(value)
    if kind in _NATIVE_IMMUTABLE_TYPES:
        return value
    if kind is dict:
        result = value.copy()
        for key, child in value.items():
            if type(child) not in _NATIVE_IMMUTABLE_TYPES:
                result[key] = _clone_native(child)
        return result
    if kind is list:
        result = value.copy()
        for index, child in enumerate(value):
            if type(child) not in _NATIVE_IMMUTABLE_TYPES:
                result[index] = _clone_native(child)
        return result
    # Tuples/dataclasses, subclasses and unknown objects retain deepcopy's
    # native protocols and memo handling. Admitted JSON trees are acyclic.
    return copy.deepcopy(value)


def _same_value(left, right):
    # Python equality equates True and 1. Such a mutation must still reach
    # native schema validation, even when the source's byte digest is unchanged.
    if left is right:
        return True
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if not (left.keys() == right.keys()):
            return False
        for key, value in left.items():
            other = right[key]
            if value is not other and not _same_value(value, other):
                return False
        return True
    if isinstance(left, (list, tuple)):
        if len(left) != len(right):
            return False
        for value, other in zip(left, right):
            if value is not other and not _same_value(value, other):
                return False
        return True
    return left == right


def _projection_rows(source):
    return tuple(tuple(_clone_native(row) for row in source[axis]) for axis in (0, 2, 4))


def _same_projection_rows(source, admitted):
    return all(len(source[axis]) == len(rows)
        and all(_same_value(row, saved) for row, saved in zip(source[axis], rows))
        for axis, rows in zip((0, 2, 4), admitted))


def notification(root, node, runtime, session, messages, *, presence=""):
    from .colleague_frame import frame_clause
    from .cursor_wait import _drain_command
    return {"followup_message": (
        (presence + "\n" if presence else "") + frame_clause(messages) + "\n"
        f"[floati] unread mail: {len(messages)} new message(s) for {node} on {root}.\n"
        "Exact native envelopes:\n" + json.dumps(messages, sort_keys=True) + "\n"
        "Act on these envelopes, newest architect order first, then acknowledge each exact ID "
        f"with this acting session: {session}.\n"
        "Drain it now:\n"
        + _drain_command(root, node, runtime, session)
        + "\nThis command may return an older held prefix; the selected envelopes above are the notification."
    )}


def limit_selection(selection, bodybuilder):
    chosen = []
    body = bodybuilder(chosen)
    for message in selection["fresh_messages"][:1000]:
        candidate = bodybuilder(chosen + [message])
        if len((json.dumps(candidate) + "\n").encode("utf-8")) > OUTPUT_BYTES:
            break
        chosen.append(message)
        body = candidate
    if selection["fresh_messages"] and not chosen:
        raise ProtocolRefusal("presentation_output_too_large", "one native envelope exceeds the bounded hook body",
            remedy="use the native inbox to act on and acknowledge the exact oversized envelope")
    return dict(selection, fresh_messages=chosen), body


class _HistoricalCuts:
    """Retain native admission and each historical cut's dependencies.

    Native validation is prefix-hereditary except for dependencies between
    the event, delivery and ACK axes. Prefix maxima retain those dependencies
    without validating every earlier event again for every presentation.
    This is invocation-local derived state, never persisted authority.
    """

    def __init__(self, source, node, tenant, limits, *, root=None, home=None, resolver=None, _replay=None):
        self.source, self.node, self.tenant = source, node, tenant
        self.root, self.home, self.resolver = root, home, resolver
        self._replay = _replay
        self._replay_cache, self._eligible_cache = {}, {}
        self.limits = limits
        self.full_states = None
        self.full_binding = None
        self.full_rows = None
        full_counts = tuple(len(source[axis]) for axis in (0, 2, 4))
        try:
            # This is speculative, side-effect-free native validation. A bad
            # unreferenced tail must not change read() or refusal precedence.
            self.full_states = self._native(full_counts)
        except Exception:
            self._native(limits)
            indexed_counts = limits
        else:
            self.full_states = _clone_native(tuple(self.full_states))
            self.full_rows = _projection_rows(source)
            self.full_binding = self.source_binding(source, node, tenant)
            indexed_counts = full_counts
        self._indexed_counts = indexed_counts
        self._indexed = True
        event_count, delivery_count, acknowledgment_count = indexed_counts
        events, ep, deliveries, dp, acknowledgments, ap = source
        if any(receipt["kind"] == "wake_hold_receipt" and (
                receipt["delivery_prefix_digest"] != dp[ordinal]
                or receipt["event_prefix_digest"] not in ep[:event_count + 1]
                or receipt["acknowledgment_prefix_digest"] not in ap[:acknowledgment_count + 1])
                for ordinal, receipt in enumerate(deliveries[:delivery_count])):
            # Imported hashes name the verified archive's axes, not these
            # re-anchored physical axes. No index may translate that proof.
            # Reproduce every requested cut through the sealed native path.
            self._indexed = False
            self._native(limits)
            return
        self.messages, self.retracted, self.acknowledged = {}, {}, {}
        event_positions = {}
        for ordinal, event in enumerate(events[:event_count], 1):
            if event["kind"] == "message_envelope":
                event_positions[event["id"]] = ordinal
                if event.get("recipient") == node and event.get("worker_session_id") is None:
                    self.messages[event["id"]] = ordinal
            elif event["kind"] == "message_retracted":
                self.retracted.setdefault(event["retracted_message_id"], ordinal)
        event_digests = {}
        for ordinal, digest in enumerate(ep[:event_count + 1]):
            event_digests.setdefault(digest, []).append(ordinal)
        self._index_receipts(event_positions, event_digests, deliveries[:delivery_count],
            acknowledgments[:acknowledgment_count], ap[:acknowledgment_count + 1])

        if self.full_states is not None and not self._dependencies_admit(limits):
            # Full validity cannot make a historical delivery/hold/ACK borrow
            # evidence beyond its cut. Reproduce the original joint refusal
            # before any bounded lookup could report a missing event/digest.
            self._native(limits)

    def _index_receipts(self, event_positions, event_digests, deliveries, acknowledgments, ack_prefixes):
        acknowledgment_digests = {}
        for ordinal, digest in enumerate(ack_prefixes):
            acknowledgment_digests.setdefault(digest, ordinal)
        self.delivery_events, self.delivery_acks = [0], [0]
        first_delivery = {}
        for ordinal, receipt in enumerate(deliveries, 1):
            latest_event = max((event_positions[item] for item in receipt["item_ids"]), default=0)
            required_ack = 0
            if receipt["kind"] == "wake_hold_receipt":
                # A hold accepts any matching digest occurrence that covers
                # all its named events, unlike a prepared cut's first match.
                positions = event_digests[receipt["event_prefix_digest"]]
                latest_event = positions[bisect_left(positions, latest_event)]
                required_ack = acknowledgment_digests[receipt["acknowledgment_prefix_digest"]]
            self.delivery_events.append(max(self.delivery_events[-1], latest_event))
            self.delivery_acks.append(max(self.delivery_acks[-1], required_ack))
            for item in receipt["item_ids"]:
                first_delivery.setdefault(item, ordinal)
        self.ack_deliveries = [0]
        for ordinal, receipt in enumerate(acknowledgments, 1):
            self.ack_deliveries.append(max(self.ack_deliveries[-1],
                max(first_delivery[item] for item in receipt["item_ids"])))
            for item in receipt["item_ids"]:
                self.acknowledged.setdefault(item, ordinal)

    @staticmethod
    def source_binding(source, node, tenant):
        try:
            if len(source) != 6 or any(not source[axis] for axis in (1, 3, 5)):
                return None
            return (tenant, node, tuple(len(source[axis]) for axis in (0, 2, 4)),
                    tuple(source[axis][-1] for axis in (1, 3, 5)))
        except (IndexError, KeyError, TypeError):
            return None

    def _dependencies_admit(self, counts):
        if not self._indexed:
            return False
        if any(type(count) is not int or not 0 <= count <= maximum
               for count, maximum in zip(counts, self._indexed_counts)):
            return False
        events, deliveries, acknowledgments = counts
        return (self.delivery_events[deliveries] <= events
                and self.delivery_acks[deliveries] <= acknowledgments
                and self.ack_deliveries[acknowledgments] <= deliveries)

    def _native(self, counts):
        if self._replay is not None and counts in self._replay_cache:
            return self._replay_cache[counts]
        cut = []
        for axis, count in enumerate(counts):
            cut.extend((self.source[axis * 2][:count], self.source[axis * 2 + 1][:count + 1]))
        if self._replay is not None:
            result = self._replay(tuple(cut))
            self._replay_cache[counts] = result
            return result
        proofs = None
        if self.root is not None:
            from .epoch_carry import verified_hold_proofs
            proofs = verified_hold_proofs(self.root, cut, self.node,
                home=self.home, resolver=self.resolver)
        return project_wake_items(events=cut[0], deliveries=cut[2], acknowledgments=cut[4],
            recipient=self.node, worker_session_id=None, tenant_id=self.tenant,
            event_prefix_digests=cut[1], delivery_prefix_digests=cut[3],
            acknowledgment_prefix_digests=cut[5], _hold_proofs=proofs)

    def eligible(self, counts, item_ids):
        events, deliveries, acknowledgments = counts
        if not self._dependencies_admit(counts):
            # Preserve the native refusal and its detail on inconsistent cuts.
            if self._replay is not None:
                # Cold pool sources are immutable for this invocation. Cache
                # only successful exact-cut validation, before any reslicing.
                if counts not in self._eligible_cache:
                    self._eligible_cache[counts] = {item.message["id"]: ordinal
                        for ordinal, item in enumerate(self._native(counts))
                        if item.state in {"fresh", "held"}}
                positions = self._eligible_cache[counts]
                return sorted((item for item in set(item_ids) if item in positions),
                              key=positions.__getitem__)
            owned = [item.message["id"] for item in self._native(counts)
                if item.state in {"fresh", "held"}]
            return [item for item in owned if item in item_ids]
        infinity = float("inf")
        return sorted((item for item in set(item_ids) if item in self.messages
            and self.messages[item] <= events
            and self.retracted.get(item, infinity) > events
            and self.acknowledged.get(item, infinity) > acknowledgments), key=self.messages.__getitem__)



def _presentation_witness_data(state, data, tenant, node):
    fields = {"schema_version", "tenant_id", "node_id", "genesis_id", "before_bytes", "before_digest", "reserved_record"}
    import re
    if not isinstance(state, dict) or set(state) != fields or (
        type(state["schema_version"]) is not int or state["schema_version"] != 1
        or state["tenant_id"] != tenant or state["node_id"] != node
        or not isinstance(state["genesis_id"], str) or re.fullmatch(r"genesis-[0-9a-f]{32}", state["genesis_id"]) is None
        or type(state["before_bytes"]) is not int or not 0 <= state["before_bytes"] <= MAX_LEDGER_BYTES
        or not isinstance(state["before_digest"], str) or re.fullmatch(r"[0-9a-f]{64}", state["before_digest"]) is None
    ):
        raise _failure("presentation witness has invalid closed identity or prefix fields")
    if data is None or len(data) < state["before_bytes"] or _digest(data[:state["before_bytes"]]) != state["before_digest"]:
        raise _failure("initialized presentation history is missing, truncated or changed")
    reserved = state["reserved_record"]
    if reserved is not None:
        validate_record(reserved, tenant, KINDS, integrity=True)
        if reserved["node_id"] != node:
            raise _failure("reserved presentation belongs to another node")
        expected = encode_frame(reserved)
        tail = data[state["before_bytes"]:]
        if tail and tail != expected:
            raise _failure("presentation tail conflicts with its write-ahead witness")
        if not tail and reserved["phase"] == "prepared":
            # A crash before append cannot erase the durable reservation.
            # Materialize only that exact reserved prepare; never output it.
            data += expected
    elif len(data) != state["before_bytes"]:
        raise _failure("presentation append has no native write-ahead witness")
    return state, data


def _presentation_groups(rows, node):
    groups = {}
    for row in rows:
        if row["node_id"] != node:
            raise _failure("presentation ledger contains another node")
        batch = groups.setdefault(row["batch_id"], [])
        if row["phase"] == "prepared":
            if batch or row["id"] != row["batch_id"]:
                raise _failure("presentation batch has duplicate or conflicting preparation")
        elif len(batch) != 1 or any(row[key] != batch[0][key] for key in _IMMUTABLE):
            raise _failure("presentation terminal has no exact unmatched preparation")
        batch.append(row)
    return groups


def _presentation_cuts(groups, epochs):
    from .cut_prefix import CertifiedPrefixes
    positions = {}
    for epoch, source in epochs.items():
        axes = []
        for index in (1, 3, 5):
            if type(source[index]) is CertifiedPrefixes:
                axes.append({digest: ordinal for digest, ordinal in source[0].cuts.items()
                    if ordinal is not None})
                continue
            first = {}
            for ordinal, digest in enumerate(source[index]):
                first.setdefault(digest, ordinal)
            axes.append(first)
        positions[epoch] = axes
    cuts, limits = {}, {}
    for key, batch in groups.items():
        prepared = batch[0]
        epoch = prepared["epoch_anchor"]
        if epoch not in epochs:
            raise _failure("presentation epoch is outside verified native lineage")
        try:
            counts = tuple(positions[epoch][axis][prepared[field]] for axis, field in enumerate((
                "event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest")))
        except KeyError as exc:
            raise _failure("presentation input prefix is not native evidence") from exc
        cuts[key] = counts
        limits[epoch] = tuple(max(old, new) for old, new in zip(limits.get(epoch, (0, 0, 0)), counts))

    return cuts, limits


class _SelectedPresentationCuts(_HistoricalCuts):
    """Index historical dependencies after native projection of this exact selected source."""

    def __init__(self, source):
        from .send_prefix import CacheMiss
        events, _, deliveries, _, acks, ap = source
        self._replay = None
        self._indexed = True
        self._indexed_counts = len(events), len(deliveries), len(acks)
        self.messages = {}
        self.retracted, self.acknowledged = {}, {}
        for ordinal, row, removed in events.rows:
            item = row["id"]
            self.messages[item] = ordinal
            if removed:
                position = events.inbox.get("retracted-at", item)
                if type(position) is not int or not ordinal < position <= len(events):
                    raise CacheMiss()
                self.retracted[item] = position
        if self.retracted:
            events.assert_current()
        event_digests = {digest: [ordinal] for digest, ordinal in events.cuts.items()
            if ordinal is not None}
        self._index_receipts(self.messages, event_digests, deliveries, acks, ap)

    def _native(self, counts):
        # An inconsistent joint cut receives its original native refusal.
        from .send_prefix import CacheMiss
        raise CacheMiss()


def _validate_presentation_groups(groups, epochs, cuts, history, consents, claims, node):
    results = []
    # Preserve the native first match while avoiding repeated full-ledger scans.
    consent_ids, claim_ids = {}, {}
    for row in consents:
        consent_ids.setdefault(row["id"], row)
    for row in claims:
        claim_ids.setdefault(row["id"], row)
    claimed = set()
    for key, batch in groups.items():
        prepared = batch[0]
        consent = consent_ids.get(prepared["consent_receipt_id"])
        claim = claim_ids.get(prepared["claim_receipt_id"])
        if consent is None or consent["node_id"] != node or consent["workspace"] != prepared["workspace"] or (
            consent["state"] != "armed" or (claim is not None and (
                claim["node_id"] != node or claim["workspace"] != prepared["workspace"]
                or claim["acting_session_id"] != prepared["acting_session_id"]
                or claim["consent_receipt_id"] != consent["id"]
            )) or (prepared["claim_receipt_id"] is not None and claim is None)
        ):
            raise _failure("presentation does not reproduce its native consent and claim")
        source = epochs.get(prepared["epoch_anchor"])
        if source is None:
            raise _failure("presentation epoch is outside verified native lineage")
        selected = history[prepared["epoch_anchor"]].eligible(cuts[key], prepared["item_ids"])
        if selected != prepared["item_ids"]:
            raise _failure("presentation references foreign, retracted, ACKed or reordered envelopes")
        identity = (prepared["epoch_anchor"], prepared["acting_session_id"])
        if any((identity, item) in claimed for item in selected):
            raise _failure("presentation exceeds one automatic attempt per conversation and epoch")
        claimed.update((identity, item) for item in selected)
        delivery = None
        if len(batch) == 2:
            offset = cuts[key][1]
            delivery = source[2][offset] if offset < len(source[2]) else None
            if delivery is None or delivery.get("id") != batch[1]["delivery_receipt_id"] or (
                delivery.get("kind") not in ("delivery_receipt", "delivered_unpresented") or delivery.get("recipient") != node
                or delivery.get("item_ids") != selected or delivery.get("worker_session_id") is not None
            ):
                raise _failure("flushed presentation lacks its exact native delivery receipt")
        results.append({"records": batch, "consent": consent, "claim": claim,
                        "counts": list(cuts[key]), "eligible_ids": selected, "following_delivery": delivery})
    return results


class _SourcePrefix:
    """Invocation-local testimony for one fully decoded physical cut."""

    def __init__(self, contract, data, value, digest, *, physical_ids, physical_count, append_eligible,
                 admitted_rows=None):
        self.contract, self.data, self.value, self.digest = contract, data, value, digest
        self.physical_digest = _digest(data)
        self.ids, self.physical_count = frozenset(physical_ids), physical_count
        self.append_eligible = append_eligible
        # Exact-byte hits preserve the existing exposed-row cache behavior.
        # Changed cuts must extend privately admitted objects: public source
        # rows (including nested lists/dicts) may have been mutated by a caller.
        self._admitted_rows = _clone_native(value[0]) if admitted_rows is None else admitted_rows


class CursorPresentationLedger:
    def __init__(self, root, *, sealed="verify", _epoch_resolver=None, _authority_reader=None):
        if sealed not in ("verify", "bytes"):
            raise ValueError("sealed must be bytes or verify")
        self.root = root
        self.sealed = sealed
        self._epoch_resolver = _epoch_resolver
        self._authority_reader = _authority_reader
        self._epoch_homes = {}
        self._paths = StopHoldLedger(root)
        self._source_cache = {}
        self._source_absent = set()
        self._history_cache = None
        self._cut_cache = {}
        self._current_tuple = None
        self._byte_memo = {}
        self._load_memo = None
        self._selection_memo = None
        self._controller_memo = None
        self._waiter_states = None
        self._waiter_projection = None
        self._waiter_contract = None
        self._borrow_active = False
        self._current_append_proven = False
        self._pending_source_prefixes = None
        self._pending_source_lineage = None
        self._admission_lineage = None
        self._waiter_completed_load = None
        self._waiter_derived = None

    @staticmethod
    def relative(node):
        from .root import validate_identifier
        return Path("receipts/cursor-presentations") / (validate_identifier(node, "node") + ".jsonl")

    @staticmethod
    def witness_relative(node):
        from .root import validate_identifier
        return Path("state/cursor-wait") / validate_identifier(node, "node") / "presentation-history.v1.json"

    def _memo_semantics(self):
        from .memo_key_closure import projection_binding_snapshot
        from .epoch_carry import ArchiveProofResolver
        from .epoch_prefix import _projection_contract
        # Bindings are derived; these call-time contracts also carry values
        # that a code snapshot cannot observe for this root.
        return (projection_binding_snapshot(), _projection_contract(),
            ArchiveProofResolver(self.root)._source_contract())

    def _coordinate_identity(self, relative):
        """Read only guarded identities; failure never supplies native refusal."""
        path, parents = self._paths._path(relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            leaf = None
        else:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise _failure("memo coordinate needs native readmission")
            leaf = tuple(getattr(info, name) for name in (
                "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))
        return leaf, tuple((str(parent), device, inode) for parent, device, inode in parents if parent != path)

    def _memo_dependencies(self, node, *, worker_session_id=None, controller=False):
        from .cursor import SparseCursor
        from .codex_wait_contract import CodexWaitSessionLedger, consent_ledger_relative, WORKSPACE_MAP_RELATIVE
        from .bus_epoch import observe_epoch_roll_state
        cursor = SparseCursor(self.root)
        coordinates = [Path("events.jsonl"),
            cursor._delivery_relative_path_for(node, worker_session_id=worker_session_id),
            cursor._relative_path_for(node, worker_session_id=worker_session_id)]
        if not controller:
            coordinates.extend((self.relative(node), self.witness_relative(node),
                consent_ledger_relative(node), CodexWaitSessionLedger._relative(node),
                WORKSPACE_MAP_RELATIVE, Path("receipts/wake-control") / (node + ".jsonl")))
        identities = tuple((str(path), self._coordinate_identity(Path(path))) for path in coordinates)
        # A marker or changed receipt-owned namespace invalidates silently;
        # the native selected read then chooses its original error precedence.
        transition = observe_epoch_roll_state(self.root)
        if transition["classification"] != "absent":
            raise _failure("memo epoch requires native readmission")
        archives = ()
        if not controller and self._epoch_resolver is not None:
            archives = tuple(self._epoch_resolver.archives())
        return self._memo_semantics(), identities, archives

    def _memo_inputs_unchanged(self, node):
        try:
            contract = self._memo_semantics()
            if self._waiter_contract != contract:
                self._discard_waiter_admission()
                self._waiter_contract = contract
            memo = self._load_memo
            lineage = self._admission_lineage
            if lineage is None or lineage[0] != node:
                self._discard_waiter_admission()
                return False
            current = self._memo_dependencies(node)
            if memo is not None and memo[0] == node and memo[1] == current:
                return True
            if lineage[1] == current or self._monotone_input_growth(lineage[1], current):
                # Initial witness creation can require a native load even for
                # unchanged inputs. In either case the actual protected reads
                # must still match the OLD admission's lineage and full bytes,
                # not merely the advisory metadata observed just above.
                self._pending_source_prefixes = lineage[2]
                self._pending_source_lineage = lineage[1][1][:3]
            else:
                self._discard_waiter_admission()
            return False
        except Exception:
            # An observed absence, invalid parent, changed archive or uncertain
            # observation ends the old admission even if it later reappears.
            self._discard_waiter_admission()
            return False

    def _discard_waiter_admission(self, *, clear_sources=True):
        self._load_memo = self._selection_memo = self._controller_memo = None
        self._waiter_states = self._waiter_projection = None
        self._history_cache = self._current_tuple = None
        self._cut_cache.clear()
        self._pending_source_prefixes = None
        self._pending_source_lineage = None
        self._admission_lineage = None
        self._waiter_completed_load = self._waiter_derived = None
        self._current_append_proven = False
        if clear_sources:
            # An equal-byte decoder hit is not fresh admission of a replaced
            # inode, parent/root, lost input, changed mode or rewritten source.
            self._source_cache.clear()
            self._source_absent.clear()
            self._byte_memo.clear()

    def _monotone_input_growth(self, before, after):
        if (before[0] != after[0] or before[2] != after[2]
                or len(before[1]) != len(after[1]) or before[1][3:] != after[1][3:]):
            return False
        return before[1][:3] != after[1][:3] and self._source_lineage_continues(before[1][:3], after[1][:3])

    @staticmethod
    def _source_lineage_continues(before, after):
        if before is None or len(before) != 3 or len(after) != 3:
            return False
        for (old_name, old_identity), (name, identity) in zip(before, after):
            if old_identity is None or identity is None:
                return False
            old_leaf, old_parents = old_identity
            leaf, parents = identity
            if old_name != name or old_parents != parents:
                return False
            if old_leaf == leaf:
                continue
            if leaf is None:
                return False
            if old_leaf is None:
                # First native creation of a previously observed absent axis
                # is allowed only under the exact existing parent lineage.
                if leaf[4] <= 0:
                    return False
            elif old_leaf[:4] != leaf[:4] or leaf[4] <= old_leaf[4]:
                # Device/inode/mode/link identity must stay exact, and length
                # must strictly increase. Equal-sized rewrites are not append.
                return False
        return True

    def _waiter_controller_read(self, native_read, *, recipient, worker_session_id):
        try:
            before = self._memo_dependencies(recipient, worker_session_id=worker_session_id, controller=True)
        except Exception:
            before = None
        memo = self._controller_memo
        if before is not None and memo is not None and memo[:3] == (recipient, worker_session_id, before):
            return _clone_native(memo[3])
        self._controller_memo = None
        value = native_read()  # This distinct native reader owns its refusals.
        try:
            after = self._memo_dependencies(recipient, worker_session_id=worker_session_id, controller=True)
        except Exception:
            after = None
        if before is not None and after is not None and self._controller_dependencies_match(before, after):
            self._controller_memo = recipient, worker_session_id, after, _clone_native(value)
        return value

    @staticmethod
    def _controller_dependencies_match(before, after):
        if before[0] != after[0] or before[2] != after[2] or len(before[1]) != len(after[1]):
            return False
        for (old_name, (old_leaf, old_parents)), (name, (leaf, parents)) in zip(before[1], after[1]):
            if old_name != name or old_leaf != leaf:
                return False
            if old_parents != parents and not (leaf is None and parents[:len(old_parents)] == old_parents):
                return False
        return True

    def _load_admission_matches(self, before, after, node, *, sources_only=False):
        if before is None or before[0] != after[0]:
            return False
        # Consent/claim are read through their native readers. Keep their
        # before/after coordinates exact. The first five inputs have stronger
        # protected descriptor testimony from this actual load, so first-time
        # creation of an absent axis's lock directory need not waste admission.
        if before[1][5:] != after[1][5:]:
            return False
        for name, identity in after[1][:(3 if sources_only else 5)]:
            relative = Path(name)
            maximum = 131072 if relative == self.witness_relative(node) else MAX_LEDGER_BYTES
            saved = self._byte_memo.get((relative, maximum))
            if saved is None or (saved[0], saved[3]) != identity:
                return False
        return True

    @contextmanager
    def _parent_fd(self, relative, *, create=False):
        descriptors = []
        try:
            descriptor = os.open(self.root.tenant_home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            for part in relative.parent.parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                descriptors.append(descriptor)
            yield descriptor
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _bytes(self, relative, maximum=MAX_LEDGER_BYTES, *, _borrow=False):
        path, parents = self._paths._path(relative)
        try:
            with self._parent_fd(relative) as parent:
                fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            if _borrow:
                self._byte_memo[(relative, maximum)] = (None, None, None,
                    tuple((str(parent), device, inode) for parent, device, inode in parents if parent != path))
            return None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid():
                raise _failure("presentation coordinate must be a singly linked account-owned ordinary file")
            if before.st_size > maximum:
                raise _failure("presentation input exceeds its byte bound")
            fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
            identity = tuple(getattr(before, field) for field in fields)
            saved = self._byte_memo.get((relative, maximum)) if _borrow else None
            if saved is not None and saved[0] == identity:
                data = saved[1]
            else:
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(maximum + 1)
            after = os.fstat(fd)
            if any(getattr(before, field) != getattr(after, field) for field in fields) or len(data) != after.st_size:
                raise _failure("presentation input changed during observation")
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
                raise _failure("presentation input identity changed")
            for parent, device, inode in parents:
                current = parent.lstat()
                if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (device, inode):
                    raise _failure("presentation input parent changed")
            if _borrow:
                # Retained bytes cover physical unknown frames too. No digest
                # of projected known rows can stand in for this full input.
                self._byte_memo[(relative, maximum)] = (identity, data, _digest(data),
                    tuple((str(parent), device, inode) for parent, device, inode in parents if parent != path))
            return data
        finally:
            os.close(fd)

    def _save_witness(self, node, state):
        relative = self.witness_relative(node)
        path, _ = self._paths._path(relative)
        content = (json.dumps(state, sort_keys=True) + "\n").encode("utf-8")
        with self._parent_fd(relative, create=True) as parent:
            temporary = ".presentation-" + uuid7_hex()
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._paths._path(relative)
                expected_parent = os.fstat(parent)
                observed_parent = path.parent.lstat()
                if (expected_parent.st_dev, expected_parent.st_ino) != (observed_parent.st_dev, observed_parent.st_ino):
                    raise _failure("presentation witness parent changed before publication")
                os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                if self._bytes(relative, 131072) != content:
                    raise _failure("presentation witness readback differs")
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _witness(self, node, data, *, materialize=True, _borrow=False):
        raw = self._bytes(self.witness_relative(node), 131072, _borrow=_borrow)
        if raw is None:
            if data is not None:
                raise _failure("presentation ledger exists without its initialization witness")
            if not materialize:
                raise _failure("read-only presentation history was not initialized")
            relative = self.relative(node)
            path, _ = self._paths._path(relative)
            with self._parent_fd(relative, create=True) as parent:
                fd = os.open(relative.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                try:
                    os.fsync(fd)
                    os.fsync(parent)
                finally:
                    os.close(fd)
            state = {"schema_version": 1, "tenant_id": self.root.tenant_id, "node_id": node,
                "genesis_id": "genesis-" + uuid7_hex(), "before_bytes": 0,
                "before_digest": _digest(b""), "reserved_record": None}
            self._save_witness(node, state)
            return state, b""
        try:
            state = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise _failure("presentation witness is malformed") from exc
        effective_state, effective_data = _presentation_witness_data(state, data, self.root.tenant_id, node)
        if materialize and len(effective_data) != len(data):
            self._append_reserved(node, data, state["reserved_record"])
        return effective_state, effective_data

    def _append_reserved(self, node, data, row):
        """One native frame under the caller's physical node/epoch guards.

        Every producer and authoritative reader of this new plane shares the
        node guard. Keep a retained parent/leaf descriptor through append;
        the generic pathname JSONL writer cannot supply this confinement.
        """
        relative = self.relative(node)
        validate_record(row, self.root.tenant_id, KINDS, integrity=False)
        prior = _decode_path_records(self.root.tenant_home / relative, self.root.tenant_id, KINDS, data)
        self._groups(prior + [row], node)
        encoded = encode_frame(row)
        if len(encoded) > MAX_RECORD_BYTES or len(data) + len(encoded) > MAX_LEDGER_BYTES or len(prior) >= MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("presentation_ledger_limit", "presentation append exceeds native framing bounds",
                remedy="preserve the exact history and request governed ledger recovery")
        if self._bytes(relative) != data:
            raise _failure("presentation prefix changed before append")
        with self._parent_fd(relative) as parent:
            fd = os.open(relative.name, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, dir_fd=parent)
            try:
                identity = os.fstat(fd)
                current = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1 or identity.st_uid != os.getuid() or (
                    identity.st_size != len(data) or (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino)
                ):
                    raise _failure("presentation append no longer owns the verified ordinary prefix")
                observed = bytearray()
                while len(observed) <= len(data):
                    chunk = os.read(fd, min(65536, len(data) + 1 - len(observed)))
                    if not chunk:
                        break
                    observed.extend(chunk)
                verified = os.fstat(fd)
                if bytes(observed) != data or any(getattr(identity, field) != getattr(verified, field)
                        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
                    raise _failure("presentation retained append prefix changed before write")
                written = os.write(fd, encoded)
                if written != len(encoded):
                    os.ftruncate(fd, len(data))
                    os.fsync(fd)
                    raise DurabilityFailure("short_write", "presentation frame was only partially appended")
                os.fsync(fd)
                os.fsync(parent)
                current = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino) or self._bytes(relative) != data + encoded:
                    raise _failure("presentation append lost its verified path or readback")
            finally:
                os.close(fd)
        from .wake_hints import publish_recipient_hint
        publish_recipient_hint(self.root, node)
        return row

    def _groups(self, rows, node):
        return _presentation_groups(rows, node)

    def _source_contract(self, path, kinds, domain, events):
        from . import jsonl
        from .epoch_prefix import _projection_contract
        # A changed parser, validation vocabulary, bound or digest domain cannot
        # inherit testimony from a different decoding contract.
        return (path, self.root.tenant_id, frozenset(kinds), domain, events,
            jsonl.MAX_LEDGER_BYTES, jsonl.MAX_LEDGER_RECORDS, jsonl.MAX_RECORD_BYTES,
            tuple(sorted((getattr(_decode_path_records, "__kwdefaults__", None) or {}).items())),
            _decode_path_records, jsonl._decode_path_records, jsonl._decode_bounded_frames, jsonl.validate_record,
            jsonl.is_known_record_kind, jsonl.validate_unknown_record,
            decode_frames, jsonl.decode_frames, encode_frame, VerifiedLedgerCursor._initial_digest,
            _projection_contract())

    def _decode_source(self, path, kinds, domain, data, cached, *, events):
        from . import jsonl
        contract = self._source_contract(path, kinds, domain, events)
        if cached is not None and cached.contract == contract:
            if data == cached.data:
                return cached.value, cached
            if (cached.append_eligible and len(data) > len(cached.data) and data.startswith(cached.data)
                    and (not cached.data or cached.data.endswith(b"\n"))
                    and len(data) <= jsonl.MAX_LEDGER_BYTES):
                suffix = data[len(cached.data):]
                try:
                    unknown = {} if events else None
                    added = _decode_path_records(path, self.root.tenant_id, frozenset(kinds), suffix,
                        unrecognized=unknown)
                    frames = suffix.splitlines(keepends=True)
                    ids = frozenset(row["id"] for row in added)
                    # Unknown frames affect physical counts and duplicate IDs
                    # even when omitted from the event projection. A roll at
                    # suffix record one is not physical record one. Replay both
                    # cases with the original whole-ledger parser below.
                    if (not unknown and len(frames) == len(added)
                            and cached.physical_count + len(frames) <= jsonl.MAX_LEDGER_RECORDS
                            and not cached.ids.intersection(ids)
                            and not any(row["kind"] == "bus_epoch_roll_receipt" for row in added)
                            and all(frame == encode_frame(row) for frame, row in zip(frames, added))):
                        digest = cached.digest.copy()
                        prefixes = list(cached.value[1])
                        for frame in frames:
                            digest.update(frame)
                            prefixes.append(digest.hexdigest())
                        admitted = cached._admitted_rows + added
                        value = _clone_native(admitted), tuple(prefixes)
                        return value, _SourcePrefix(contract, data, value, digest,
                            physical_ids=cached.ids.union(ids),
                            physical_count=cached.physical_count + len(frames), append_eligible=True,
                            admitted_rows=admitted)
                except Exception:
                    # An unproven suffix never supplies the public refusal.
                    # Whole replay retains native error precedence, absolute
                    # physical line numbers and installed-reader testimony.
                    pass
        unknown = {} if events else None
        rows = _decode_path_records(path, self.root.tenant_id, frozenset(kinds), data,
            unrecognized=unknown)
        frames = data.splitlines(keepends=True)
        physical_count = len(frames)
        physical_ids = (row["id"] for row in rows)
        if unknown:
            from .records import is_known_record_kind
            physical = decode_frames(data)
            physical_ids = (row["id"] for row in physical)
            frames = [frame for frame, raw in zip(frames, physical) if is_known_record_kind(raw["kind"])]
        if len(frames) != len(rows) or any(frame != encode_frame(row) for frame, row in zip(frames, rows)):
            raise _failure("presentation source is not native canonical framing")
        digest = VerifiedLedgerCursor._initial_digest(domain)
        prefixes = [digest.hexdigest()]
        for frame in frames:
            digest.update(frame)
            prefixes.append(digest.hexdigest())
        value = rows, tuple(prefixes)
        # Preserve the existing exact-byte hit, including unknown vocabulary.
        # A changed cut containing any unknown kind always takes whole replay.
        return value, _SourcePrefix(contract, data, value, digest,
            physical_ids=physical_ids, physical_count=physical_count, append_eligible=not unknown)

    def _sources(self, home, node, *, _borrow=False, selected_events=None):
        if (home == self.root.tenant_home and self._epoch_resolver is not None
                and self._epoch_resolver.frozen is not None):
            return self._epoch_resolver.source(home, node)
        from .cursor import SparseCursor
        coordinates = ((Path("events.jsonl"), EVENT_KINDS, WakeHoldController._EVENT_DOMAIN),
            (SparseCursor(self.root)._delivery_relative_path_for(node), WAKE_HOLD_KINDS, WakeHoldController._DELIVERY_DOMAIN),
            (SparseCursor(self.root)._relative_path_for(node), {"ack_receipt"}, WakeHoldController._ACK_DOMAIN))
        result, pending, append_proven, observed = [], {}, True, []
        if selected_events is not None:
            from .cut_prefix import CertifiedPrefixes
            result.extend((selected_events, CertifiedPrefixes(selected_events)))
            coordinates = coordinates[1:]
        for relative, kinds, domain in coordinates:
            owned_relative = home.relative_to(self.root.tenant_home) / relative
            if home == self.root.tenant_home:
                from .jsonl import _lock_beside, _locked_path
                path, _ = self._paths._path(owned_relative)
                lock_path, lock_relative = _lock_beside(path, owned_relative)
                self._paths._path(Path(lock_relative))
                with _locked_path(lock_path, exclusive=False, relative=lock_relative,
                        _open_handle=lambda: self._lock_handle(Path(lock_relative))):
                    data = self._bytes(owned_relative, _borrow=_borrow)
            else:
                if home.is_file() and not home.is_symlink():
                    from .epoch_history import read_archived_bytes
                    data = read_archived_bytes(home, relative.as_posix())
                else:
                    data = self._bytes(owned_relative, _borrow=_borrow)
            if _borrow and home == self.root.tenant_home:
                # _bytes has just checked this descriptor, its named inode
                # and parents. Keep that testimony, not a new path preflight.
                saved = self._byte_memo.get((owned_relative, MAX_LEDGER_BYTES))
                observed.append((str(owned_relative), None if saved is None else (saved[0], saved[3])))
            if data is None:
                append_proven = append_proven and str(owned_relative) not in self._source_cache
                pending[str(owned_relative)] = None
                result.extend(([], (VerifiedLedgerCursor._initial_digest(domain).hexdigest(),)))
                continue
            cached = self._source_cache.get(str(owned_relative))
            value, pending[str(owned_relative)] = self._decode_source(home / relative, kinds, domain, data, cached,
                events=relative == Path("events.jsonl"))
            append_proven = append_proven and (
                (cached is None and str(owned_relative) in self._source_absent
                    and pending[str(owned_relative)].append_eligible)
                or (cached is not None and cached.contract == pending[str(owned_relative)].contract
                    and cached.append_eligible and pending[str(owned_relative)].append_eligible
                    and data.startswith(cached.data)
                    and _digest(data[:len(cached.data)]) == cached.physical_digest))
            if _borrow:
                # Exact-byte public hits may expose caller-mutated value rows.
                # Borrow only the detached rows captured at native admission.
                value = pending[str(owned_relative)]._admitted_rows, value[1]
            result.extend(value)
        if selected_events is not None:
            # The remaining axes used the same guarded descriptors, canonical
            # frame decoder and digest domains as full native replay. Keep
            # this cut separate from the complete-row append memo.
            selected_events.prepare_cuts(result[2])
            return tuple(result)
        validate_event_records(result[0])
        if _borrow and home == self.root.tenant_home:
            self._current_append_proven = append_proven
        for key, cached in pending.items():
            if cached is None:
                self._source_cache.pop(key, None)
                self._source_absent.add(key)
            else:
                self._source_cache[key] = cached
                self._source_absent.discard(key)
        if _borrow and home == self.root.tenant_home and self._pending_source_prefixes is not None:
            if (not self._source_lineage_continues(self._pending_source_lineage, observed)
                    or not append_proven or not self._projection_physical_prefix(self._pending_source_prefixes, node)):
                # A replacement after metadata preflight can have identical
                # bytes; a larger inode can contain a middle-prefix rewrite.
                # Both end old history/current admission before it is reused.
                # Native decoding already owns failures and the current bytes.
                self._discard_waiter_admission(clear_sources=False)
            self._pending_source_prefixes = None
            self._pending_source_lineage = None
        return tuple(result)

    @contextmanager
    def _lock_handle(self, relative):
        try:
            path, parents = self._paths._path(relative)
            with self._parent_fd(relative, create=True) as parent:
                fd = os.open(relative.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600, dir_fd=parent)
                with os.fdopen(fd, "a+b") as handle:
                    identity = os.fstat(fd)
                    current = path.lstat()
                    if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1 or identity.st_uid != os.getuid() or (
                            identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                        raise _failure("presentation source lock lost its ordinary coordinate")
                    for ancestor, device, inode in parents:
                        current = ancestor.lstat()
                        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (device, inode):
                            raise _failure("presentation source lock parent changed")
                    yield handle
        except OSError as exc:
            raise _failure("presentation source lock coordinate became unavailable") from exc

    def _epochs(self, node, genesis, required_anchors=()):
        from .bus_epoch import observe_epoch_roll_state
        from .epoch_carry import ArchiveProofResolver
        if observe_epoch_roll_state(self.root)["classification"] != "absent":
            raise _failure("native epoch transition is unresolved")
        # Preserve the native event-reader refusal before interpreting lineage.
        current_source = self._sources(self.root.tenant_home, node, _borrow=self._borrow_active)
        if self._epoch_resolver is None or self._epoch_resolver.frozen is None:
            self._epoch_resolver = ArchiveProofResolver(self.root,
                head=current_source[0][0] if current_source[0] else None)
        archived = self._epoch_resolver.epoch_homes(genesis) if (
            current_source[0] and current_source[0][0]["kind"] == "bus_epoch_roll_receipt"
        ) else {}
        current_anchor = str(current_source[0][0]["id"]) if (
            current_source[0] and current_source[0][0]["kind"] == "bus_epoch_roll_receipt") else genesis
        if current_anchor in archived:
            raise _failure("native epoch lineage has duplicate anchors")
        # Native archived cuts are needed only by retained presentations;
        # old completed mail never joins the current wake denominator.
        needed = set(required_anchors) - {current_anchor}
        if not needed.issubset(archived):
            raise _failure("presentation epoch is outside verified native lineage")
        homes = (self.root.tenant_home, *(home for anchor, home in archived.items()
            if anchor in needed and self.sealed == "verify"))
        epochs = {}
        self._epoch_homes = dict(archived)
        for home in homes:
            source = current_source if home == self.root.tenant_home else self._sources(home, node, _borrow=self._borrow_active)
            events = source[0]
            anchor = str(events[0]["id"]) if events and events[0]["kind"] == "bus_epoch_roll_receipt" else genesis
            if anchor in epochs:
                raise _failure("native epoch lineage has duplicate anchors")
            epochs[anchor] = source
            self._epoch_homes[anchor] = home
        return next(iter(epochs)), epochs

    @staticmethod
    def _states(source, node, tenant, _hold_proofs=None):
        events, ep, deliveries, dp, acknowledgments, ap = source
        return project_wake_items(events=events, deliveries=deliveries, acknowledgments=acknowledgments,
            recipient=node, worker_session_id=None, tenant_id=tenant,
            event_prefix_digests=ep, delivery_prefix_digests=dp, acknowledgment_prefix_digests=ap,
            _hold_proofs=_hold_proofs)

    def _current_states(self, source, node, anchor, *, _borrow=False):
        from .epoch_carry import verified_hold_proofs
        if _borrow:
            if self._waiter_states is not None and self._waiter_states[0] is source:
                return self._waiter_states[1]
            derived = self._waiter_derived
            if (derived is not None and self._load_memo is not None
                    and self._load_memo[2][5][anchor] is source
                    and self._foreign_send_extension(source, node, anchor, derived[0])):
                # This reuses successful derived states, never an old sealed
                # proof for a different full source. Relevant/uncertain tails
                # still reach native proof issuance below at its old point.
                return self._retain_waiter_states(source, node, anchor, derived[1])
            previous = self._waiter_projection
            if (previous is not None and previous[0] == anchor and self._current_append_proven
                    and self._projection_physical_prefix(previous[2], node)):
                projection = previous[1]
                if projection.extend(source, previous_counts=projection._limits):
                    states = projection.project(tuple(len(source[axis]) for axis in (0, 2, 4)))
                    if states is not None:
                        self._waiter_states = source, states
                        self._waiter_projection = anchor, projection, self._current_physical_sources(node)
                        return states
            binding = _HistoricalCuts.source_binding(source, node, self.root.tenant_id)
            for historical_key, index in self._cut_cache.items():
                if (historical_key[:2] == (node, anchor) and index.full_states is not None
                        and index.full_binding == binding and _same_projection_rows(source, index.full_rows)):
                    # _load completed ALL history checks before this seam. Its
                    # exact full-source native proof can now be retained.
                    return self._retain_waiter_states(source, node, anchor, index.full_states)
        proofs = verified_hold_proofs(self.root, source, node,
            home=self._epoch_homes.get(anchor, self.root.tenant_home), resolver=self._epoch_resolver)
        binding = _HistoricalCuts.source_binding(source, node, self.root.tenant_id)
        if binding is None:
            return self._states(source, node, self.root.tenant_id, _hold_proofs=proofs)
        key = (anchor, binding)
        if self._current_tuple is not None:
            previous_key, states, admitted = self._current_tuple
            if previous_key == key and _same_projection_rows(source, admitted):
                return self._retain_waiter_states(source, node, anchor, states) if _borrow else _clone_native(states)
            if self._irrelevant_append(source, key, previous_key, admitted):
                # Prefix rows already match detached native testimony. Retain
                # only a detached new suffix and advance to the latest digest.
                old_events = len(admitted[0])
                admitted = (admitted[0] + tuple(_clone_native(row) for row in source[0][old_events:]),
                            admitted[1], admitted[2])
                self._current_tuple = key, states, admitted
                return self._retain_waiter_states(source, node, anchor, states) if _borrow else _clone_native(states)
        for historical_key, index in self._cut_cache.items():
            # A historical cache hit proves only its earlier prefixes. Its
            # speculative full tuple may refer to an older appended source.
            if (historical_key[:2] == (node, anchor)
                    and index.full_states is not None and index.full_binding == binding
                    and _same_projection_rows(source, index.full_rows)):
                states = index.full_states
                admitted = index.full_rows
                break
        else:
            # Failed speculation is never a cached refusal. Run the original
            # current projector at select's original point, after _load guards.
            states = self._states(source, node, self.root.tenant_id, _hold_proofs=proofs)
            admitted = _projection_rows(source)
        states = _clone_native(tuple(states))
        self._current_tuple = key, states, admitted
        if _borrow:
            return self._retain_waiter_states(source, node, anchor, states)
        return _clone_native(states)

    def _retain_waiter_states(self, source, node, anchor, states):
        from .epoch_prefix import _ArchivePrefixProjection
        states = _clone_native(tuple(states))
        self._waiter_states = source, states
        # Native projection already admitted this exact source. The prefix
        # helper still applies its supported vocabulary/imported-hold gates;
        # only its ordinal-index construction reuses that successful result.
        projection = _ArchivePrefixProjection(source, node, None, self.root.tenant_id,
            native_projector=lambda **_arguments: states)
        self._waiter_projection = (anchor, projection, self._current_physical_sources(node)) if projection._ready else None
        # Both direct current projection and exact full historical adoption
        # arrive here only after _load. Speculative history alone cannot admit
        # this exception, nor can a load whose protected dependencies changed.
        self._waiter_derived = None
        if (self._load_memo is not None and self._waiter_completed_load is not None
                and self._load_memo[2][5][anchor] is source):
            testimony = (anchor, _clone_native(source), self._current_physical_sources(node),
                self._load_memo[1], self._waiter_completed_load[5])
            self._waiter_derived = testimony, _clone_native(states)
        return states

    def _waiter_archive_snapshot(self):
        from .bus_epoch import _selected_member_identity
        self._epoch_resolver.archives()
        # Preserve native membership AND seven-field identities across resolver
        # replacement in _epochs. A list of unchanged path names is not enough.
        return tuple((path, tuple((name, plane, _selected_member_identity(status))
            for name, plane, status in snapshot)) for path, snapshot in self._epoch_resolver._paths)

    def _foreign_send_extension(self, source, node, anchor, previous):
        """Recognize only canonical ordinary foreign send pairs over a rolled base."""
        try:
            old_anchor, old_source, physical, dependencies, archives = previous
            old_events = len(old_source[0])
            if (anchor != old_anchor or not self._current_append_proven
                    or not old_events or old_source[0][0]["kind"] != "bus_epoch_roll_receipt"
                    or not any(row["kind"] == "wake_hold_receipt" and (
                        row["delivery_prefix_digest"] != old_source[3][ordinal]
                        or row["event_prefix_digest"] not in old_source[1]
                        or row["acknowledgment_prefix_digest"] not in old_source[5])
                        for ordinal, row in enumerate(old_source[2]))
                    or len(source[0]) <= old_events
                    or not _same_value(source[2:], old_source[2:])
                    or not _same_value(source[0][:old_events], old_source[0])
                    or not _same_value(source[1][:old_events + 1], old_source[1])):
                return False
            current = self._memo_dependencies(node)
            # The only changed coordinate may be strict events growth. Check
            # actual protected reads as well as the advisory path identities;
            # archives, history/witness and authority dependencies stay exact.
            if (not self._monotone_input_growth(dependencies, current)
                    or dependencies[1][1:] != current[1][1:]
                    or not self._load_admission_matches(dependencies, current, node)
                    or self._waiter_archive_snapshot() != archives
                    or not self._projection_physical_prefix(physical, node)):
                return False
            old = physical[0][1]
            new = self._current_physical_sources(node)[0][1]
            if (old is None or new is None or not _same_value(source[0], new._admitted_rows)
                    or len(source[1]) != len(source[0]) + 1):
                return False
            suffix = source[0][old_events:]
            if not suffix or len(suffix) % 2:
                return False
            kinds = frozenset({"message_envelope", "send_readiness_observation"})
            validated = [validate_record(dict(row), self.root.tenant_id, kinds, integrity=True) for row in suffix]
            # Original validators own the entire readiness schema and its
            # parties/key/time binding. No readiness is re-observed or treated
            # as present authority. The grammar here only narrows the suffix.
            validate_event_records(validated)
            seen = set(old.ids)
            for offset in range(0, len(validated), 2):
                envelope, observation = validated[offset:offset + 2]
                if (envelope["kind"] != "message_envelope"
                        or observation["kind"] != "send_readiness_observation"
                        or observation["schema_version"] != 1
                        or envelope["recipient"] == node
                        or envelope.get("attempt_binding") != "absent_legacy"
                        or {"worker_session_id", "reply_to"}.intersection(envelope)
                        or observation["envelope_id"] != envelope["id"]):
                    return False
                for row in (envelope, observation):
                    if row["id"] in seen:
                        return False
                    seen.add(row["id"])
            return (new.data == old.data + b"".join(encode_frame(row) for row in validated)
                    and new.physical_count == old.physical_count + len(validated))
        except Exception:
            # An uncertain optimization cannot supply or reorder a refusal.
            return False

    def _completed_history_extension(self, node, witness, cache_key, anchor, epochs):
        completed = self._waiter_completed_load
        if not self._borrow_active or completed is None or completed[0] != node:
            return False
        _, dependencies, loaded, physical, old_key, archives = completed
        old_witness, _, _, _, old_anchor, old_epochs = loaded
        if (self._history_cache is None or self._history_cache[0] != old_key
                or cache_key[:2] != old_key[:2] or cache_key[3:] != old_key[3:]
                or anchor != old_anchor or not _same_value(witness, old_witness)
                or tuple(epochs) != tuple(old_epochs)):
            return False
        # Exact complete archived sources are stronger than individual consumed
        # cuts; the current source must preserve its ENTIRE old physical prefix.
        # Thus every previously consumed cut and terminal delivery stays exact.
        if any(not _same_value(source, old_epochs[epoch]) for epoch, source in epochs.items() if epoch != anchor):
            return False
        return self._foreign_send_extension(epochs[anchor], node, anchor,
            (old_anchor, old_epochs[old_anchor], physical, dependencies, archives))

    def _current_physical_sources(self, node):
        from .cursor import SparseCursor
        cursor = SparseCursor(self.root)
        return tuple((str(path), self._source_cache.get(str(path))) for path in (
            Path("events.jsonl"), cursor._delivery_relative_path_for(node), cursor._relative_path_for(node)))

    def _projection_physical_prefix(self, previous, node):
        current = self._current_physical_sources(node)
        for (old_name, old), (name, new) in zip(previous, current):
            if old_name != name:
                return False
            if old is None:
                if new is not None and not new.append_eligible:
                    return False
                continue
            if (new is None or not old.append_eligible or not new.append_eligible
                    or old.contract != new.contract or not new.data.startswith(old.data)
                    or _digest(new.data[:len(old.data)]) != old.physical_digest):
                return False
        return True

    @staticmethod
    def _irrelevant_append(source, key, previous_key, admitted):
        anchor, (tenant, node, counts, digests) = key
        old_anchor, (old_tenant, old_node, old_counts, old_digests) = previous_key
        old_events = old_counts[0]
        if (anchor != old_anchor or (tenant, node) != (old_tenant, old_node)
                or counts[0] <= old_events or counts[1:] != old_counts[1:]
                or digests[1:] != old_digests[1:]
                or len(source[1]) != counts[0] + 1
                or source[1][old_events] != old_digests[0]):
            return False
        prefix = (source[0][:old_events], source[1], *source[2:])
        if not _same_projection_rows(prefix, admitted):
            return False
        # _sources retains its protected framing and whole-event semantics.
        # Recheck suffix schema/IDs because an internal caller may have mutated
        # exposed rows after admission. Any doubt delegates the exact refusal
        # to the original current projector at its original call site.
        seen = {row["id"] for row in admitted[0]}
        try:
            for row in source[0][old_events:]:
                row = validate_record(dict(row), tenant, EVENT_KINDS, integrity=True)
                if row["id"] in seen or row["kind"] in {"bus_epoch_roll_receipt", "message_retracted"}:
                    return False
                seen.add(row["id"])
                if (row["kind"] == "message_envelope" and row.get("recipient") == node
                        and row.get("worker_session_id") is None):
                    return False
        except Exception:
            return False
        return True

    def _load(self, node, *, materialize=True, _borrow=False):
        if _borrow and self._memo_inputs_unchanged(node):
            return self._load_memo[2]
        admission_before = None
        if _borrow:
            self._load_memo = self._selection_memo = None
            try:
                admission_before = self._memo_dependencies(node)[:2]
            except Exception:
                pass
        previous = self._borrow_active
        self._borrow_active = _borrow
        try:
            loaded = self._load_native(node, materialize=materialize)
            if _borrow:
                try:
                    dependencies = self._memo_dependencies(node)
                    archives = self._waiter_archive_snapshot()
                except Exception:
                    dependencies = None
                # Append writers may run between axis reads even though the
                # waiter owns EPOCH SH / WAKE. Never bind an old source to a
                # newer post-read identity. Witness initialization likewise
                # gets a subsequent complete native admission before reuse.
                if dependencies is not None and self._load_admission_matches(
                        admission_before, dependencies, node, sources_only=True):
                    # Source-only lineage is not permission to skip _load. It
                    # permits supported suffix extension after first witness
                    # creation, whose native readback is deliberately physical.
                    self._admission_lineage = node, dependencies, self._current_physical_sources(node)
                if dependencies is not None and self._load_admission_matches(admission_before, dependencies, node):
                    self._waiter_contract = dependencies[0]
                    self._load_memo = node, dependencies, loaded
                    self._waiter_completed_load = (node, dependencies, _clone_native(loaded),
                        self._current_physical_sources(node), self._history_cache[0], archives)
                else:
                    self._waiter_completed_load = self._waiter_derived = None
            return loaded
        except BaseException:
            if _borrow:
                self._discard_waiter_admission()
            raise
        finally:
            self._borrow_active = previous

    def _presentation_inputs(self, node, *, materialize=True, _borrow=False):
        data = self._bytes(self.relative(node), _borrow=_borrow)
        witness, data = self._witness(node, data, materialize=materialize, _borrow=_borrow)
        rows = _decode_path_records(self.root.tenant_home / self.relative(node), self.root.tenant_id, KINDS, data)
        groups = self._groups(rows, node)
        return witness, data, rows, groups

    def _load_native(self, node, *, materialize=True):
        witness, data, rows, groups = self._presentation_inputs(node, materialize=materialize,
            _borrow=self._borrow_active)
        anchor, epochs = self._epochs(node, witness["genesis_id"],
            (batch[0]["epoch_anchor"] for batch in groups.values()))
        # The selected resolver exists only after lineage resolution. One cold
        # scope admits the same receipt-owned archive across all history cuts.
        with self._epoch_resolver._verification_scope():
            return self._load_history(node, witness, data, rows, groups, anchor, epochs)

    def _presentation_authorities(self, node):
        from .codex_wait_contract import CodexWaitSessionLedger, consent_ledger_relative
        consents = (read_records_snapshot(self.root, consent_ledger_relative(node), allowed_kinds={"codex_wait_consent_receipt"})
            if self._authority_reader is None else self._authority_reader(consent_ledger_relative(node), {"codex_wait_consent_receipt"}))
        claims = (read_records_snapshot(self.root, CodexWaitSessionLedger._relative(node), allowed_kinds={"codex_wait_session_receipt"})
            if self._authority_reader is None else self._authority_reader(CodexWaitSessionLedger._relative(node), {"codex_wait_session_receipt"}))
        return consents, claims

    def _load_history(self, node, witness, data, rows, groups, anchor, epochs):
        consents, claims = self._presentation_authorities(node)
        cache_key = (_digest(data), witness["genesis_id"], tuple((key, value[1][-1], value[3][-1], value[5][-1])
            for key, value in epochs.items()), _digest(b"".join(encode_frame(row) for row in consents + claims)))
        if self._history_cache is not None and self._history_cache[0] == cache_key:
            return witness, data, rows, _clone_native(self._history_cache[1]), anchor, epochs
        if self._completed_history_extension(node, witness, cache_key, anchor, epochs):
            # Retain only completed validation of unchanged consumed cuts. The
            # returned epochs contain the newly read actual current source.
            self._history_cache = cache_key, self._history_cache[1]
            return witness, data, rows, _clone_native(self._history_cache[1]), anchor, epochs
        audited = groups if self.sealed == "verify" else {
            key: batch for key, batch in groups.items() if batch[0]["epoch_anchor"] == anchor}
        if self.sealed == "bytes":
            claimed = set()
            for batch in groups.values():
                row = batch[0]
                for item in row["item_ids"]:
                    identity = (row["epoch_anchor"], row["acting_session_id"], item)
                    if identity in claimed:
                        raise _failure("presentation exceeds one automatic attempt per conversation and epoch")
                    claimed.add(identity)
        cuts, limits = _presentation_cuts(audited, epochs)
        history, retained = {}, {}
        for epoch, counts in limits.items():
            source = epochs[epoch]
            # Every source is reopened and its canonical prefix chain verified
            # above. Appended tail bytes cannot change a previously verified
            # historical cut; rewrites cannot retain its exact prefix digest.
            key = (node, epoch, counts, tuple(source[axis * 2 + 1][count]
                for axis, count in enumerate(counts)))
            index = self._cut_cache.get(key)
            if index is None:
                index = _HistoricalCuts(source, node, self.root.tenant_id, counts,
                    root=self.root, home=self._epoch_homes[epoch], resolver=self._epoch_resolver)
            history[epoch] = index
            retained[key] = index
        self._cut_cache = retained
        _validate_presentation_groups(audited, epochs, cuts, history, consents, claims, node)
        # Public read() returns these rows. Never retain their dictionaries in
        # the successful history cache, including after a source-only refresh.
        self._history_cache = cache_key, _clone_native(groups)
        return witness, data, rows, groups, anchor, epochs

    def read(self, node):
        from .bus_epoch import epoch_guard
        from .wake_hold import wake_coordination_guard
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, node):
            if self._bytes(self.relative(node)) is None and self._bytes(self.witness_relative(node), 131072) is None:
                return []
            return self._load(node, materialize=False)[2]

    def select(self, binding, consent, classification, *, harness):
        return self._select(binding, consent, classification, harness=harness, _borrow=False)

    def _prepare_rolled_waiter_projection(self, node, session):
        from .codex_wait_contract import CodexWaitSessionLedger, consent_ledger_relative, WORKSPACE_MAP_RELATIVE
        from .wake_control import WakeController

        self._witness(node, self._bytes(self.relative(node), _borrow=True), _borrow=True)
        control = WakeController(self.root)
        marker = control._marker(node, control._identity(node, session)[2])
        return (self.relative(node), self.witness_relative(node), consent_ledger_relative(node),
            CodexWaitSessionLedger._relative(node), WORKSPACE_MAP_RELATIVE,
            control._ledger(node), marker.relative_to(self.root.tenant_home))

    def _select_waiter(self, binding, consent, classification, *, harness, at_stop=True, materialize=True):
        selection = self._select(binding, consent, classification, harness=harness, _borrow=True,
            _materialize=materialize)
        if not at_stop:
            from .mail_health import wake_eligible
            messages = [message for message in selection["fresh_messages"] if wake_eligible(message)]
            selection.update(fresh_messages=messages, fresh_total=len(messages),
                _eligible_ids=tuple(message["id"] for message in messages))
        return selection

    @contextmanager
    def _cold_event_prefix(self, *, _hint=False):
        """Borrow the native complete source certificate under its read lock."""
        from . import jsonl, send_prefix
        from .inbox_prefix import InboxPrefix, FIRST, RELATIVE
        path, _ = self._paths._path(RELATIVE)
        lock, coordinate = jsonl._lock_beside(path, RELATIVE)
        with jsonl._locked_path(lock, exclusive=False, relative=coordinate,
                timeout_seconds=0 if _hint else jsonl.LOCK_TIMEOUT_SECONDS,
                _open_handle=lambda: self._lock_handle(Path(coordinate))):
            identity = self._coordinate_identity(RELATIVE)
            prefix = InboxPrefix(self.root, read_only=True)
            prefix.load()
            first = prefix.get("first", FIRST)
            if ((prefix.payload["count"] == 0 and first is not None)
                    or (prefix.payload["count"] != 0 and type(first) is not dict)
                    or (first is not None and first.get("kind") == "bus_epoch_roll_receipt")):
                raise send_prefix.CacheMiss()
            yield prefix
            prefix._assert_current()
            if self._coordinate_identity(RELATIVE) != identity:
                raise send_prefix.CacheMiss()

    def _cold_source_has_no_roll(self):
        from . import send_prefix
        try:
            # Watch setup is advisory. A contended cache must fall back to
            # the native first-frame hint before the cold watcher imports and
            # binding snapshot, rather than spending its entire consent window.
            with self._cold_event_prefix(_hint=True):
                pass
            return True
        except send_prefix._CACHE_ERRORS + (IntegrityFailure, ProtocolRefusal):
            return False

    def _cold_empty_source(self, node):
        """Admit an empty answer, never a fabricated event source or cut.

        The native writer's complete, identity-bound recipient certificate
        proves absence across every worker plane. Any selected receipt or
        presentation history still needs its original full cut validation.
        """
        from . import jsonl, send_prefix
        from .bus_epoch import observe_epoch_roll_state
        from .cursor import SparseCursor
        try:
            if observe_epoch_roll_state(self.root)["classification"] != "absent":
                return None
            with self._cold_event_prefix() as prefix:
                if prefix.recipient_count(node) != 0:
                    return None
                prefix._assert_current()
                cursor = SparseCursor(self.root)
                for relative in (cursor._delivery_relative_path_for(node), cursor._relative_path_for(node)):
                    path, _ = self._paths._path(relative)
                    lock, coordinate = jsonl._lock_beside(path, relative)
                    with jsonl._locked_path(lock, exclusive=False, relative=coordinate,
                            _open_handle=lambda: self._lock_handle(Path(coordinate))):
                        if self._bytes(relative, _borrow=True) not in (None, b""):
                            return None
                # A retained cut, reservation or terminal receipt is never
                # dismissed using a current mailbox-absence proof.
                data = self._bytes(self.relative(node), _borrow=True)
                if data not in (None, b""):
                    return None
                witness, data = self._witness(node, data, _borrow=True)
                result = (witness, data) if data == b"" else None
            return result
        except send_prefix._CACHE_ERRORS + (IntegrityFailure, ProtocolRefusal):
            # Cache absence, replacement, corruption and uncertain coordinates
            # all retain the original controller/source reader's refusal.
            return None

    def _select_waiter_empty(self, binding, consent, classification, *, harness):
        return self._select(binding, consent, classification, harness=harness,
            _borrow=True, _empty_only=True)

    def _selected_source(self, node):
        from . import cut_prefix, jsonl
        from .bus_epoch import observe_epoch_roll_state
        if ((self._epoch_resolver is not None and self._epoch_resolver.frozen is not None)
                or not (self.root.tenant_home / cut_prefix.cache_name()).is_dir()
                or observe_epoch_roll_state(self.root)["classification"] != "absent"):
            return None
        relative = cut_prefix.RELATIVE
        identity = self._coordinate_identity(relative)
        path, _ = self._paths._path(relative)
        _, coordinate = jsonl._lock_beside(path, relative)
        events = cut_prefix.certified_source(self.root, node,
            _open_handle=lambda: self._lock_handle(Path(coordinate)))
        if events is None or identity[0] is None or tuple(events.identity) != identity[0]:
            return None
        source = self._sources(self.root.tenant_home, node, _borrow=True, selected_events=events)
        if self._coordinate_identity(relative) != identity:
            return None
        return source

    def _selected_presentation(self, node, session):
        from . import send_prefix
        try:
            source = self._selected_source(node)
            if source is None:
                return None
            witness, data, rows, groups = self._presentation_inputs(node, _borrow=True)
            anchor = witness["genesis_id"]
            epochs = {anchor: source}
            events = source[0]
            events.prepare_cuts((), presentations=(batch[0]["event_prefix_digest"] for batch in groups.values()))
            consents, claims = self._presentation_authorities(node)
            # Read every selected checkpoint proof, including historical
            # retraction positions, before reusing a completed projection.
            # The current source, witness and authority reads own all refusals.
            history = {anchor: _SelectedPresentationCuts(source)} if groups else {}
            memo = self._selection_memo
            self._selection_memo = None
            key = None
            if groups:
                try:
                    semantics = self._memo_semantics()
                except Exception:
                    semantics = None  # Introspection cannot refuse a native selection.
                if semantics is not None:
                    key = (semantics, node, events.identity, events.prefix.payload,
                        events.inbox.payload, events.send.payload, source[3][-1], source[5][-1],
                        data, witness, consents, claims)
                if key is not None and memo is not None and memo[1] == session and memo[0] == key:
                    self._selection_memo = memo
                    return memo[2]
            states = self._states(source, node, self.root.tenant_id)
            cuts, _ = _presentation_cuts(groups, epochs)
            _validate_presentation_groups(groups, epochs, cuts, history, consents, claims, node)
            payload = self._selection_payload(source, node, anchor, session, groups, epochs,
                _borrow=False, selected_states=states)
            selected = {**payload, "epoch_anchor": anchor, "_witness": witness, "_data": data,
                "_rows": rows, "_selected_digests": tuple(source[i][-1] for i in (1, 3, 5))}
            if key is not None and not payload["fresh_total"]:
                self._selection_memo = key, session, selected
            return selected
        except send_prefix._CACHE_ERRORS + (IntegrityFailure, ProtocolRefusal):
            # A rolled epoch, uncertain retraction, changing bank, or failed
            # native join gets its original validation and refusal precedence.
            return None

    def _select(self, binding, consent, classification, *, harness, _borrow, _empty_only=False,
            _materialize=True):
        selection = self._select_native(binding, consent, classification, harness=harness,
            _borrow=_borrow, _empty_only=_empty_only, _materialize=_materialize)
        if selection is None:
            return None
        return withhold_unapproved_selection(self.root, selection["_binding"].node_id, selection)

    def _select_native(self, binding, consent, classification, *, harness, _borrow, _empty_only,
            _materialize):
        from .codex_wait_contract import CodexWaitSessionLedger, CodexWaitConsentLedger, resolve_participant
        from .wake_control import is_session_paused
        node, session = CodexWaitSessionLedger(self.root)._identity(binding, consent, classification.calling_session_id)
        # Codex retains the startup mapping until its native WatchedLedger
        # observes replacement. Cursor's contract resolves mapping each poll.
        # The caller's classifier, consent and current claim are checked here
        # in both paths; this seam must not invent a second map-reopen policy.
        current = resolve_participant(self.root.tenant_home, binding.workspace) if harness == "cursor" else None
        ledger = CodexWaitSessionLedger(self.root)
        claims = read_records_snapshot(self.root, ledger._relative(node), allowed_kinds=ledger._KINDS)
        matching = ledger._matching(claims, binding)
        actual = matching[-1] if matching else None
        if classification.state != "authorized" or (harness == "cursor" and (current is None or current.binding != binding)) or (
            actual != classification.authority or (actual is not None and actual["acting_session_id"] != session)
            or CodexWaitConsentLedger(self.root).require_armed(binding) != consent
            or is_session_paused(self.root, node, session)
        ):
            raise ProtocolRefusal("presentation_authority_changed", "presentation no longer has native unpaused authority",
                remedy="restore the exact mapped and consented current session")
        if _empty_only:
            admitted = self._cold_empty_source(node)
            if admitted is None:
                return None
            witness, data = admitted
            # There is deliberately no _source: no raw digest or historical
            # prefix was projected, and an empty selection cannot be prepared.
            return {"fresh_messages": [], "fresh_total": 0, "held_items": [], "held_total": 0,
                "sealed_read": [], "_eligible_ids": (), "epoch_anchor": witness["genesis_id"],
                "_binding": binding, "_consent": _clone_native(consent), "_classification": classification,
                "_harness": harness, "_witness": _clone_native(witness), "_data": data, "_rows": []}
        if _borrow and harness == "cursor" and _materialize:
            selected = self._selected_presentation(node, session)
            if selected is not None:
                return {**_clone_native(selected), "_binding": binding, "_consent": _clone_native(consent),
                    "_classification": classification, "_harness": harness}
        witness, data, rows, groups, anchor, epochs = self._load(node, _borrow=_borrow, materialize=_materialize)
        source = epochs[anchor]
        memo = self._selection_memo if _borrow else None
        if memo is not None and memo[0] is source and memo[1] == session:
            payload = memo[2]
        else:
            payload = self._selection_payload(source, node, anchor, session, groups, epochs, _borrow=_borrow)
            if _borrow:
                self._selection_memo = source, session, payload
        # No authority, write-ahead state, or prepared output is retained in the
        # derived payload. Publicly returned graphs are always detached.
        return {**_clone_native(payload),
            "epoch_anchor": anchor, "_binding": binding, "_consent": _clone_native(consent), "_classification": classification,
            "_harness": harness, "_source": _clone_native(source), "_witness": _clone_native(witness),
            "_data": data, "_rows": _clone_native(rows)}

    def _selection_payload(self, source, node, anchor, session, groups, epochs, *, _borrow, selected_states=None):
        states = self._current_states(source, node, anchor, _borrow=_borrow) if selected_states is None else selected_states
        active = [item for item in states if item.state in {"fresh", "held"}]
        # Carrying one exact ID cannot erase an earlier reservation in this
        # same conversation. The original epoch/cut remains the proof; a new
        # roll receipt never fabricates a second prepare or terminal flush.
        active_frames = {item.message["id"]: encode_frame(item.message) for item in active}
        from .epoch_teardown import carried_envelope_digests
        # A selected source admits only a fully validated non-roll epoch.
        carried_digests = carried_envelope_digests(source[0]) if selected_states is None else None
        attempts, original_frames, sealed_read = {}, {}, set()
        for batch in groups.values():
            prepared = batch[0]
            if prepared["acting_session_id"] != session:
                continue
            borrowed = [item for item in prepared["item_ids"] if item in active_frames]
            if not borrowed:
                continue
            epoch = prepared["epoch_anchor"]
            if selected_states is not None and epoch == anchor:
                # The saved cut was reproduced in these same certified bytes.
                # Unique native IDs retain their exact frames across its tail.
                for item in borrowed:
                    attempts[item] = "flushed" if len(batch) == 2 else "prepared"
                continue
            if epoch != anchor and carried_digests is not None:
                for item in borrowed:
                    if hashlib.sha256(active_frames[item]).hexdigest() != carried_digests.get(item):
                        raise _failure("carried presentation envelope differs from its exact original native frame")
                    attempts[item] = "flushed" if len(batch) == 2 else "prepared"
                continue
            if epoch not in original_frames:
                if epoch in epochs:
                    original = epochs[epoch]
                else:
                    original = self._epoch_resolver._source(self._epoch_homes[epoch], node)
                    sealed_read.add(epoch)
                original_frames[epoch] = {row["id"]: encode_frame(row) for row in original[0]
                    if row["kind"] == "message_envelope"}
            for item in borrowed:
                if active_frames[item] != original_frames[epoch].get(item):
                    raise _failure("carried presentation envelope differs from its exact original native frame")
                attempts[item] = "flushed" if len(batch) == 2 else "prepared"
        fresh = [dict(item.message) for item in active if item.message["id"] not in attempts]
        held = [{"item_id": str(item.message["id"]), "presentation_count": item.presentation_count,
            "phase": attempts[item.message["id"]], "attempt_count": 1} for item in active if item.message["id"] in attempts]
        return {"fresh_messages": fresh, "fresh_total": len(fresh), "held_items": held, "held_total": len(held),
            "sealed_read": sorted(sealed_read),
            "_eligible_ids": tuple(message["id"] for message in fresh)}

    def held_evidence(self, selection):
        held = selection["held_items"]
        return {"state": "presentation_uncertain" if any(item["phase"] == "prepared" for item in held) else "held_unacked",
            "held_total": len(held), "held_items": held[:1000], "truncated": len(held) > 1000}

    def _append(self, selection, row):
        node = row["node_id"]
        data = selection["_data"]
        if self._bytes(self.relative(node)) != data:
            raise _failure("presentation history changed before guarded publication")
        state = dict(selection["_witness"], before_bytes=len(data), before_digest=_digest(data), reserved_record=row)
        # Write ahead: even a lost terminal append can only become uncertain,
        # never erase the reservation or make the IDs fresh again.
        self._save_witness(node, state)
        result = self._append_reserved(node, data, row)
        selection.update(_witness=state, _data=data + encode_frame(row), _rows=selection["_rows"] + [row])
        return result

    def prepare(self, selection, body):
        from .registry import utc_now
        from .registry import Registry
        Registry(self.root).require_protocol_lease(selection["_binding"].node_id, act="fresh delivery to recipient")
        messages = selection["fresh_messages"]
        require_presentable(self.root, selection["_binding"].node_id, messages)
        selected_ids = [message["id"] for message in messages]
        if not messages or len(messages) > 1000 or tuple(selected_ids) != selection["_eligible_ids"][:len(messages)]:
            raise ProtocolRefusal("presentation_batch_invalid", "presentation requires a bounded exact native selection",
                remedy="select native eligible envelopes before publication")
        encoded = (json.dumps(body) + "\n").encode("utf-8")
        if len(encoded) > OUTPUT_BYTES:
            raise ProtocolRefusal("presentation_output_too_large", "hook body exceeds its output bound",
                remedy="select whole envelopes within the output byte bound")
        for prior in selection["_rows"]:
            if prior["phase"] == "prepared" and prior["acting_session_id"] == selection["_classification"].calling_session_id and (
                prior["epoch_anchor"] == selection["epoch_anchor"] and prior["item_ids"] == selected_ids
            ):
                if prior["output_digest"] != _digest(encoded):
                    raise _failure("conflicting replay changes the prepared hook body")
                self._prepared_selection = selection
                return prior
        digests = selection.get("_selected_digests")
        if digests is None:
            digests = tuple(selection["_source"][i][-1] for i in (1, 3, 5))
        identity = "cursor-presentation-" + uuid7_hex()
        claim = selection["_classification"].authority
        row = {"schema_version": 1, "kind": KIND, "tenant_id": self.root.tenant_id,
            "id": identity, "batch_id": identity, "timestamp": utc_now(), "phase": "prepared",
            "node_id": selection["_binding"].node_id, "workspace": str(selection["_binding"].workspace),
            "acting_session_id": selection["_classification"].calling_session_id,
            "consent_receipt_id": selection["_consent"]["id"], "claim_receipt_id": claim["id"] if claim else None,
            "harness": selection["_harness"], "epoch_anchor": selection["epoch_anchor"],
            "item_ids": [message["id"] for message in messages], "event_prefix_digest": digests[0],
            "delivery_prefix_digest": digests[1], "acknowledgment_prefix_digest": digests[2],
            "output_digest": _digest(encoded), "delivery_receipt_id": None}
        validate_record(row, self.root.tenant_id, KINDS, integrity=False)
        self._append(selection, row)
        self._prepared_selection = selection
        return row

    def deliver(self, prepared, selection):
        from .registry import Registry
        Registry(self.root).require_protocol_lease(prepared["node_id"], act="fresh delivery to recipient")
        if prepared != selection["_rows"][-1] or prepared["item_ids"] != [row["id"] for row in selection["fresh_messages"]]:
            raise _failure("delivery differs from its guarded prepared selection")
        return EventLog(self.root)._write_delivery_receipt(prepared["node_id"], selection["fresh_messages"],
            _acting_session_id=prepared["acting_session_id"])

    def flushed(self, prepared, delivery):
        from .registry import utc_now
        selection = self._prepared_selection
        if delivery.get("recipient") != prepared["node_id"] or delivery.get("item_ids") != prepared["item_ids"]:
            raise _failure("terminal publication differs from its native delivery")
        source = None
        if "_selected_digests" in selection:
            from . import send_prefix
            try:
                source = self._selected_source(prepared["node_id"])
            except send_prefix._CACHE_ERRORS + (IntegrityFailure, ProtocolRefusal):
                pass
        if source is None:
            source = self._sources(self.root.tenant_home, prepared["node_id"])
        try:
            offset = source[3].index(prepared["delivery_prefix_digest"])
        except ValueError as exc:
            raise _failure("terminal publication lacks the prepared delivery prefix") from exc
        if offset >= len(source[2]) or source[2][offset] != delivery or delivery.get("kind") not in ("delivery_receipt", "delivered_unpresented"):
            raise _failure("terminal publication must name this batch's immediate native delivery")
        batch = self._groups(selection["_rows"], prepared["node_id"]).get(prepared["batch_id"], [])
        if not batch or batch[0] != prepared:
            raise _failure("terminal publication lacks the exact retained preparation")
        if len(batch) == 2:
            if batch[1]["delivery_receipt_id"] != delivery["id"]:
                raise _failure("terminal replay changes the native delivery")
            return batch[1]
        row = dict(prepared, id="cursor-presentation-" + uuid7_hex(), timestamp=utc_now(),
            phase="flushed", delivery_receipt_id=delivery["id"])
        validate_record(row, self.root.tenant_id, KINDS, integrity=False)
        return self._append(selection, row)
