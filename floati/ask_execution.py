"""Publish content-free ask closures from enrolled answering runtime callbacks.

This is a local adapter interface. Ordinary ask replies have no scope callback
and cannot attach native evidence. Provider finals and accounting belong to
the consumer of the projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from .ask import _from_terminal, _publish_reply, present_question
from .ask_cost_v2 import private_local_directory, read_private_file, session_reference, stamp
from .ask_replay import _receipt_chain
from .bus_epoch import _acquire_epoch_descriptor
from .registry import utc_now


_EXECUTION = "ask-cost-execution-v1.json"
_RECEIPTS = "ask-cost-receipts-v1.json"
_MANIFEST = "ask-cost-manifest-v1.json"
_PENDING = re.compile(r"\.ask-scope-([0-9a-f]{64})\.json")
_NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_PROVIDER = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_NATIVE_MAP_VERSION = 1
_END_DISPOSITIONS = frozenset({"completed", "failed", "cancelled"})
_MAX_BYTES = 16 * 1024 * 1024
_MAX_RECORDS = 4096


@dataclass(frozen=True)
class NativeCoordinate:
    """Verified adapter coordinates from the original native source.

    Codex uses the first rollout header session ID and the unsuppressed
    task_started turn ID. Claude Code uses the original transcript session ID
    and the initiating user row's UUID witnessed at injection.
    """

    provider: str
    source_lineage_id: str
    native_session_id: str
    native_turn_id: str


def _turn_identity(harness, coordinate):
    if type(coordinate) is not NativeCoordinate:
        raise ValueError("ask_execution_coordinate_invalid")
    fields = asdict(coordinate)
    for field, value in fields.items():
        pattern = _PROVIDER if field == "provider" else _NATIVE_ID
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValueError("ask_execution_coordinate_invalid")
    if coordinate.source_lineage_id != coordinate.native_session_id:
        raise ValueError("ask_execution_coordinate_invalid")
    if harness == "claude-code" and coordinate.provider != "anthropic":
        raise ValueError("ask_execution_source_unsupported")
    return fields


def _codex_final_selector(session, usage):
    # Puddle's cumulative-backed CodexUsageEvidenceID.tokenCount fingerprint.
    if type(usage) is not tuple or len(usage) != 2:
        raise ValueError("ask_execution_final_coordinate_invalid")
    fingerprints = []
    for vector in usage:
        if type(vector) is not tuple or len(vector) != 5 or any(
            type(value) is not int or not 0 <= value < 2**63 for value in vector
        ):
            raise ValueError("ask_execution_final_coordinate_invalid")
        fingerprints.append(":".join(map(str, vector)))
    total, last = fingerprints
    return _digest(f"codex:{session}:token:total:{total}:last:{last}".encode("utf-8"))


def _sequence(value):
    if type(value) is not int or not 0 <= value < 2**31:
        raise ValueError("ask_execution_sequence_invalid")
    return value


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _stage(parent, name, raw):
    if len(raw) > _MAX_BYTES:
        raise ValueError("ask_execution_generation_too_large")
    temporary = ".ask-stage-" + secrets.token_hex(16)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _receipt_projection(receipt):
    fields = ("id", "timestamp", "sender", "recipient", "acting_session_id")
    projected = {key: receipt[key] for key in fields}
    if "reply_to" in receipt:
        projected["reply_to"] = receipt["reply_to"]
    return projected


def _session_ref(receipt):
    return session_reference(receipt["tenant_id"], receipt["id"], receipt["acting_session_id"])


class AnswerExecutionProjection:
    """An enrolled adapter's explicit, tenant-local private publication directory."""

    def __init__(self, root, directory: Path):
        self.root = root
        self.directory = directory
        with private_local_directory(directory) as parent:
            identity = os.fstat(parent)
            self.directory_identity = identity.st_dev, identity.st_ino

    def _require_directory(self, parent):
        identity = os.fstat(parent)
        if (identity.st_dev, identity.st_ino) != self.directory_identity:
            raise ValueError("ask_execution_directory_changed")

    def present(self, harness, *, sequence, **presentation):
        """Present through the ledger before the adapter dispatches provider work."""
        if harness not in ("codex", "claude-code"):
            raise ValueError("ask_execution_source_unsupported")
        _sequence(sequence)
        result = present_question(self.root, **presentation)
        chain, _gap = _receipt_chain(self.root, result["question_id"])
        receipt = next(row for row in chain if row["id"] == result["presentation_id"])
        if any(row["kind"] in {"ask_answer", "ask_decline"} for row in chain):
            raise ValueError("ask_execution_already_resolved")
        return _AnswerScope(self, chain[0], receipt, harness, sequence)

    def _remember(self, scope):
        raw = _bytes({
            "native_map_version": _NATIVE_MAP_VERSION, "tenant_id": self.root.tenant_id, "scope": scope,
        })
        with private_local_directory(self.directory) as parent:
            self._require_directory(parent)
            _stage(parent, ".ask-scope-" + _digest(raw) + ".json", raw)

    def publish(self):
        """Recover staged closures by exact retained receipt and publish manifest last."""
        with private_local_directory(self.directory) as parent:
            self._require_directory(parent)
            _acquire_epoch_descriptor(parent, exclusive=True, use_turnstile=False)
            names = sorted(name for name in os.listdir(parent) if _PENDING.fullmatch(name))
            if len(names) > _MAX_RECORDS:
                raise ValueError("ask_execution_generation_too_large")
            scopes, receipts, chains = [], {}, {}
            remaining = _MAX_BYTES
            for name in names:
                raw = read_private_file(parent, name, remaining)
                remaining -= len(raw)
                if remaining < 0:
                    raise ValueError("ask_execution_generation_too_large")
                if _digest(raw) != _PENDING.fullmatch(name)[1]:
                    raise ValueError("ask_execution_attachment_invalid")
                attachment = json.loads(raw)
                if attachment["tenant_id"] != self.root.tenant_id:
                    raise ValueError("ask_execution_tenant_mismatch")
                # Earlier attachments accepted arbitrary provider call IDs.
                if attachment.get("native_map_version") != _NATIVE_MAP_VERSION:
                    continue
                scope = attachment["scope"]
                qid = scope["question_id"]
                if qid not in chains:
                    chain, _gap = _receipt_chain(self.root, qid)
                    chains[qid] = {row["id"]: row for row in chain}
                committed = chains[qid]
                ids = (qid, scope["presentation_id"], scope["answer_id"])
                if not all(receipt_id in committed for receipt_id in ids):
                    continue
                pair = {key: _receipt_projection(committed[rid])
                        for key, rid in zip(("question", "presentation", "answer"), ids)}
                scopes.append(scope)
                receipts[ids] = pair
            self._publish(parent, scopes, [receipts[key] for key in sorted(receipts)])

    def _publish(self, parent, scopes, pairs):
        common = {"schema_version": 1, "tenant_id": self.root.tenant_id}
        generation = _digest(_bytes({**common, "scopes": scopes, "pairs": pairs}))[:32]
        common["generation_id"] = "g-" + generation
        payloads = {
            _EXECUTION: _bytes(dict(common, scopes=scopes)),
            _RECEIPTS: _bytes(dict(common, pairs=pairs)),
        }
        if any(len(raw) > _MAX_BYTES for raw in payloads.values()):
            raise ValueError("ask_execution_generation_too_large")
        manifest = _bytes(dict(common, files={name: _digest(raw) for name, raw in payloads.items()}))
        with private_local_directory(self.directory) as current:
            self._require_directory(current)
            for name, raw in payloads.items():
                _stage(parent, name, raw)
            _stage(parent, _MANIFEST, manifest)


class _AnswerScope:
    def __init__(self, projection, question, presentation, harness, sequence):
        self.projection = projection
        self.question = question
        self.presentation = presentation
        self.harness = harness
        self.native = None
        self.opened_sequence = sequence
        self.opened_at = utc_now()
        self.last_sequence = sequence
        self.attempts = {}
        self.events = {}
        self.incomplete = False
        self.closed = False

    def bind_turn(self, native):
        """Bind the native turn-start callback before the first issue callback."""
        _turn_identity(self.harness, native)
        if self.native is not None and self.native != native:
            self.incomplete = True
            raise ValueError("ask_execution_turn_conflict")
        self.native = native

    def _advance(self, sequence, event):
        _sequence(sequence)
        if self.closed or self.events.get(sequence) == event:
            return False
        if sequence <= self.last_sequence:
            self.incomplete = True
            return False
        if sequence != self.last_sequence + 1:
            self.incomplete = True
        if len(self.events) >= 2 * _MAX_RECORDS:
            raise ValueError("ask_execution_generation_too_large")
        self.last_sequence = sequence
        self.events[sequence] = event
        return True

    def issued(self, sequence, native, call_id, *, message_id=None, parent_attempt=None,
               membership="exclusive"):
        """Record an issue witnessed by the adapter.

        For Claude, supply the explicit requestId and message.id joined to
        nonsynthetic native usage. For Codex the issue ID stays private.
        """
        if self.native is None:
            raise ValueError("ask_execution_turn_unbound")
        if (membership not in ("exclusive", "shared") or not isinstance(call_id, str)
                or _NATIVE_ID.fullmatch(call_id) is None):
            self.incomplete = True
            raise ValueError("ask_execution_attempt_invalid")
        identity = _turn_identity(self.harness, native)
        if self.harness == "claude-code":
            if not isinstance(message_id, str) or _NATIVE_ID.fullmatch(message_id) is None:
                self.incomplete = True
                raise ValueError("ask_execution_attempt_invalid")
            identity["native_call_id"] = call_id
        if not self._advance(sequence, ("issue", native, call_id, message_id, parent_attempt, membership)):
            return
        if len(self.attempts) == _MAX_RECORDS:
            raise ValueError("ask_execution_generation_too_large")
        if parent_attempt is not None and _sequence(parent_attempt) not in self.attempts:
            self.incomplete = True
            return
        if native != self.native or parent_attempt is not None:
            # Dedicated provider turns cannot certify a delegated population.
            self.incomplete = True
        self.attempts[sequence] = dict(
            identity, issued_sequence=sequence, ended_sequence=None, parent_attempt=parent_attempt,
            disposition="open", membership=membership,
        )

    def ended(self, sequence, issued_sequence, disposition, *, final_call_id=None, codex_final_usage=None):
        """Close an issue using counters bound by a positive Codex final callback.

        The callback supplies (total_token_usage, last_token_usage), each in
        input, cached input, output, reasoning output, total order.
        """
        if disposition not in _END_DISPOSITIONS:
            raise ValueError("ask_execution_attempt_invalid")
        _sequence(issued_sequence)
        attempt = self.attempts.get(issued_sequence)
        selector = None
        try:
            if final_call_id is not None or (codex_final_usage is not None and self.harness != "codex"):
                raise ValueError("ask_execution_final_coordinate_invalid")
            if codex_final_usage is not None and attempt is not None:
                selector = _codex_final_selector(attempt["native_session_id"], codex_final_usage)
        except ValueError:
            self.incomplete = True
            raise
        if not self._advance(sequence, ("end", issued_sequence, disposition, selector)):
            return
        if attempt is None or attempt["ended_sequence"] is not None:
            self.incomplete = True
            return
        if selector is not None:
            attempt["native_call_id"] = selector
        attempt.update(ended_sequence=sequence, disposition=disposition)

    def answer(self, *, sequence, through_sequence, **answer):
        _sequence(sequence)
        _sequence(through_sequence)
        if self.native is None:
            raise ValueError("ask_execution_turn_unbound")
        if sequence <= self.last_sequence or not self.opened_sequence <= through_sequence <= sequence:
            raise ValueError("ask_execution_closure_invalid")
        question, presentation = self.question, self.presentation
        # The Claude request identity alone cannot select a final snapshot.
        mapped = {}
        for issued_sequence, attempt in self.attempts.items():
            parent = attempt["parent_attempt"]
            if "native_call_id" in attempt and (parent is None or parent in mapped):
                mapped[issued_sequence] = attempt
        attempts = list(mapped.values())
        complete = (
            self.harness == "codex" and not self.incomplete and bool(attempts)
            and len(attempts) == len(self.attempts)
            and len({attempt["native_call_id"] for attempt in attempts}) == len(attempts)
            and through_sequence == sequence == self.last_sequence + 1
            and all(attempt["ended_sequence"] is not None for attempt in attempts)
        )
        native = _turn_identity(self.harness, self.native)
        scope_id = "s-" + _digest(_bytes([presentation["id"], self.harness, native]))[:32]

        def prepare(receipt):
            if not stamp(presentation["timestamp"]) <= stamp(self.opened_at) <= stamp(receipt["timestamp"]):
                raise ValueError("ask_execution_clock_regressed")
            scope = {
                **native, "scope_id": scope_id, "harness": self.harness,
                "question_id": question["id"], "presentation_id": presentation["id"],
                "answer_id": receipt["id"],
                "asking_node": question["sender"], "asking_session_ref": _session_ref(question),
                "answering_node": receipt["sender"], "answering_session_ref": _session_ref(receipt),
                "presentation_session_ref": _session_ref(presentation),
                "opened_at": self.opened_at, "closed_at": receipt["timestamp"],
                "opened_sequence": self.opened_sequence, "closed_sequence": sequence,
                "completeness": {"status": "complete" if complete else "incomplete",
                                 "through_sequence": through_sequence},
                "attempts": attempts,
            }
            self.projection._remember(scope)

        question, record = _publish_reply(
            self.projection.root, prepare=prepare, sender=presentation["sender"],
            recipient=presentation["recipient"], question_id=question["id"],
            session=presentation["acting_session_id"], disposition="answer",
            reason=None, **answer,
        )
        self.closed = True
        return {**_from_terminal(question, record), "record": record}
