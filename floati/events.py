"""Guarded message events and distinct delivery/denial evidence."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import re
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .bus_epoch import shared_epoch_operation
from .epoch_rotation import automatic_epoch_operation
from .errors import IntegrityFailure, ProtocolRefusal, bind_send_refusal_remedy
from .ids import uuid7_hex
from .inbound_hold import rejected_message_ids, require_presentable, withhold_unapproved
from .jsonl import (
    append_record,
    read_records,
    read_records_compatible,
    read_records_compatible_sealed_snapshot,
    read_records_compatible_snapshot,
    read_records_compatible_with_versions,
    read_records_sealed_snapshot,
    read_records_snapshot,
    transact,
    transact_records,
)
from .mail_health import RecipientReadiness
from .native_dispatch_launcher import (
    KIND as LAUNCHER_KIND,
    capture_native_launcher,
    native_launcher_runtime_session,
)
from .records import ASK_RECORD_KINDS, WAKE_HOLD_KINDS, validate_record
from .registry import Registry, utc_now
from .root import FloatiRoot, validate_identifier
from .runtruth import RUN_KINDS
from .version_skew import VocabularySkewFact, vocabulary_skew_fact


MAX_PRESENTATION_ITEMS = 1000
EVENT_KINDS = frozenset(
    {
        "message_envelope", "delivery_claim", "message_retracted",
        "bus_epoch_roll_receipt",
        "seat_teardown_epoch_verification",
        "epoch_carry_verification",
        "ledger_repair_receipt",
        "send_readiness_observation",
        LAUNCHER_KIND,
        "seat_execution",
        "ask_question",
        "ask_answer",
        "ask_decline",
        "ask_timeout",
    }
)
_ATTEMPT_BINDING_FIELDS = frozenset(
    {"attempt_id", "claim_id", "lease_id", "worker_session_id"}
)


class SendReceipt(dict):
    """G2 send testimony with temporary direct envelope lookup compatibility."""

    def __getitem__(self, key: str) -> object:
        try:
            return super().__getitem__(key)
        except KeyError:
            message = super().__getitem__("message")
            if isinstance(message, Mapping):
                return message[key]
            raise

    def get(self, key: str, default: object = None) -> object:
        if key in self:
            return super().get(key, default)
        message = super().get("message")
        if isinstance(message, Mapping):
            return message.get(key, default)
        return default

    def __eq__(self, other: object) -> bool:
        if super().__eq__(other):
            return True
        message = super().__getitem__("message")
        return isinstance(message, Mapping) and message == other


def _send_observed_at(now: Optional[datetime]) -> str:
    """Use one valid operation instant for leased sends and their envelope."""

    if now is None:
        return utc_now()
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ProtocolRefusal(
            "time_invalid", "lease evaluation requires an aware datetime"
        )
    return now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_receipt_time(stamp: str) -> datetime:
    """Parse one registry UTC-milliseconds stamp back into an aware instant."""

    parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "receipt stamps must be aware")
    return parsed.astimezone(timezone.utc)


def _validate_ask_event(
    record: Mapping[str, object],
    questions: Dict[str, Dict[str, object]],
    resolved: set,
) -> None:
    """Require a question before its answer, decline, or timeout."""

    kind = record["kind"]
    if kind == "ask_question":
        questions[str(record["id"])] = record
        return
    question = questions.get(str(record.get("reply_to")))
    if question is None:
        raise IntegrityFailure(
            "ask_correlation_invalid",
            "ask terminal evidence must follow its question",
        )
    if kind == "ask_timeout":
        if record.get("observer") != question["sender"]:
            raise IntegrityFailure(
                "ask_correlation_invalid",
                "ask timeout observer must be the question sender",
            )
        return
    if question["sender"] != record.get("recipient") or question["recipient"] != record.get("sender"):
        raise IntegrityFailure(
            "ask_correlation_invalid",
            "ask terminal evidence must reverse the question parties",
        )
    if str(record.get("reply_to")) in resolved:
        raise IntegrityFailure(
            "ask_already_resolved",
            "a question can have one terminal answer or decline",
        )
    resolved.add(str(record.get("reply_to")))


def validate_event_records(records: Sequence[Dict[str, object]]) -> None:
    """Reject a schema-valid event prefix that cannot be semantically replayed."""

    if any(row["kind"] in {"seat_teardown_epoch_verification", "epoch_carry_verification"} for row in records) or (
            records and records[0]["kind"] == "bus_epoch_roll_receipt"
            and "teardown_carry" in records[0].get("automatic", {})):
        from .epoch_teardown import validate_event_verifications
        validate_event_verifications(records)
    _validate_event_order(records, 0, {}, set(), set(), {}, set(), set())


def _validate_event_order(records, start, prior_by_id, retracted, observed_envelopes,
                          ask_questions, ask_resolved, launcher_envelopes):
    """Apply the same ordered joins to a whole ledger or a certified slice."""
    for index, record in enumerate(records, start):
        if record["kind"] == "bus_epoch_roll_receipt":
            if index != 0:
                raise IntegrityFailure(
                    "bus_epoch_roll_receipt_position_invalid",
                    "one bus epoch roll receipt is permitted only as physical record one",
                )
            continue
        if record["kind"] == "message_envelope":
            reply_to = record.get("reply_to")
            if reply_to is not None:
                original = prior_by_id.get(str(reply_to))
                if original is None:
                    raise IntegrityFailure(
                        "message_order_invalid",
                        "reply evidence must follow its referenced original message",
                    )
                if (
                    original["sender"] != record["recipient"]
                    or original["recipient"] != record["sender"]
                ):
                    raise IntegrityFailure(
                        "message_order_invalid",
                        "reply evidence must reverse the original message parties",
                    )
            prior_by_id[str(record["id"])] = record
            continue
        if record["kind"] == "delivery_claim":
            envelope = prior_by_id.get(str(record["note_ref"]))
            if envelope is None:
                raise IntegrityFailure(
                    "delivery_claim_order_invalid",
                    "delivery claim evidence must follow its referenced message",
                )
            if envelope["sha"] != record["sha"]:
                raise IntegrityFailure(
                    "delivery_claim_sha_invalid",
                    "delivery claim sha must equal its referenced message sha",
                )
            continue
        if record["kind"] in {"ledger_repair_receipt", "seat_teardown_epoch_verification", "epoch_carry_verification"}:
            continue
        if record["kind"] == LAUNCHER_KIND:
            message_id = record["message_id"]
            envelope = prior_by_id.get(message_id)
            if (envelope is None or message_id not in observed_envelopes
                    or message_id in launcher_envelopes
                    or envelope["sender"] != record["sender_node_id"]
                    or envelope["timestamp"] != record["timestamp"]):
                raise IntegrityFailure("native_dispatch_launcher_invalid",
                    "launcher testimony must follow its exact guarded send")
            launcher_envelopes.add(message_id)
            continue
        if record["kind"] == "seat_execution":
            if record["envelope_id"] is not None:
                envelope = prior_by_id.get(record["envelope_id"])
                if envelope is None or envelope["sender"] != record["node_id"] or envelope["idempotency_key"] != record["idempotency_key"] or envelope["timestamp"] != record["timestamp"]:
                    raise IntegrityFailure("seat_execution_notice_invalid", "execution must follow its exact native notice")
                if record["envelope_id"] not in observed_envelopes:
                    raise IntegrityFailure("seat_execution_notice_invalid", "execution notice needs atomic readiness testimony")
            elif record["event"] != "running" and not (
                record["event"] == "unknown" and record["code"] in {
                    "cursor_followup_returned", "gateway_action_returned"}
            ):
                raise IntegrityFailure("seat_execution_notice_invalid", "terminal execution acceptance needs its notice")
            continue
        if record["kind"] == "send_readiness_observation":
            envelope_id = str(record["envelope_id"])
            envelope = prior_by_id.get(envelope_id)
            if envelope is None:
                raise IntegrityFailure(
                    "send_readiness_order_invalid",
                    "send readiness must follow its referenced message envelope",
                )
            if envelope_id in observed_envelopes:
                raise IntegrityFailure(
                    "send_readiness_duplicate",
                    "one envelope can have only one readiness observation",
                )
            if any(record[field] != envelope[field] for field in (
                "sender", "recipient", "idempotency_key",
            )):
                raise IntegrityFailure(
                    "send_readiness_binding_invalid",
                    "readiness parties and idempotency key must equal its envelope",
                )
            if not (
                record["timestamp"] == record["observed_at"]
                == record["readiness"]["observed_at"] == envelope["timestamp"]
            ):
                raise IntegrityFailure(
                    "send_readiness_timestamp_invalid",
                    "readiness observation timestamps must equal its envelope timestamp",
                )
            observed_envelopes.add(envelope_id)
            continue
        if record["kind"] in ASK_RECORD_KINDS:
            _validate_ask_event(record, ask_questions, ask_resolved)
            continue
        original_id = str(record["retracted_message_id"])
        original = prior_by_id.get(original_id)
        if original is None:
            raise IntegrityFailure("message_retraction_order_invalid", "retraction evidence must follow its original message")
        if original.get("worker_session_id") != record["worker_session_id"]:
            raise IntegrityFailure("message_retraction_session_invalid", "retraction worker session must equal its original message session")
        if record["author"] not in {original["sender"], original["recipient"]}:
            raise IntegrityFailure("message_retraction_party_invalid", "retraction author must be an original message party")
        if original_id in retracted:
            raise IntegrityFailure("message_retraction_duplicate", "a message can have one append-only retraction")
        retracted.add(original_id)


class EventLog:
    def __init__(self, root: FloatiRoot, registry: Optional[Registry] = None) -> None:
        self.root = root
        self.registry = registry or Registry(root)
        self.relative_path = Path("events.jsonl")
        self.denial_relative_path = Path("receipts/denials.jsonl")
        self.path = root.resolve_relative(self.relative_path)
        self.denial_path = root.resolve_relative(self.denial_relative_path)

    def delivery_path_for(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> Path:
        return self.root.resolve_relative(
            self._delivery_relative_path(recipient, worker_session_id=worker_session_id)
        )

    def records(self) -> List[Dict[str, object]]:
        """Return retained message envelopes, including messages later retracted."""

        records, _unrecognized = read_records_compatible_sealed_snapshot(
            self.root, self.relative_path, allowed_kinds=set(EVENT_KINDS)
        )
        self._validate_event_records(records)
        return [
            record
            for record in records
            if record["kind"] == "message_envelope"
        ]

    @staticmethod
    def _claim_semantics(record: Mapping[str, object]) -> Dict[str, object]:
        return {
            field: record[field]
            for field in (
                "kind", "schema_version", "sha", "repo_path", "bank",
                "declared", "artifacts", "deadline_seconds",
            )
        }

    def _delivery_claim(
        self,
        claim: object,
        *,
        envelope_id: str,
        envelope_sha: str,
    ) -> Dict[str, object]:
        if not isinstance(claim, Mapping):
            raise ProtocolRefusal(
                "claim_not_object", "delivery claim document must be a JSON object"
            )
        expected = {
            "kind", "schema_version", "sha", "repo_path", "bank",
            "declared", "artifacts", "deadline_seconds",
        }
        if set(claim) != expected:
            raise ProtocolRefusal(
                "claim_fields_invalid",
                "delivery claim document fields do not match the v0 intake contract",
            )
        if claim.get("sha") != envelope_sha:
            raise ProtocolRefusal(
                "claim_sha_mismatch",
                "delivery claim sha must equal the enclosing send sha",
            )
        record: Dict[str, object] = {
            "schema_version": claim.get("schema_version"),
            "id": "delivery-claim-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": claim.get("kind"),
            "sha": claim.get("sha"),
            "repo_path": claim.get("repo_path"),
            "bank": claim.get("bank"),
            "declared": claim.get("declared"),
            "artifacts": claim.get("artifacts"),
            "note_ref": envelope_id,
            "deadline_seconds": claim.get("deadline_seconds"),
        }
        return validate_record(
            record,
            self.root.tenant_id,
            frozenset({"delivery_claim"}),
            integrity=False,
        )

    def event_records(self, *, sealed: bool = False) -> List[Dict[str, object]]:
        """Known event frames; well-formed future kinds are skipped (SKEW-2)."""

        records, _unrecognized = self.compatible_event_records(sealed=True) if sealed else self.compatible_event_records()
        return records

    def strict_event_records(self) -> List[Dict[str, object]]:
        """Authoritative read that refuses any kind outside EVENT_KINDS."""

        records = read_records(self.root, self.relative_path, allowed_kinds=EVENT_KINDS)
        self._validate_event_records(records)
        return records

    def skip_receipt(self) -> Optional[Dict[str, object]]:
        """Name one unknown event kind this reader skipped, with the reader schema."""

        from .records import READER_VERSION

        _records, unrecognized = self.compatible_event_records()
        if not unrecognized:
            return None
        first = unrecognized[0]
        return {
            "kind": str(first["kind"]),
            "reader_schema_version": READER_VERSION,
            "first_id": str(first["first_id"]),
            "ledger": self.relative_path.as_posix(),
        }

    def compatible_event_records(
        self, *, snapshot: bool = False, sealed: bool = False
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        """Read known event testimony without letting future kinds darken the bus."""

        reader = (read_records_compatible_sealed_snapshot if sealed
                  else read_records_compatible_snapshot if snapshot else read_records_compatible)
        records, unrecognized = reader(
            self.root, self.relative_path, allowed_kinds=set(EVENT_KINDS)
        )
        self._validate_event_records(records)
        return records, unrecognized

    def _compatible_event_records_with_skew(
        self, *, snapshot: bool = False
    ) -> Tuple[
        List[Dict[str, object]],
        List[Dict[str, object]],
        Optional[VocabularySkewFact],
    ]:
        records, unrecognized, versions = read_records_compatible_with_versions(
            self.root,
            self.relative_path,
            allowed_kinds=set(EVENT_KINDS),
            snapshot=snapshot,
        )
        self._validate_event_records(records)
        return records, unrecognized, vocabulary_skew_fact(versions)

    @staticmethod
    def _validate_event_records(records: Sequence[Dict[str, object]]) -> None:
        """Reject a schema-valid event prefix that cannot be semantically replayed."""
        validate_event_records(records)

    @staticmethod
    def _session_component(worker_session_id: str) -> str:
        if (
            not isinstance(worker_session_id, str)
            or not worker_session_id
            or len(worker_session_id) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in worker_session_id)
        ):
            raise ProtocolRefusal(
                "worker_session_id_invalid",
                "worker session must be a bounded opaque non-control string",
            )
        return hashlib.sha256(worker_session_id.encode("utf-8")).hexdigest()

    def _delivery_relative_path(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> Path:
        node = validate_identifier(recipient, "recipient")
        if worker_session_id is None:
            return Path("receipts/deliveries") / f"{node}.jsonl"
        return (
            Path("receipts/deliveries")
            / node
            / f"{self._session_component(worker_session_id)}.jsonl"
        )

    @staticmethod
    def _normalized_attempt_binding(value: object) -> object:
        """Persist only the reviewer's exact full binding or literal legacy sentinel."""

        if not isinstance(value, dict) or set(value) != _ATTEMPT_BINDING_FIELDS:
            return "absent_legacy"
        return dict(value)

    @staticmethod
    def _reply_refusal_detail(
        sender: str,
        recipient: str,
        reply_to: object,
        prior_events: Sequence[Dict[str, object]],
        original: Optional[Dict[str, object]],
    ) -> Dict[str, str]:
        """Advice from the checked snapshot, never a rewritten authored reply."""

        coordinate = reply_to
        if (
            not isinstance(coordinate, str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", coordinate) is None
        ):
            coordinate = "invalid-" + hashlib.sha256(repr(reply_to).encode("utf-8")).hexdigest()[:16]
        newest = next(
            (row for row in reversed(prior_events)
             if row.get("kind") == "message_envelope"
             and row.get("sender") == recipient and row.get("recipient") == sender),
            None,
        )
        remedy = (
            "omit --reply-to to start a new thread" if newest is None
            else "retry send with --reply-to " + str(newest["id"])
        )
        case = "unknown_reply" if original is None else (
            "own_outgoing" if original["sender"] == sender else "other_parties"
        )
        return {"case": case, "requested_reply_to": coordinate, "remedy": remedy}

    def _deny(
        self, sender: str, recipient: str, reason_code: str,
        *, detail: Optional[Dict[str, str]] = None,
    ) -> None:
        def safe_claim(value: object) -> str:
            if isinstance(value, str) and 1 <= len(value) <= 64:
                return value
            digest = hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:16]
            return "invalid-" + digest
        denial: Dict[str, object] = {
            "schema_version": 0,
            "id": "denial-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "denial_receipt",
            "attempt_id": "attempt-" + uuid7_hex(),
            "claimed_sender": safe_claim(sender),
            "claimed_recipient": safe_claim(recipient),
            "reason_code": reason_code,
        }
        message = f"message refused: {reason_code}"
        if detail is not None:
            denial["detail"] = detail
            case_text = {
                "unknown_reply": "was not found",
                "own_outgoing": "was sent by you",
                "other_parties": "is between other parties",
            }[detail["case"]]
            message += f"; {detail['requested_reply_to']} {case_text}"
        append_record(self.root, self.denial_relative_path, denial, allowed_kinds={"denial_receipt"})
        refusal = ProtocolRefusal(
            reason_code, message, remedy=None if detail is None else detail["remedy"]
        )
        if detail is not None:
            refusal.refusal_detail = dict(detail)
        raise refusal

    @automatic_epoch_operation("send")
    @shared_epoch_operation(selected_writer=True)
    def _send_guarded(
        self,
        sender: str,
        recipient: str,
        repo: str,
        sha: str,
        doc: str,
        note: str,
        *,
        reply_to: Optional[str] = None,
        message_class: str = "normal",
        idempotency_key: Optional[str] = None,
        worker_session_id: Optional[str] = None,
        attempt_binding: object = None,
        claim: object = None,
        address_preview: object = None,
        now: Optional[datetime] = None,
        _execution_publication: object = None,
        _teardown_report: bool = False,
    ) -> Dict[str, object]:
        try:
            return self._send_impl(
                sender,
                recipient,
                repo,
                sha,
                doc,
                note,
                reply_to=reply_to,
                message_class=message_class,
                idempotency_key=idempotency_key,
                worker_session_id=worker_session_id,
                attempt_binding=attempt_binding,
                claim=claim,
                address_preview=address_preview,
                now=now,
                _execution_publication=_execution_publication,
                _teardown_report=_teardown_report,
            )
        except ProtocolRefusal as exc:
            rebound = bind_send_refusal_remedy(exc)
            if rebound is exc:
                raise
            raise rebound from exc

    def send(
        self, sender: str, recipient: str, repo: str, sha: str, doc: str, note: str,
        *, reply_to: Optional[str] = None, message_class: str = "normal",
        idempotency_key: Optional[str] = None, worker_session_id: Optional[str] = None,
        attempt_binding: object = None, claim: object = None, address_preview: object = None,
        now: Optional[datetime] = None, _execution_publication: object = None,
        _teardown_report: bool = False,
    ) -> Dict[str, object]:
        result = self._send_guarded(sender, recipient, repo, sha, doc, note,
            reply_to=reply_to, message_class=message_class, idempotency_key=idempotency_key,
            worker_session_id=worker_session_id, attempt_binding=attempt_binding,
            claim=claim, address_preview=address_preview, now=now,
            _execution_publication=_execution_publication, _teardown_report=_teardown_report)
        # The receiving hook needs the native writer locks. Never wait for its
        # presentation from inside the send transaction or epoch maintenance.
        from .claude_inbox import notify_send
        try:
            observation = notify_send(self.root, result["message"])
        except (OSError, ProtocolRefusal) as exc:
            # The envelope already exists. An optional wake failure cannot
            # turn that append into a before-mutation send refusal.
            observation = {"status": "fallback", "code": "claude_inbox_unavailable",
                           "reason": getattr(exc, "code", "state_io_error")}
        if observation is not None:
            result["claude_inbox"] = observation
        return result

    # Existing epoch qualification uses this seam to bypass automatic
    # maintenance while retaining writer admission. Preserve that contract.
    send.__wrapped__ = _send_guarded.__wrapped__

    def _send_impl(
        self,
        sender: str,
        recipient: str,
        repo: str,
        sha: str,
        doc: str,
        note: str,
        *,
        reply_to: Optional[str] = None,
        message_class: str = "normal",
        idempotency_key: Optional[str] = None,
        worker_session_id: Optional[str] = None,
        attempt_binding: object = None,
        claim: object = None,
        address_preview: object = None,
        now: Optional[datetime] = None,
        _execution_publication: object = None,
        _teardown_report: bool = False,
    ) -> Dict[str, object]:
        if message_class not in ("normal", "fyi"):
            raise ProtocolRefusal("message_class_invalid", "message class must be normal or fyi",
                remedy="send with --message-class normal or --message-class fyi")
        if _execution_publication is not None:
            from .seat_execution import _ExecutionPublication
            if type(_execution_publication) is not _ExecutionPublication or not _execution_publication.authorized(self.root):
                raise ProtocolRefusal("seat_execution_controller_only", "execution companion requires its native controller",
                    remedy="use the authenticated native execution producer; caller-created companions have no authority")
        def execution_matches(records, envelope):
            saved = next((r for r in records if r.get("kind") == "seat_execution"
                          and r["envelope_id"] == envelope["id"]), None)
            return ((_execution_publication is None and saved is None)
                    or (_execution_publication is not None and saved is not None
                        and _execution_publication.matches(saved)))
        # Node spellings are lexical input, not a claim eligible for durable
        # denial evidence.  Validate both parties before touching registry or
        # receipt state so malformed CLI values have a zero-state refusal.
        sender = self.registry.resolve_node_id(
            sender, field="sender", unknown_code="unknown_sender",
        )
        try:
            recipient = self.registry.resolve_node_id(
                recipient, field="recipient", unknown_code="unknown_recipient",
            )
        except ProtocolRefusal as exc:
            if exc.code != "unknown_recipient":
                raise
            roster = ", ".join(self.registry.active_node_ids()) or "(none)"
            raise ProtocolRefusal(
                "recipient_unregistered",
                f"message refused: recipient {recipient!r} is not registered; "
                f"registered nodes: {roster}",
            ) from exc
        # Validate an explicit clock during preflight. A default observation
        # clock is captured only for a fresh send under the recipient guard;
        # exact replay never needs a new observation instant.
        observed_at = None if now is None else _send_observed_at(now)
        key = ("idempotency-" + uuid7_hex()) if idempotency_key is None else idempotency_key
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ProtocolRefusal("idempotency_key_invalid", "idempotency key must contain 1 to 128 characters")
        binding = self._normalized_attempt_binding(attempt_binding)
        session = worker_session_id
        if isinstance(binding, dict) and session is None:
            session = binding["worker_session_id"]  # validator proves this opaque value
        envelope: Dict[str, object] = {
            "schema_version": 0,
            "id": "msg-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            # This fixed stamp admits argument/claim validation before any
            # mutation. For a default clock it is replaced in the fresh-send
            # branch before publication; it never reaches a transaction or receipt.
            "timestamp": observed_at or "1970-01-01T00:00:00.000Z",
            "kind": "message_envelope",
            "sender": sender,
            "recipient": recipient,
            "repo": repo,
            "sha": sha,
            "doc": doc,
            "note": note,
            "idempotency_key": key,
            "attempt_binding": binding,
        }
        if message_class != "normal":
            envelope["message_class"] = message_class
        if session is not None:
            envelope["worker_session_id"] = session
        if address_preview is not None:
            envelope["address_preview"] = address_preview
        validate_record(envelope, self.root.tenant_id, frozenset({"message_envelope"}), integrity=False)
        delivery_claim = None
        if claim is not None:
            delivery_claim = self._delivery_claim(
                claim, envelope_id=str(envelope["id"]), envelope_sha=sha
            )
        from .send_prefix import selected_records
        prior_events = selected_records(self.root, key, reply_to)
        if reply_to is not None:
            original = next((record for record in prior_events
                if record["kind"] == "message_envelope" and record["id"] == reply_to), None)
            if original is None:
                self._deny(sender, recipient, "reply_to_unknown",
                    detail=self._reply_refusal_detail(sender, recipient, reply_to, self.event_records(), original))
            if original["sender"] != recipient or original["recipient"] != sender:
                self._deny(sender, recipient, "reply_to_parties_mismatch",
                    detail=self._reply_refusal_detail(sender, recipient, reply_to, self.event_records(), original))
            # All other input was validated before cache/lock creation. The
            # reply coordinate now names a validated native envelope, rather
            # than turning a non-message record into a syntax refusal.
            envelope["reply_to"] = reply_to
        payload_fields = (
            "sender", "recipient", "repo", "sha", "doc", "note", "reply_to",
            "worker_session_id", "address_preview",
        )
        def find_replay(
            records: Sequence[Dict[str, object]],
        ) -> Optional[tuple[Dict[str, object], Dict[str, object]]]:
            for record in records:
                if record.get("kind") != "message_envelope":
                    continue
                if record.get("idempotency_key") != key:
                    continue
                prior_claim = next(
                    (
                        row for row in records
                        if row.get("kind") == "delivery_claim"
                        and row.get("note_ref") == record.get("id")
                    ),
                    None,
                )
                if (
                    all(record.get(field) == envelope.get(field) for field in payload_fields)
                    and record.get("message_class", "normal") == message_class
                    and execution_matches(records, record)
                    and record.get("attempt_binding", "absent_legacy")
                    == envelope["attempt_binding"]
                    and (
                        (delivery_claim is None and prior_claim is None)
                        or (
                            delivery_claim is not None
                            and prior_claim is not None
                            and self._claim_semantics(prior_claim)
                            == self._claim_semantics(delivery_claim)
                        )
                    )
                ):
                    return (
                        record if prior_claim is None else {"message": record, "claim": prior_claim},
                        self._saved_readiness(records, record),
                    )
                raise ProtocolRefusal("idempotency_conflict", "idempotency key has different content")
            return None

        def decide(
            records: list[Dict[str, object]],
        ) -> tuple[tuple[Dict[str, object], Dict[str, object]], Sequence[Dict[str, object]]]:
            replay = find_replay(records)
            if replay is not None:
                return replay, ()
            if delivery_claim is None:
                tail = (envelope, observation) + launcher_tail
                if _execution_publication is not None:
                    companion = _execution_publication.companion(envelope)
                    validate_record(companion, self.root.tenant_id, {"seat_execution"}, integrity=False)
                    tail += (companion,)
                return (envelope, readiness), tail
            if _execution_publication is not None:
                raise ProtocolRefusal("seat_execution_claim_unsupported", "execution notice does not accept a delivery claim",
                    remedy="let the native execution producer send its atomic notice without a delivery claim")
            result = {"message": envelope, "claim": delivery_claim}
            return (result, readiness), (envelope, delivery_claim, observation) + launcher_tail

        try:
            from .wake_hold import wake_coordination_guard

            replay = find_replay(prior_events)
            if replay is not None:
                return self._send_receipt(*replay)

            # Observe before taking the event-ledger lock. Both testimony and
            # its append stay under the same recipient guard (epoch -> node
            # -> ledger); the observer must not reacquire this physical guard.
            # Teardown reports are protocol evidence from a paused seat. The
            # teardown transaction already owns the sender's lane lock.
            native_session = (native_launcher_runtime_session()
                              if not _teardown_report and _execution_publication is None
                              and session is None else None)
            launcher_tail = ()
            with ExitStack() as guards:
                nodes = {recipient} if native_session is None else {sender, recipient}
                for node in sorted(nodes):
                    guards.enter_context(wake_coordination_guard(self.root, node))
                prior_events = selected_records(self.root, key, reply_to, previous=prior_events)
                # Frozen testimony wins even if today's observer is absent.
                replay = find_replay(prior_events)
                if replay is not None:
                    durable, saved_readiness = replay
                else:
                    if observed_at is None:
                        observed_at = _send_observed_at(None)
                    envelope["timestamp"] = observed_at
                    operation_now = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                    self.registry.require_protocol_lease(sender, now=operation_now, act="send by sender")
                    self.registry.require_protocol_lease(
                        recipient, now=operation_now, act="send to recipient"
                    )
                    if native_session is not None:
                        launcher = capture_native_launcher(self.root, envelope, native_session)
                        if launcher is not None:
                            launcher_tail = (launcher,)
                    readiness = RecipientReadiness._observe_already_guarded(
                        self.root, recipient, observed_at=observed_at,
                    ).artifact()
                    observation = {
                        "schema_version": 1,
                        "id": "send-readiness-" + uuid7_hex(),
                        "tenant_id": self.root.tenant_id,
                        "timestamp": observed_at,
                        "kind": "send_readiness_observation",
                        "envelope_id": envelope["id"],
                        "sender": sender,
                        "recipient": recipient,
                        "observed_at": observed_at,
                        "readiness": readiness,
                        "idempotency_key": key,
                    }
                    from .jsonl import _transact_seat_execution_records
                    transaction = _transact_seat_execution_records if _execution_publication is not None else transact_records
                    coordinates = (self.root,) if _execution_publication is not None else (self.root, self.relative_path)
                    from .send_prefix import stage_send
                    with stage_send(self.root, EVENT_KINDS, key, reply_to, prior_events, decide) as stage:
                        durable, saved_readiness = transaction(
                            *coordinates,
                            decide,
                            allowed_kinds=EVENT_KINDS,
                            skip_unknown_kinds=True,
                            _send_selection=(key, reply_to, prior_events, stage),
                        )
            return self._send_receipt(durable, saved_readiness)
        except ProtocolRefusal as exc:
            if exc.code == "idempotency_conflict":
                self._deny(sender, recipient, "idempotency_conflict")
            raise

    @staticmethod
    def _saved_readiness(
        records: Sequence[Dict[str, object]], envelope: Mapping[str, object],
    ) -> Dict[str, object]:
        """Read the frozen companion; never invent checks for legacy mail."""

        for record in records:
            if (record["kind"] == "send_readiness_observation"
                    and record["envelope_id"] == envelope["id"]):
                return dict(record["readiness"])
        return {
            "state": "unknown",
            "reason": "historical_readiness_missing",
            "observed_at": str(envelope["timestamp"]),
            "checked_mechanisms": [],
            "mechanism_evidence": [],
        }

    @staticmethod
    def _send_receipt(
        durable: Dict[str, object],
        readiness: Mapping[str, object],
    ) -> SendReceipt:
        result = dict(durable) if "message" in durable else {"message": durable}
        result["recipient_readiness"] = dict(readiness)
        return SendReceipt(result)

    @automatic_epoch_operation("retract")
    @shared_epoch_operation(selected_writer=True)
    def retract(
        self,
        retracted_message_id: str,
        *,
        worker_session_id: str,
        reason: str,
        author: str,
    ) -> Dict[str, object]:
        """Coordinate retraction with any wake decision for its exact namespace."""
        original = next(
            (row for row in self.event_records() if row["kind"] == "message_envelope" and row["id"] == retracted_message_id),
            None,
        )
        if original is None:
            return self._retract_already_guarded(retracted_message_id, worker_session_id=worker_session_id, reason=reason, author=author)
        from .wake_hold import wake_coordination_guard
        with wake_coordination_guard(self.root, str(original["recipient"]), worker_session_id=original.get("worker_session_id")):
            return self._retract_already_guarded(retracted_message_id, worker_session_id=worker_session_id, reason=reason, author=author)

    def _retract_already_guarded(
        self,
        retracted_message_id: str,
        *,
        worker_session_id: str,
        reason: str,
        author: str,
    ) -> Dict[str, object]:
        """Append the reviewer's exact retraction record without deleting its original frame."""

        retraction: Dict[str, object] = {
            "schema_version": 0,
            "id": "ret-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "message_retracted",
            "retracted_message_id": retracted_message_id,
            "worker_session_id": worker_session_id,
            "reason": reason,
            "author": author,
        }
        validate_record(
            retraction,
            self.root.tenant_id,
            frozenset({"message_retracted"}),
            integrity=False,
        )

        def decide(
            records: list[Dict[str, object]],
        ) -> tuple[Dict[str, object], Optional[Dict[str, object]]]:
            self._validate_event_records(records)
            prior_by_id: Dict[str, Dict[str, object]] = {}
            for record in records:
                if record["kind"] == "message_envelope":
                    prior_by_id[str(record["id"])] = record
                    continue
                if record["kind"] != "message_retracted":
                    continue
                if record["retracted_message_id"] == retracted_message_id:
                    if (
                        record["worker_session_id"] == worker_session_id
                        and record["reason"] == reason
                        and record["author"] == author
                    ):
                        return record, None
                    raise ProtocolRefusal(
                        "message_retraction_duplicate",
                        "a message already has an append-only retraction",
                    )
            original = prior_by_id.get(retracted_message_id)
            if original is None:
                raise ProtocolRefusal(
                    "message_retraction_unknown",
                    "retraction must name an existing message",
                )
            if original.get("worker_session_id") != worker_session_id:
                raise ProtocolRefusal(
                    "message_retraction_session_invalid",
                    "retraction worker session must equal its original message session",
                )
            if author not in {original["sender"], original["recipient"]}:
                raise ProtocolRefusal(
                    "message_retraction_party_invalid",
                    "retraction author must be an original message party",
                )
            return retraction, retraction

        return transact(
            self.root,
            self.relative_path,
            decide,
            allowed_kinds=EVENT_KINDS,
            skip_unknown_kinds=True,
        )

    @automatic_epoch_operation("present")
    @shared_epoch_operation(selected_writer=True)
    def present(
        self,
        recipient: str,
        limit: int = MAX_PRESENTATION_ITEMS,
        *,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
        _wake_only: bool = False,
    ) -> Tuple[List[Dict[str, object]], Optional[Dict[str, object]]]:
        """Present under the same recipient/session guard as wake evaluation."""
        recipient = self.registry.resolve_node_id(recipient, field="recipient")
        from .wake_hold import wake_coordination_guard
        with wake_coordination_guard(self.root, recipient, worker_session_id=worker_session_id):
            return self._present_already_guarded(
                recipient,
                limit,
                worker_session_id=worker_session_id,
                now=now,
                _wake_only=_wake_only,
            )

    @automatic_epoch_operation("drain")
    @shared_epoch_operation(selected_writer=True)
    def drain(
        self,
        recipient: str,
        *,
        acting_session_id: str,
        limit: int = MAX_PRESENTATION_ITEMS,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[
        List[Dict[str, object]],
        Optional[Dict[str, object]],
        Optional[Dict[str, object]],
    ]:
        """Present and acknowledge one exact batch under one coordination guard."""

        from .cursor import SparseCursor
        from .wake_control import validate_session_id
        from .wake_hold import wake_coordination_guard

        node = self.registry.resolve_node_id(recipient, field="recipient")
        session = validate_session_id(acting_session_id)
        with wake_coordination_guard(
            self.root, node, worker_session_id=worker_session_id
        ):
            messages, delivery = self._present_already_guarded(
                node,
                limit,
                worker_session_id=worker_session_id,
                now=now,
            )
            if not messages:
                return [], None, None
            acknowledgment = SparseCursor(self.root)._ack_already_guarded(
                node,
                [str(message["id"]) for message in messages],
                acting_session_id=session,
                worker_session_id=worker_session_id,
                now=now,
            )
            return messages, delivery, acknowledgment

    def unacked_ids(self, recipient: str) -> List[str]:
        """Message ids still awaiting acknowledgment for one active recipient."""

        from .cursor import SparseCursor
        from .inbox_prefix import recipient_frames

        node = self.registry.require_active(recipient)["node_id"]
        frames = recipient_frames(self.root, str(node), read_only=True)
        events = [record for record in frames if record["kind"] == "message_envelope"]
        retracted = {
            str(record["retracted_message_id"])
            for record in frames
            if record["kind"] == "message_retracted"
        }
        _delivered, acked = SparseCursor(self.root).state_for(
            str(node), events, retracted_ids=retracted, sealed=True,
        )
        # A person's rejection is terminal: never presented, never acknowledged.
        rejected = rejected_message_ids(self.root, str(node))
        return [
            str(record["id"])
            for record in events
            if record["recipient"] == node
            and record["id"] not in acked
            and record["id"] not in retracted
            and record["id"] not in rejected
        ]

    def empty_inbox(
        self,
        recipient: str,
        *,
        acting_session_id: str,
    ) -> Dict[str, object]:
        """Acknowledge remaining mail for one node and leave the node registered."""

        node = str(self.registry.require_active(recipient)["node_id"])
        acked_ids: List[str] = []
        acknowledgment_ids: List[str] = []
        seen: set[str] = set()
        while True:
            messages, _delivery, acknowledgment = self.drain(
                node, acting_session_id=acting_session_id
            )
            if not messages:
                break
            batch = [str(message["id"]) for message in messages]
            if seen.intersection(batch):
                raise ProtocolRefusal(
                    "node_drain_incomplete",
                    "drain repeated the same unacked batch",
                )
            seen.update(batch)
            acked_ids.extend(batch)
            if acknowledgment is not None:
                acknowledgment_ids.append(str(acknowledgment["id"]))
        leftover = self.unacked_ids(node)
        if leftover:
            raise ProtocolRefusal(
                "node_drain_incomplete",
                "unacked mail: " + ",".join(leftover),
            )
        return {
            "node_id": node,
            "state": "active",
            "registered": True,
            "acked_ids": acked_ids,
            "acknowledgment_ids": acknowledgment_ids,
            "acting_session_id": acting_session_id,
        }

    @shared_epoch_operation(selected_writer=True)
    def present_compatible(
        self,
        recipient: str,
        limit: int = MAX_PRESENTATION_ITEMS,
        *,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[
        List[Dict[str, object]],
        Optional[Dict[str, object]],
        Optional[Dict[str, object]],
    ]:
        """Present known mail while preserving a stamped newer-vocabulary fact."""

        recipient = self.registry.resolve_node_id(recipient, field="recipient")
        from .wake_hold import wake_coordination_guard

        with wake_coordination_guard(
            self.root, recipient, worker_session_id=worker_session_id
        ):
            return self._present_compatible_already_guarded(
                recipient,
                limit,
                worker_session_id=worker_session_id,
                now=now,
            )

    def _present_compatible_already_guarded(
        self,
        recipient: str,
        limit: int = MAX_PRESENTATION_ITEMS,
        *,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[
        List[Dict[str, object]],
        Optional[Dict[str, object]],
        Optional[Dict[str, object]],
    ]:
        recipient = validate_identifier(recipient, "recipient")
        self.registry.require_active(recipient)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_PRESENTATION_ITEMS
        ):
            raise ProtocolRefusal(
                "presentation_limit_invalid", "presentation limit must be 1 through 1000"
            )
        if worker_session_id is not None:
            self._session_component(worker_session_id)
        frames, _unrecognized, skew = self._compatible_event_records_with_skew()
        messages, _payload = self._present_from_frames(
            frames, recipient, limit, worker_session_id=worker_session_id
        )
        if not messages:
            return [], None, None if skew is None else skew.artifact()
        self.registry.require_protocol_lease(
            recipient, now=now, act="fresh delivery to recipient"
        )
        receipt = self._write_delivery_receipt(
            recipient, messages, worker_session_id=worker_session_id
        )
        return messages, receipt, None if skew is None else skew.artifact()

    def _present_already_guarded(
        self,
        recipient: str,
        limit: int = MAX_PRESENTATION_ITEMS,
        *,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
        _wake_only: bool = False,
    ) -> Tuple[List[Dict[str, object]], Optional[Dict[str, object]]]:
        messages = self._read_inbox_already_guarded(
            recipient, limit, worker_session_id=worker_session_id, _wake_only=_wake_only)
        if not messages:
            return [], None
        self.registry.require_protocol_lease(
            recipient, now=now, act="fresh delivery to recipient"
        )
        receipt = self._write_delivery_receipt(
            recipient, messages, worker_session_id=worker_session_id
        )
        return messages, receipt

    def _read_inbox_already_guarded(
        self, recipient: str, limit: int = MAX_PRESENTATION_ITEMS, *,
        worker_session_id: Optional[str] = None, _wake_only: bool = False,
    ) -> List[Dict[str, object]]:
        """The shared inbox reader, without a premature presentation receipt.

        The ordinary peek and native context relay hold the same recipient
        guard. Keep reader/index adoption here so both inherit it together.
        """
        recipient = validate_identifier(recipient, "recipient")
        self.registry.require_active(recipient)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PRESENTATION_ITEMS:
            raise ProtocolRefusal("presentation_limit_invalid", "presentation limit must be 1 through 1000")
        if worker_session_id is not None:
            self._session_component(worker_session_id)
        from .inbox_prefix import recipient_frames
        frames = recipient_frames(self.root, recipient)
        messages, _payload = self._present_from_frames(
            frames, recipient, limit, worker_session_id=worker_session_id,
            _wake_only=_wake_only,
        )
        return messages

    def _write_delivery_receipt(
        self,
        recipient: str,
        messages: Sequence[Dict[str, object]],
        *,
        worker_session_id: Optional[str] = None,
        _bridge_publication: object = None,
        _acting_session_id: Optional[str] = None,
    ) -> Optional[Dict[str, object]]:
        require_presentable(self.root, recipient, messages)
        from .bridge_publication import BridgePublication, deferred_delivery
        proof = None
        if _bridge_publication is not None:
            if (not isinstance(_bridge_publication, tuple) or len(_bridge_publication) != 2
                    or type(_bridge_publication[0]) is not BridgePublication
                    or worker_session_id is not None):
                raise ProtocolRefusal("bridge_publication_invalid", "delivery has no native bridge publication owner",
                    remedy="preserve the exact envelope IDs and recover through the declared native inbox")
            owner, supplied = _bridge_publication
            proof = owner.receipt_proof(self.root, recipient, messages, supplied)
        elif deferred_delivery(self, recipient, messages, worker_session_id):
            # This private bridge query has not flushed its output.  Return
            # the real messages and no receipt; a pending batch is not one.
            return None
        stamp = utc_now()
        from .presenter_liveness import presenter_evidence

        evidence = {"shown": True} if proof is not None else presenter_evidence(
            self.root, recipient, now=_parse_receipt_time(stamp)
        )
        from .claude_inbox import delivery_fallbacks
        diagnostic = delivery_fallbacks(self.root, recipient,
            proof["acting_session_id"] if proof is not None else _acting_session_id,
            messages) if evidence["shown"] and worker_session_id is None else None

        def decide(prior_count: int) -> tuple[Dict[str, object], Dict[str, object]]:
            # BUS-DELIVERY-UNPRESENTED-1: a delivery whose recipient presenter
            # cannot be shown alive from the bus's own receipts at this instant
            # is a distinct kind, never a plain delivery.
            receipt: Dict[str, object] = {
                "schema_version": 0,
                "id": "delivery-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": stamp,
                "kind": "delivery_receipt" if evidence["shown"] else "delivered_unpresented",
                "recipient": recipient,
                "item_ids": [message["id"] for message in messages],
                "presentation_count": prior_count + 1,
            }
            if not evidence["shown"]:
                receipt["presenter_evidence"] = evidence
            if proof is not None:
                receipt["bridge_publication"] = proof
            if diagnostic is not None:
                receipt.update(schema_version=1, claude_inbox=diagnostic)
            return receipt, receipt

        from .cursor_prefix import transact_delivery
        return transact_delivery(self.root, recipient, [message["id"] for message in messages], decide,
            worker_session_id=worker_session_id)

    def stale_send_projection(self) -> List[Dict[str, object]]:
        """Project dead-holder sends from evidence only; this method never renews a lease."""

        projections: List[Dict[str, object]] = []
        for physical_frame, record in enumerate(self.event_records(), 1):
            if record["kind"] != "message_envelope":
                continue
            binding = record.get("attempt_binding")
            if not isinstance(binding, dict):
                continue
            lease_id = binding.get("lease_id")
            if not isinstance(lease_id, str):
                continue
            lease_state = self._dead_holder_state(lease_id)
            if lease_state is None:
                continue
            projections.append(
                {
                    "state": "stale_send",
                    "message_id": record["id"],
                    "holder_lease_id": lease_id,
                    "lease_state_at_projection": lease_state,
                    "physical_frame": physical_frame,
                    "renews": False,
                }
            )
        return projections

    def _dead_holder_state(self, lease_id: str) -> Optional[str]:
        """Map only durable authority/orphan evidence to the reviewer's closed stale states."""

        try:
            subject = validate_identifier(lease_id, "lease_id")
        except ProtocolRefusal:
            return None
        grants = read_records_snapshot(
            self.root,
            Path("authority-grants") / f"{subject}.jsonl",
            allowed_kinds={"authority_grant"},
        )
        if grants:
            latest_state = grants[-1].get("state")
            if latest_state == "expired":
                return "expired"
            if latest_state == "released":
                return "abandoned"
        run_records = read_records_snapshot(
            self.root,
            Path("runs/events.jsonl"),
            allowed_kinds=RUN_KINDS,
        )
        matching = [
            record
            for record in run_records
            if record["kind"] == "supervisor_orphaned"
            and record.get("lease_id") == lease_id
        ]
        if not matching:
            return None
        orphan_class = matching[-1].get("orphan_class")
        if orphan_class == "lease_abandonment":
            return "abandoned"
        if orphan_class in {"owner_loss", "unregister"}:
            return "dead_holder"
        return None

    def _present_full_scan(
        self, recipient: str, limit: int, *, worker_session_id: Optional[str] = None,
        _wake_only: bool = False,
    ) -> tuple[List[Dict[str, object]], Dict[str, object]]:
        frames = self.event_records(sealed=True)
        return self._present_from_frames(
            frames, recipient, limit, worker_session_id=worker_session_id, _wake_only=_wake_only
        )

    def _present_from_frames(
        self,
        frames: Sequence[Dict[str, object]],
        recipient: str,
        limit: int,
        *,
        worker_session_id: Optional[str] = None,
        _wake_only: bool = False,
    ) -> tuple[List[Dict[str, object]], Dict[str, object]]:
        from .cursor import SparseCursor

        events = [record for record in frames if record["kind"] == "message_envelope"]
        retracted_ids = {
            str(record["retracted_message_id"])
            for record in frames
            if record["kind"] == "message_retracted"
        }
        delivered, acknowledged = SparseCursor(self.root).state_for(
            recipient,
            events,
            worker_session_id=worker_session_id,
            retracted_ids=retracted_ids,
            sealed=True,
        )
        matching = [
            record
            for record in events
            if record.get("recipient") == recipient
            and record.get("worker_session_id") == worker_session_id
            and record["id"] not in retracted_ids
            and record["id"] not in acknowledged
        ]
        matching = withhold_unapproved(self.root, recipient, matching)
        if _wake_only:
            from .mail_health import wake_eligible
            matching = [record for record in matching if wake_eligible(record)]
        messages = matching[:limit]
        payload: Dict[str, object] = {
            "recipient": recipient,
            "limit": limit,
            "messages": messages,
            "truncated": len(matching) > limit,
            "delivered_ids": sorted(delivered),
            "acked_ids": sorted(acknowledged),
        }
        if worker_session_id is not None:
            payload["worker_session_id"] = worker_session_id
        return messages, payload
