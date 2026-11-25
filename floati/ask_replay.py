"""Read one ask receipt chain from the current and receipt-owned epochs."""

from __future__ import annotations

import re

from .ask_cost import project_ask_cost
from .ask_git import compare_ask_commits
from .bus_epoch import epoch_guard
from .errors import IntegrityFailure, ProtocolRefusal
from .events import validate_event_records
from .records import ASK_RECORD_KINDS, _UUID7
from .replay_history import event_epochs, project_asked


_QUESTION_ID = re.compile("ask-question-" + _UUID7)


def replay_ask(root, question_id: str, *, repository: str | None = None) -> dict:
    if not _QUESTION_ID.fullmatch(question_id):
        raise ProtocolRefusal(
            "ask_question_id_invalid",
            "Replay needs an ask question receipt ID.",
            "Pass the question_id returned by ask.",
        )

    chain, gap = _receipt_chain(root, question_id)
    history = {} if gap is None else {"history_gap": gap}
    if not chain:
        return {"question_id": question_id, "code": "ask_question_unknown", **history}
    question = chain[0]
    asked = project_asked(question)
    presentation = next((row for row in chain if row["kind"] == "ask_presentation"), None)
    presented = (
        {"status": "missing", "reason": "ask_presentation_receipt_absent"}
        if presentation is None else {
            "status": "recorded",
            "receipt_id": presentation["id"],
            "question_id": question_id,
            "node": presentation["sender"],
            "session": presentation["acting_session_id"],
            "timestamp": presentation["timestamp"],
        }
    )
    terminal = next((row for row in chain if row["kind"] in {"ask_answer", "ask_decline"}), None)
    answer = terminal if terminal is not None and terminal["kind"] == "ask_answer" else None

    if terminal is None:
        outcome = "UNKNOWN"
        acted = {"status": "missing", "reason": "ask_reply_receipt_absent"}
    else:
        outcome = "answered" if answer is not None else "declined"
        acted = {
            "status": "recorded",
            "outcome": outcome,
            "node": terminal["sender"],
            "receipt_id": terminal["id"],
        }

    return {
        "question_id": question_id,
        "asker": asked["node"],
        "asked": asked,
        "recipient": question["recipient"],
        "outcome": outcome,
        "receipts": [_receipt_entry(row) for row in chain],
        "presented": presented,
        "acted": acted,
        "cost": project_ask_cost(
            root.tenant_id, question_id, asked["node"], answer["id"] if answer else None,
        ),
        "git_change": _git_change(question, answer, repository),
        **history,
    }


def _receipt_chain(root, question_id: str) -> tuple[list[dict], dict | None]:
    rows_by_id = {}
    with epoch_guard(root):
        epochs, gap = event_epochs(root)
        for _archive, rows in epochs:
            for row in rows:
                if row["kind"] not in ASK_RECORD_KINDS:
                    continue
                if row["id"] != question_id and row.get("reply_to") != question_id:
                    continue
                if rows_by_id.setdefault(row["id"], row) != row:
                    raise IntegrityFailure(
                        "ask_receipt_conflict",
                        "One ask receipt ID names different records across epochs.",
                    )

    chain = list(rows_by_id.values())
    # Validate again after coalescing identical carry copies from later epochs.
    validate_event_records(chain)
    return chain, gap


def _receipt_entry(row: dict) -> dict:
    entry = {
        "receipt_id": row["id"],
        "kind": row["kind"],
        "timestamp": row["timestamp"],
    }
    if "reply_to" in row:
        entry["reply_to"] = row["reply_to"]
    if row["kind"] == "ask_timeout":
        entry["node"] = row["observer"]
    else:
        entry["node"] = row["sender"]
        entry["session"] = row["acting_session_id"]
    return entry


def _git_change(question: dict, answer: dict | None, repository: str | None) -> dict:
    def reference(row):
        return {**{key: row[key] for key in ("repo", "sha", "doc")}, "receipt_id": row["id"]}

    before = reference(question)
    if answer is None:
        return {"status": "missing", "reason": "ask_answer_absent", "question": before}
    after = reference(answer)
    if question["repo"] != answer["repo"]:
        status = "different_repository_references"
    elif question["sha"] == answer["sha"]:
        status = "same_commit_reference"
    else:
        status = "different_commit_references"
    return {
        "status": status, "question": before, "answer": after,
        "comparison": compare_ask_commits(repository, before, after),
    }
