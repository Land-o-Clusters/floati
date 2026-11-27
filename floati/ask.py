"""Local correlated question and answer on one fleet ledger.

One call appends an addressed question and waits until a reversed-party
answer or decline is on this machine's event ledger. It opens no listener
and contacts no other machine. Timeout and an unreadable bound answer are
UNKNOWN; answered is produced only when the answer record id is present.
"""

from __future__ import annotations

import hashlib
import re
import time
from threading import Event
from typing import Dict, Mapping, Optional, Sequence

from .ask_cost import project_ask_cost
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EVENT_KINDS, validate_event_records
from .framing import FrameError, decode_frames, encode_frame
from .ids import uuid7_hex
from .jsonl import transact, transact_records
from .records import ASK_RECORD_KINDS, _UUID7, validate_record
from .registry import Registry, utc_now

_EVENTS = "events.jsonl"
_TERMINALS = frozenset({"ask_answer", "ask_decline"})
_ANSWER_ID = re.compile("ask-answer-" + _UUID7)
_DECLINE_ID = re.compile("ask-decline-" + _UUID7)
_POLL_SECONDS = 0.05


def project_terminal(terminal: object) -> Dict[str, object]:
    """Map one observed terminal to a caller-visible outcome.

    answered is returned only when the terminal carries an answer receipt id.
    """

    if not isinstance(terminal, Mapping):
        return {"outcome": "UNKNOWN", "code": "ask_answer_unreadable"}
    kind = terminal.get("kind")
    receipt = terminal.get("id")
    if kind == "ask_answer" and isinstance(receipt, str) and _ANSWER_ID.fullmatch(receipt):
        return {
            "outcome": "answered",
            "answer_id": receipt,
            "answer_note": terminal.get("note"),
        }
    if kind == "ask_decline" and isinstance(receipt, str) and _DECLINE_ID.fullmatch(receipt):
        return {
            "outcome": "declined",
            "decline_id": receipt,
            "reason": terminal.get("reason"),
        }
    return {"outcome": "UNKNOWN", "code": "ask_answer_unreadable"}


def perform_ask(
    root,
    *,
    sender: str,
    recipient: str,
    repo: str,
    sha: str,
    doc: str,
    note: str,
    session: str,
    idempotency_key: str,
    wait_seconds: int,
    timing: Optional[Dict[str, int]] = None,
    stop_waiting: Optional[Event] = None,
) -> Dict[str, object]:
    """Append one question and wait for its correlated terminal.

    An optional local instrument receives monotonic boundaries, never ledger
    fields: after question publication and on correlated answer observation.
    """

    sender_id, recipient_id = _parties(root, sender, recipient)
    question = _publish_question(
        root,
        sender=sender_id,
        recipient=recipient_id,
        repo=repo,
        sha=sha,
        doc=doc,
        note=note,
        session=session,
        idempotency_key=idempotency_key,
    )
    if timing is not None:
        timing["question_sent_ns"] = time.monotonic_ns()
    result = wait_for_question(root, question, wait_seconds, stop_waiting=stop_waiting)
    if timing is not None and result["state"] == "answered":
        timing["answer_received_ns"] = time.monotonic_ns()
    return result


def wait_for_question(root, question, wait_seconds, *, stop_waiting: Optional[Event] = None):
    """Wait without holding the membership barrier needed by reply writers."""
    started = time.monotonic()
    deadline = started + wait_seconds
    while True:
        if stop_waiting is not None and stop_waiting.is_set():
            return {
                "state": "cancelled",
                "evidence": {
                    "outcome": "UNKNOWN",
                    "code": "ask_wait_cancelled",
                    "question_id": question["id"],
                },
            }
        seen = _observe(root, str(question["id"]))
        if seen["state"] == "torn":
            if time.monotonic() >= deadline:
                raise IntegrityFailure(
                    "ask_ledger_unreadable",
                    "DRAFT - the event ledger could not be read for this question",
                )
        elif seen["state"] != "open":
            return _finish_observation(seen, question)
        elif time.monotonic() >= deadline:
            break
        pause = min(_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
        if stop_waiting is None:
            time.sleep(pause)
        else:
            stop_waiting.wait(pause)
    return _publish_timeout(root, question, started)


def perform_group_ask(root, args):
    """Publish a frozen audience together, then wait within one shared budget."""
    from .addressing import bound_preview, group_key, group_write_guard
    from .role_addressing import confirm_role_preview, is_role_address, load_role_preview

    with group_write_guard(root):
        path = getattr(args, "recipient_preview", None)
        role = is_role_address(args.recipient)
        preview = (load_role_preview(root, args.sender, args.recipient, path) if role else
                   bound_preview(root, args.sender, args.recipient, path))
        candidates = [
            _question_record(
                root, sender=args.sender, recipient=node, repo=args.repo,
                sha=args.sha, doc=args.doc, note=args.note, session=args.session,
                idempotency_key=args.idempotency_key if role else group_key(args.idempotency_key, node),
                address_preview=preview,
            ) for node in preview["recipients"]
        ]
        for candidate in candidates:
            if not role:
                _parties(root, args.sender, candidate["recipient"])
            validate_record(candidate, root.tenant_id, frozenset({"ask_question"}), integrity=False)

        def decide(records):
            validate_event_records(records)
            questions, tail = [], []
            for candidate in candidates:
                saved = next((row for row in records if row.get("kind") == "ask_question"
                              and row.get("idempotency_key") == candidate["idempotency_key"]), None)
                if saved is not None:
                    if not _same_question(saved, candidate):
                        raise ProtocolRefusal("idempotency_conflict", "group question key has different content",
                                              "reuse the key with the same question or choose a new key")
                    questions.append(saved)
                else:
                    if role:
                        unknown = confirm_role_preview(root, preview, getattr(args, "puddle_quota_snapshot", None))
                        if unknown is not None:
                            return ([], unknown), []
                    questions.append(candidate)
                    tail.append(candidate)
            validate_event_records([*records, *tail])
            return (questions, None), tail

        questions, unknown = transact_records(root, _EVENTS, decide, allowed_kinds=set(EVENT_KINDS))
    if unknown is not None:
        return "degraded", unknown, 35
    deadline = time.monotonic() + args.wait_seconds
    results = []
    for question in questions:
        result = wait_for_question(root, question, max(0, deadline - time.monotonic()))
        results.append({"recipient": question["recipient"], "state": result["state"], **result["evidence"]})
    evidence = dict(preview, results=results)
    if any(row["state"] not in {"answered", "declined", "timeout"} for row in results):
        return "degraded", dict(evidence, outcome="UNKNOWN"), 35
    if any(row["state"] == "timeout" for row in results):
        return "orchestration_deadline", dict(evidence, outcome="UNKNOWN", code="ask_timeout"), 34
    return "ok", dict(evidence, outcome="complete"), 0


def perform_reply(
    root,
    *,
    sender: str,
    recipient: str,
    question_id: str,
    disposition: str,
    session: str,
    idempotency_key: str,
    note: Optional[str] = None,
    reason: Optional[str] = None,
    repo: Optional[str] = None,
    sha: Optional[str] = None,
    doc: Optional[str] = None,
) -> Dict[str, object]:
    """Append the one terminal answer or decline for an existing question."""

    sender_id, recipient_id = _parties(root, sender, recipient)
    question, record = _publish_reply(
        root,
        sender=sender_id,
        recipient=recipient_id,
        question_id=question_id,
        disposition=disposition,
        session=session,
        idempotency_key=idempotency_key,
        note=note,
        reason=reason,
        repo=repo,
        sha=sha,
        doc=doc,
    )
    return {**_from_terminal(question, record), "record": record}


def present_question(root, *, sender, recipient, question_id, session, idempotency_key):
    """Return the addressed question through a recipient-bound presentation receipt."""
    sender, recipient = _parties(root, sender, recipient)
    candidate = {
        "schema_version": 1,
        "id": "ask-presentation-" + uuid7_hex(),
        "tenant_id": root.tenant_id,
        "timestamp": _stamp(),
        "kind": "ask_presentation",
        "sender": sender,
        "recipient": recipient,
        "reply_to": question_id,
        "idempotency_key": idempotency_key,
        "acting_session_id": session,
    }
    validate_record(candidate, root.tenant_id, frozenset({"ask_presentation"}), integrity=False)

    def decide(records):
        validate_event_records(records)
        question = _question_for_reply(records, question_id, sender, recipient)
        for saved in records:
            if saved["kind"] != "ask_presentation" or saved["sender"] != sender:
                continue
            if saved["idempotency_key"] != idempotency_key:
                continue
            if saved == dict(candidate, id=saved["id"], timestamp=saved["timestamp"]):
                return (question, saved), None
            raise ProtocolRefusal(
                "idempotency_conflict", "This presentation key names a different request.",
                "Use a new key for a different question or session.",
            )
        if any(row["kind"] in _TERMINALS and row["reply_to"] == question_id for row in records):
            raise ProtocolRefusal(
                "ask_already_resolved", "This question already has an answer or decline.",
                "Use replay to inspect the recorded question history.",
            )
        validate_event_records([*records, candidate])
        return (question, candidate), candidate

    question, receipt = transact(root, _EVENTS, decide, allowed_kinds=set(EVENT_KINDS))
    return {
        "outcome": "presented", "question_id": question_id, "presentation_id": receipt["id"],
        "question": {key: question[key] for key in ("sender", "recipient", "repo", "sha", "doc", "note")},
    }


def _parties(root, sender: str, recipient: str) -> tuple:
    registry = Registry(root)
    sender_id = registry.resolve_node_id(
        sender, field="sender", unknown_code="unknown_sender",
    )
    try:
        recipient_id = registry.resolve_node_id(
            recipient, field="recipient", unknown_code="unknown_recipient",
        )
    except ProtocolRefusal as exc:
        if exc.code != "unknown_recipient":
            raise
        roster = ", ".join(registry.active_node_ids()) or "(none)"
        raise ProtocolRefusal(
            "recipient_unregistered",
            "DRAFT - message refused: recipient {0!r} is not registered; registered nodes: {1}".format(
                recipient, roster
            ),
            "DRAFT - register the recipient, then ask again",
        ) from exc
    return sender_id, recipient_id


def _stamp() -> str:
    return utc_now()


def _question_record(root, **fields) -> Dict[str, object]:
    record = {
        "schema_version": 1,
        "id": "ask-question-" + uuid7_hex(),
        "tenant_id": root.tenant_id,
        "timestamp": _stamp(),
        "kind": "ask_question",
        "sender": fields["sender"],
        "recipient": fields["recipient"],
        "repo": fields["repo"],
        "sha": fields["sha"],
        "doc": fields["doc"],
        "note": fields["note"],
        "idempotency_key": fields["idempotency_key"],
        "acting_session_id": fields["session"],
    }
    if fields.get("address_preview") is not None:
        record["address_preview"] = fields["address_preview"]
    return record


def _same_question(record: Mapping[str, object], candidate: Mapping[str, object]) -> bool:
    names = (
        "sender", "recipient", "repo", "sha", "doc", "note",
        "idempotency_key", "acting_session_id",
        "address_preview",
    )
    return all(record.get(name) == candidate.get(name) for name in names)


def _publish_question(root, **fields) -> Dict[str, object]:
    candidate = _question_record(root, **fields)

    def decide(records):
        validate_event_records(records)
        for record in records:
            if record.get("kind") != "ask_question" or record.get("idempotency_key") != fields["idempotency_key"]:
                continue
            saved = dict(candidate, id=record["id"], timestamp=record["timestamp"])
            if _same_question(record, saved):
                return record, None
            raise ProtocolRefusal(
                "idempotency_conflict",
                "DRAFT - idempotency key has different question content",
                "DRAFT - reuse the key only with the same question, or choose a new key",
            )
        validate_record(candidate, root.tenant_id, frozenset({"ask_question"}), integrity=False)
        validate_event_records([*records, candidate])
        return candidate, candidate

    return transact(root, _EVENTS, decide, allowed_kinds=set(EVENT_KINDS))


def _question_for_reply(records, question_id, sender, recipient):
    question = next((row for row in records if row["kind"] == "ask_question"
                     and row["id"] == question_id), None)
    if question is None:
        raise ProtocolRefusal(
            "ask_question_unknown",
            "DRAFT - reply names no question on this ledger",
            "DRAFT - reply to a question id this ledger already holds",
        )
    if question["sender"] != recipient or question["recipient"] != sender:
        raise ProtocolRefusal(
            "ask_parties_mismatch",
            "DRAFT - a reply must reverse the question parties",
            "DRAFT - send the reply from the question recipient back to its sender",
        )
    return question


def _publish_reply(root, *, prepare=None, **fields):
    kind = "ask_answer" if fields["disposition"] == "answer" else "ask_decline"
    prefix = "ask-answer-" if kind == "ask_answer" else "ask-decline-"
    record: Dict[str, object] = {
        "schema_version": 1,
        "id": prefix + uuid7_hex(),
        "tenant_id": root.tenant_id,
        "timestamp": _stamp(),
        "kind": kind,
        "sender": fields["sender"],
        "recipient": fields["recipient"],
        "reply_to": fields["question_id"],
        "idempotency_key": fields["idempotency_key"],
        "acting_session_id": fields["session"],
    }
    if kind == "ask_answer":
        record.update({
            "repo": fields["repo"],
            "sha": fields["sha"],
            "doc": fields["doc"],
            "note": fields["note"],
        })
    else:
        record["reason"] = fields["reason"]

    def decide(records):
        validate_event_records(records)
        question = _question_for_reply(records, fields["question_id"], fields["sender"], fields["recipient"])
        for saved in records:
            if saved.get("kind") not in _TERMINALS or saved.get("idempotency_key") != fields["idempotency_key"]:
                continue
            replay = dict(record, id=saved["id"], timestamp=saved["timestamp"])
            if all(saved.get(name) == replay.get(name) for name in replay):
                return (question, saved), None
            raise ProtocolRefusal(
                "idempotency_conflict",
                "DRAFT - idempotency key has different reply content",
                "DRAFT - reuse the key only with the same reply, or choose a new key",
            )
        if any(
            saved.get("kind") in _TERMINALS and saved.get("reply_to") == fields["question_id"]
            for saved in records
        ):
            raise ProtocolRefusal(
                "ask_already_resolved",
                "DRAFT - this question already has a terminal record",
                "DRAFT - start a new question; the existing terminal stands",
            )
        validate_record(record, root.tenant_id, frozenset({kind}), integrity=False)
        validate_event_records([*records, record])
        if prepare is not None:
            prepare(record)
        return (question, record), record

    return transact(root, _EVENTS, decide, allowed_kinds=set(EVENT_KINDS))


def _observe(root, question_id: str) -> Dict[str, object]:
    path = root.resolve_relative(_EVENTS)
    try:
        raw = path.read_bytes() if path.is_file() else b""
        frames = decode_frames(raw) if raw else []
    except (OSError, FrameError) as exc:
        return {"state": "torn", "error": exc}
    return _classify(frames, question_id, root.tenant_id)


def _classify(
    frames: Sequence[object], question_id: str, tenant: str,
) -> Dict[str, object]:
    question = None
    terminal = None
    chain = {}
    unreadable = False
    for frame in frames:
        if not isinstance(frame, Mapping):
            continue
        kind = frame.get("kind")
        if not isinstance(kind, str) or kind not in ASK_RECORD_KINDS:
            continue
        reference = "id" if kind == "ask_question" else "reply_to"
        if frame.get(reference) != question_id:
            continue
        try:
            if type(frame.get("schema_version")) is not int or frame["schema_version"] != 1:
                raise ValueError("unsupported ask record version")
            accepted = validate_record(dict(frame), tenant, frozenset({kind}), integrity=False)
        except (ProtocolRefusal, IntegrityFailure, KeyError, TypeError, ValueError):
            if kind == "ask_question":
                return {"state": "absent"}
            unreadable = True
            continue
        if accepted["id"] in chain:
            unreadable = True
            continue
        chain[accepted["id"]] = accepted
        if kind == "ask_question":
            question = accepted
        elif kind in _TERMINALS:
            terminal = accepted
    if question is None:
        return {"state": "absent"}
    try:
        validate_event_records(list(chain.values()))
    except IntegrityFailure:
        unreadable = True
    if unreadable:
        return {"state": "unreadable", "question": question}
    if terminal is not None:
        return {"state": "terminal", "question": question, "terminal": terminal}
    return {"state": "open", "question": question}


def _finish_observation(seen: Mapping[str, object], question: Mapping[str, object]) -> Dict[str, object]:
    state = seen["state"]
    question_id = str(question["id"])
    if state == "terminal":
        return _from_terminal(seen["question"], seen["terminal"])
    if state == "unreadable":
        return {
            "state": "unreadable",
            "evidence": {
                "outcome": "UNKNOWN",
                "code": "ask_answer_unreadable",
                "question_id": question_id,
            },
        }
    if state == "absent":
        return {
            "state": "absent",
            "evidence": {
                "outcome": "UNKNOWN",
                "code": "ask_question_unreadable",
                "question_id": question_id,
            },
        }
    return {"state": "open", "question": question}


def _from_terminal(question: Mapping[str, object], terminal: Mapping[str, object]) -> Dict[str, object]:
    question_id = question["id"]
    projected = project_terminal(terminal)
    if projected["outcome"] == "UNKNOWN":
        return {
            "state": "unreadable",
            "evidence": {
                "outcome": "UNKNOWN",
                "code": "ask_answer_unreadable",
                "question_id": question_id,
            },
        }
    evidence = {"outcome": projected["outcome"], "question_id": question_id}
    if projected["outcome"] == "answered":
        evidence["answer_id"] = projected["answer_id"]
        evidence["answer_note"] = terminal["note"]
        evidence["cost"] = project_ask_cost(question, terminal)
        return {"state": "answered", "evidence": evidence}
    evidence["decline_id"] = projected["decline_id"]
    evidence["reason"] = terminal["reason"]
    return {"state": "declined", "evidence": evidence}


def _publish_timeout(root, question: Mapping[str, object], started: float) -> Dict[str, object]:
    question_id = str(question["id"])

    def decide(records):
        validate_event_records(records)
        classified = _classify(records, question_id, root.tenant_id)
        if classified["state"] == "terminal":
            return classified, None
        if classified["state"] == "unreadable":
            return classified, None
        if classified["state"] == "absent":
            return classified, None
        elapsed_ms = int(max(0.0, (time.monotonic() - started) * 1000))
        prefix = hashlib.sha256(b"".join(encode_frame(row) for row in records)).hexdigest()
        timeout = {
            "schema_version": 1,
            "id": "ask-timeout-" + uuid7_hex(),
            "tenant_id": root.tenant_id,
            "timestamp": _stamp(),
            "kind": "ask_timeout",
            "reply_to": question_id,
            "observer": question["sender"],
            "elapsed_ms": elapsed_ms,
            "prefix_sha256": prefix,
            "idempotency_key": "wait-" + uuid7_hex(),
        }
        validate_record(timeout, root.tenant_id, frozenset({"ask_timeout"}), integrity=False)
        validate_event_records([*records, timeout])
        return {"state": "timeout", "timeout": timeout, "question": question}, timeout

    result = transact(root, _EVENTS, decide, allowed_kinds=set(EVENT_KINDS))
    if result["state"] == "terminal":
        return _from_terminal(result["question"], result["terminal"])
    if result["state"] == "unreadable":
        return _finish_observation(result, question)
    if result["state"] == "absent":
        return _finish_observation(result, question)
    timeout = result["timeout"]
    return {
        "state": "timeout",
        "evidence": {
            "outcome": "UNKNOWN",
            "code": "ask_timeout",
            "question_id": question_id,
            "timeout_id": timeout["id"],
        },
    }
