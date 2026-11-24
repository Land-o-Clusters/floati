"""Read the receipt chain of one work item without replaying its effects."""

from __future__ import annotations

import re

from .archive_consumption_replay import (
    validate_consumed_ack,
    validate_consumed_envelope,
    validate_consumed_work,
)
from .bus_epoch import epoch_guard
from .consumption import ConsumptionLedger
from .errors import IntegrityFailure, ProtocolRefusal
from .message_consumption import MessageConsumption
from .records import WAKE_HOLD_KINDS, _UUID7
from .replay_history import event_epochs, project_asked, read_epoch_member
from .work_notification import WorkNotifications


_WORK_ID = re.compile("work-" + _UUID7)


def replay_work(root, item_id: str) -> dict:
    if not _WORK_ID.fullmatch(item_id):
        raise ProtocolRefusal(
            "work_item_id_invalid",
            "Replay needs a work item ID.",
            "Pass the ID returned by work add.",
        )
    ledger = ConsumptionLedger(root)
    with epoch_guard(root):
        rows = ledger.records()
        state = ledger.project(rows).get(item_id)
        if state is None:
            return {"work_item_id": item_id, "code": "work_unknown"}
        item = next(row for row in rows if row["id"] == item_id)
        transitions = {row["action"]: row for row in rows if row.get("work_item_id") == item_id}
        claim, completion = transitions.get("claim"), transitions.get("complete")
        journal = WorkNotifications(root)
        notifications = journal.records(item_id)
        intent = next((row for row in notifications if row["phase"] == "intent"), None)
        dispatches = [
            row for row in notifications
            if row["phase"] == "sent" and row["transition"] == "add"
        ]
        notices = {row["envelope_id"]: row for row in dispatches}
        if len(notices) != len(dispatches):
            raise IntegrityFailure("work_replay_receipt_conflict", "A work notice binds the same dispatch more than once.")
        consumption = MessageConsumption(root)
        acts = _linked_acts(consumption, state, notices)
        message_ids = notices.keys() | acts.keys()
        mail, gap = _work_mail(root, consumption, message_ids)
        presented = _presentation(journal, item, intent, notices, mail)
        acted = _actions(root, acts, mail)

    asked = project_asked(item, work_intent=intent)
    cost = {"status": "unmeasured", "reason": "puddle_measured_cost_seam_unavailable"}
    if asked["actor"]["status"] == "recorded":
        cost["attributed_to"] = asked["actor"]["node"]
    result_refs = _git_references(completion, "work_result_git_reference_missing")
    return {
        "work_item_id": item_id,
        "owner": item["owner"],
        "asked": asked,
        "claim": _transition(claim, "work_claim_receipt_missing"),
        "presented": presented,
        "acted": acted,
        "completion": _transition(completion, "work_completion_receipt_missing"),
        "git_change": {
            "status": result_refs["status"],
            "request": _git_references(item, "work_request_git_reference_missing"),
            "completion": result_refs,
        },
        "cost": cost,
        **({} if gap is None else {"history_gap": gap}),
    }


def _linked_acts(consumption, work, notices):
    nodes = {notice["recipient"] for notice in notices.values()}
    if work["holder"] is not None:
        nodes.add(work["holder"])
    acts = {}
    for node in sorted(nodes):
        for receipt in consumption._raw(node, sealed=True):
            message_id = receipt["message_id"]
            notice = notices.get(message_id)
            if receipt["source"] == "work_complete" and receipt["work_item_id"] == work["id"]:
                validate_consumed_work(consumption.root.tenant_id, node, receipt, work)
            elif not (receipt["source"] == "explicit" and notice and notice["recipient"] == node):
                continue
            if acts.setdefault(message_id, receipt) != receipt:
                raise IntegrityFailure("work_replay_receipt_conflict", "A work dispatch has conflicting consumption receipts.")
    return acts


def _work_mail(root, consumption, message_ids):
    mail = {
        key: {"envelope": None, "presentations": {}, "acks": {}, "acks_without_delivery": set()}
        for key in sorted(message_ids)
    }
    if not mail:
        return mail, None
    cursor = consumption.cursor
    epochs, gap = event_epochs(root)
    for archive, events in epochs:
        planes = {}
        for event in events:
            if event["kind"] != "message_envelope" or event["id"] not in mail:
                continue
            item = mail[event["id"]]
            if item["envelope"] is not None and item["envelope"] != event:
                raise IntegrityFailure("work_replay_receipt_conflict", "A work dispatch envelope differs across epochs.")
            item["envelope"] = event
            node, worker = event["recipient"], event.get("worker_session_id")
            if (node, worker) not in planes:
                delivery_path = cursor._delivery_relative_path_for(node, worker_session_id=worker)
                ack_path = cursor._relative_path_for(node, worker_session_id=worker)
                planes[node, worker] = (
                    read_epoch_member(root, archive, delivery_path, WAKE_HOLD_KINDS),
                    read_epoch_member(root, archive, ack_path, {"ack_receipt"}),
                )
            deliveries, acks = planes[node, worker]
            _select_mail_receipts(item, event, deliveries, acks)
    return mail, gap


def _select_mail_receipts(item, event, deliveries, acks):
    delivered = False
    for receipt in deliveries:
        kind = receipt["kind"]
        if kind not in {"delivery_receipt", "delivered_unpresented"} or event["id"] not in receipt["item_ids"]:
            continue
        _check_mail_recipient(receipt, event)
        delivered = True
        if kind == "delivery_receipt":
            _index_mail_receipt(item["presentations"], receipt)
    for ack in acks:
        if event["id"] not in ack["item_ids"]:
            continue
        _check_mail_recipient(ack, event)
        if delivered:
            _index_mail_receipt(item["acks"], ack)
        else:
            item["acks_without_delivery"].add(ack["id"])


def _check_mail_recipient(receipt, event):
    if receipt["recipient"] != event["recipient"]:
        raise IntegrityFailure("work_replay_receipt_conflict", "A work mail receipt differs from its recorded binding.")


def _index_mail_receipt(index, receipt):
    if index.setdefault(receipt["id"], receipt) != receipt:
        raise IntegrityFailure("work_replay_receipt_conflict", "A work mail receipt differs from its recorded binding.")


def _presentation(journal, item, intent, notices, mail):
    if not mail:
        return _missing("work_dispatch_link_missing")
    receipts, gaps = [], []
    for message_id, facts in mail.items():
        notice = notices.get(message_id)
        envelope = facts["envelope"]
        if notice is not None:
            if not item["artifact_bindings"] or notice["transition_id"] is not None:
                raise IntegrityFailure("work_replay_receipt_conflict", "A work notice has no matching request artifact.")
            expected = journal.notice(item["id"], "add", intent["dispatcher"],
                                      notice["recipient"], item["artifact_bindings"][0])
            if notice["idempotency_key"] != expected["idempotency_key"] or (
                envelope is not None and not journal._matches(envelope, expected)
            ):
                raise IntegrityFailure("work_replay_receipt_conflict", "A work notice differs from its recorded dispatch.")
        if envelope is None or not facts["presentations"]:
            reason = "work_dispatch_envelope_missing" if envelope is None else "work_presentation_receipt_missing"
            gaps.append({"message_id": message_id, "reason": reason})
        for row in facts["presentations"].values():
            entry = {**_recorded(row, row["recipient"]), "message_id": message_id}
            if notice is not None:
                entry["notification_receipt_id"] = notice["id"]
            receipts.append(entry)
    return _with_gaps(receipts, gaps)


def _actions(root, acts, mail):
    if not acts:
        return _missing("work_consumption_link_missing")
    receipts, gaps = [], []
    for message_id, row in acts.items():
        facts = mail[message_id]
        node, ack_id = row["recipient"], row["ack_receipt_id"]
        if facts["envelope"] is not None:
            validate_consumed_envelope(root.tenant_id, node, row, facts["envelope"])
        else:
            gaps.append({"message_id": message_id, "reason": "work_dispatch_envelope_missing"})
        ack = facts["acks"].get(ack_id)
        if ack is not None:
            validate_consumed_ack(root.tenant_id, row, [ack])
        else:
            reason = ("work_ack_delivery_receipt_missing" if ack_id in facts["acks_without_delivery"]
                      else "work_ack_receipt_missing")
            gaps.append({"message_id": message_id, "reason": reason})
        receipts.append({
            **_recorded(row, node), "message_id": message_id, "ack_receipt_id": ack_id,
            "session": row["acting_session_id"],
        })
    return _with_gaps(receipts, gaps)


def _with_gaps(receipts, gaps):
    if not receipts:
        return {**_missing(gaps[0]["reason"]), "receipts": [], "gaps": gaps}
    return {"status": "partial" if gaps else "recorded", "receipts": receipts, "gaps": gaps}


def _missing(reason):
    return {"status": "missing", "reason": reason}


def _recorded(row, node):
    return {"status": "recorded", "receipt_id": row["id"], "node": node, "timestamp": row["timestamp"]}


def _transition(row, reason):
    return _missing(reason) if row is None else _recorded(row, row["actor"])


def _git_references(row, reason):
    if row is None or not row["artifact_bindings"]:
        return _missing(reason)
    return {"status": "recorded", "receipt_id": row["id"], "references": row["artifact_bindings"]}
