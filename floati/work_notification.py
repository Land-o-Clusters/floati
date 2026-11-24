"""Write-ahead work notification testimony, separate from work and mail."""

from datetime import datetime, timezone
from contextlib import nullcontext
from pathlib import Path
import re

from .errors import FloatiError, IntegrityFailure, ProtocolRefusal
from .car_fence import car_acceptance_guard
from .events import EventLog, EVENT_KINDS, validate_event_records
from .framing import decode_frames
from .ids import uuid7_hex
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, read_records_snapshot, transact
from .records import validate_record


ADD_NOTE = "Work available: {item}"
COMPLETE_NOTE = "Work completed: {item}"
KINDS = {"work_notification_record"}
DIRECTORY = Path("receipts/work-notifications")


def _conflict(detail):
    return ProtocolRefusal("idempotency_conflict", detail)


def _absence(transition, dispatcher, bindings):
    missing, flags = [], []
    if dispatcher is None:
        missing.append("dispatcher")
        flags.append("--as")
    if not bindings:
        missing.append("artifact")
        flags.extend(("--repo", "--sha", "--doc"))
    if not missing:
        return []
    return [{"code": "work_" + transition + "_unnotified", "missing": missing,
             "missing_flags": flags,
             "remedy": "author notification inputs with " + " ".join(flags)
                       + (" on work add" if dispatcher is None else " on work " + transition)}]


class WorkNotifications:
    def __init__(self, root):
        self.root = root
        self._live = None
        self._archive = None

    @staticmethod
    def relative(item_id):
        if not isinstance(item_id, str) or re.fullmatch(r"work-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}", item_id) is None:
            raise ProtocolRefusal("work_item_id_invalid", "notification requires a work UUIDv7 id")
        return DIRECTORY / (item_id + ".jsonl")

    def records(self, item_id):
        rows = read_records_snapshot(self.root, self.relative(item_id), allowed_kinds=KINDS)
        intent, slots = None, {}
        for row in rows:
            if row["work_item_id"] != item_id:
                raise _conflict("notification journal names a different work item")
            if row["phase"] == "intent":
                if intent is not None or slots:
                    raise _conflict("notification journal has conflicting authoring intents")
                intent = row
            else:
                if intent is None or row["dispatcher"] != intent["dispatcher"]:
                    raise _conflict("notification sent row has no matching authoring intent")
                slot = (row["transition"], row["recipient"])
                if slot in slots:
                    raise _conflict("notification journal repeats a sent coordinate")
                slots[slot] = row
        return rows

    def _record(self, item_id, **fields):
        return {"schema_version": 0, "id": "work-notification-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "kind": "work_notification_record", "work_item_id": item_id, **fields}

    def intent(self, item_id, dispatcher):
        row = self._record(item_id, phase="intent", transition="add", dispatcher=dispatcher,
                           recipient=None, transition_id=None, envelope_id=None, idempotency_key=None)
        validate_record(row, self.root.tenant_id, frozenset(KINDS), integrity=False)

        def decide(rows):
            if rows:
                if rows[0]["phase"] == "intent" and rows[0]["dispatcher"] == dispatcher:
                    return rows[0], None
                raise _conflict("notification intent already names different content")
            return row, row
        return transact(self.root, self.relative(item_id), decide, allowed_kinds=KINDS)

    def sent(self, item_id, intent, notice, envelope):
        row = self._record(item_id, phase="sent", transition=notice["transition"],
                           dispatcher=intent["dispatcher"], recipient=notice["recipient"],
                           transition_id=notice["transition_id"], envelope_id=envelope["id"],
                           idempotency_key=notice["idempotency_key"])

        def decide(rows):
            if not rows or rows[0]["id"] != intent["id"]:
                raise _conflict("notification intent changed before sent append")
            for prior in rows:
                if prior["phase"] == "sent" and (prior["transition"], prior["recipient"]) == (row["transition"], row["recipient"]):
                    if any(prior[k] != row[k] for k in row if k not in {"id", "timestamp"}):
                        raise _conflict("notification sent coordinate names different content")
                    return prior, None
            return row, row
        return transact(self.root, self.relative(item_id), decide, allowed_kinds=KINDS)

    def _events(self, *, archived=False):
        if self._live is None:
            self._live = EventLog(self.root).compatible_event_records(snapshot=True)[0]
        if not archived:
            return self._live
        if self._archive is None:
            archive_rows = []
            if self._live and self._live[0]["kind"] == "bus_epoch_roll_receipt":
                from .epoch_history import read_history
                for epoch in read_history(self.root, max_records=MAX_LEDGER_RECORDS, max_bytes=MAX_LEDGER_BYTES):
                    rows = decode_frames(epoch.read_member("events.jsonl"))
                    rows = [validate_record(r, self.root.tenant_id, frozenset(EVENT_KINDS), integrity=True) for r in rows]
                    validate_event_records(rows)
                    archive_rows.extend(rows)
            self._archive = archive_rows
        return self._archive

    @staticmethod
    def notice(item_id, transition, sender, recipient, binding, transition_id=None):
        return {"transition": transition, "transition_id": transition_id,
                "sender": sender, "recipient": recipient, **binding,
                "note": (ADD_NOTE if transition == "add" else COMPLETE_NOTE).format(item=item_id),
                "idempotency_key": "work-" + transition + "-" + item_id + "-" + recipient}

    @staticmethod
    def _matches(envelope, notice):
        return (all(envelope.get(k) == notice[k] for k in
                    ("sender", "recipient", "repo", "sha", "doc", "note", "idempotency_key"))
                and envelope.get("message_class", "normal") == "normal"
                and envelope.get("reply_to") is None
                and envelope.get("worker_session_id") is None
                and envelope.get("address_preview") is None
                and envelope.get("attempt_binding", "absent_legacy") == "absent_legacy")

    def envelope(self, notice, envelope_id=None):
        for archived in (False, True):
            for row in self._events(archived=archived):
                if row["kind"] != "message_envelope":
                    continue
                if envelope_id is not None:
                    candidate = row["id"] == envelope_id
                else:
                    candidate = row.get("idempotency_key") == notice["idempotency_key"]
                if candidate:
                    if not self._matches(row, notice):
                        raise _conflict("notification envelope names different content")
                    return row
        if envelope_id is not None:
            raise _conflict("notification sent row names an absent envelope")
        return None

    def plan(self, work, item_id, *, now=None):
        rows = self.records(item_id)
        intent = next((r for r in rows if r["phase"] == "intent"), None)
        work_rows = read_records_snapshot(self.root, work.relative_path, allowed_kinds={"work_item", "work_transition"})
        item = next((r for r in work_rows if r["kind"] == "work_item" and r["id"] == item_id), None)
        if item is None and any(r["phase"] == "sent" for r in rows):
            raise _conflict("notification sent row names an absent work item")
        if item is None or intent is None:
            return rows, intent, item, [], []
        completion = next((r for r in work_rows if r["kind"] == "work_transition"
                           and r["work_item_id"] == item_id and r["action"] == "complete"), None)
        notices = []
        if item["artifact_bindings"]:
            for recipient in work.registry.active_node_ids():
                if work.show(item_id, actor=recipient, now=now)[0]["eligibility"]["eligible"]:
                    notices.append(self.notice(item_id, "add", intent["dispatcher"], recipient, item["artifact_bindings"][0]))
        if completion is not None and completion["artifact_bindings"]:
            notices.append(self.notice(item_id, "complete", completion["actor"], intent["dispatcher"],
                                       completion["artifact_bindings"][0], completion["id"]))
        # A sent row remains authoritative even after eligibility changes.
        for row in rows:
            if row["phase"] != "sent":
                continue
            source = item if row["transition"] == "add" else completion
            if source is None or not source["artifact_bindings"]:
                raise _conflict("notification sent row has no authoring artifact")
            expected = self.notice(item_id, row["transition"], intent["dispatcher"] if row["transition"] == "add" else source["actor"],
                                   row["recipient"], source["artifact_bindings"][0],
                                   None if row["transition"] == "add" else source["id"])
            if (row["idempotency_key"] != expected["idempotency_key"] or row["transition_id"] != expected["transition_id"]
                    or (row["transition"] == "complete" and row["recipient"] != intent["dispatcher"])):
                raise _conflict("notification sent row differs from its authoring coordinate")
            self.envelope(expected, row["envelope_id"])
        sent = {(r["transition"], r["recipient"]) for r in rows if r["phase"] == "sent"}
        pending = [n for n in notices if (n["transition"], n["recipient"]) not in sent]
        return rows, intent, item, pending, ([] if completion is None else [completion])

    @staticmethod
    def pending(item_id, notice):
        return {"code": "work_notification_pending", "item": item_id,
                "recipient": notice["recipient"], "sender": notice["sender"],
                "remedy": "floati work notify --id " + item_id + " --as " + notice["sender"]}

    def replay(self, work, item_id, actor, *, now=None, already_guarded=False):
        with nullcontext() if already_guarded else car_acceptance_guard(self.root):
            return self._replay(work, item_id, actor, now=now)

    def _replay(self, work, item_id, actor, *, now=None):
        rows, intent, item, pending, _completion = self.plan(work, item_id, now=now)
        findings = []
        for notice in pending:
            if notice["sender"] != actor:
                findings.append(self.pending(item_id, notice))
                continue
            try:
                envelope = self.envelope(notice)
                # Live replay belongs to EventLog.send, including its claim
                # and execution-companion checks. Only archived-only recovery
                # may link without calling send in the new epoch.
                if envelope is None or any(r["id"] == envelope["id"] for r in self._events()):
                    envelope = EventLog(self.root).send(
                        notice["sender"], notice["recipient"], notice["repo"], notice["sha"],
                        notice["doc"], notice["note"], idempotency_key=notice["idempotency_key"],
                        message_class="normal", now=now)
                self.sent(item_id, intent, notice, envelope)
                self._live = self._archive = None
            except ProtocolRefusal as exc:
                if exc.code == "idempotency_conflict":
                    raise
                findings.append({"code": "work_notification_refused", "recipient": notice["recipient"],
                                 "refusal_code": exc.code, "remedy": exc.remedy})
        return {"notifications": self.records(item_id), "findings": findings}


def _finding(code, severity, subject, detail, **fields):
    # Keep path-bearing findings at the existing, reviewed authoring boundary.
    from .doctor import _finding as doctor_finding

    finding = doctor_finding(code, severity, subject, detail)
    finding.pop("remediation")
    finding.update(fields)
    return finding


def project_notification_findings(root, *, now=None):
    """Read every named journal independently; an unreadable one hides no peers."""
    from .work import WorkLog
    journal, work = WorkNotifications(root), WorkLog(root)
    findings = []
    try:
        directory = root.resolve_relative(DIRECTORY)
        if not directory.exists():
            return findings
        paths = sorted(directory.glob("*.jsonl"))
    except (FloatiError, OSError) as exc:
        return [_finding("work_notification_unreadable", "error", root.tenant_home / DIRECTORY, str(exc))]
    for path in paths:
        item_id = path.stem
        try:
            rows, intent, item, pending, _completion = journal.plan(work, item_id, now=now)
            if intent is not None and item is None:
                findings.append(_finding("work_notification_intent_orphan", "ok", item_id,
                                         "intent has no work item; replay is inert", count=1))
            if pending:
                finding = journal.pending(item_id, pending[0])
                finding.update(severity="warning", subject=item_id, detail="notification journal has pending mail",
                               pending=[journal.pending(item_id, n) for n in pending])
                findings.append(finding)
        except (FloatiError, OSError, ValueError) as exc:
            conflict = getattr(exc, "code", None) == "idempotency_conflict"
            findings.append(_finding("work_notification_conflict" if conflict else "work_notification_unreadable",
                                     "error", item_id if conflict else path, str(exc)))
    return findings
