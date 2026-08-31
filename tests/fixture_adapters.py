from __future__ import annotations

from floati import fixture_ids as public_ids

import os
import time

from floati.conformance import AdapterResult, ReferenceAdapter


class FirstCallAdapter(ReferenceAdapter):
    outcome = "ok"

    def register(self, node_id, role):
        if self.outcome == "dead":
            raise RuntimeError("fixture adapter stopped")
        if self.outcome == "no_result":
            return None
        if self.outcome == "malformed":
            return {"status": "ok"}
        if self.outcome == "intentional_silence":
            return AdapterResult("intentional_silence", {})
        if self.outcome == "hang":
            time.sleep(60)
        if self.outcome == "process_death":
            os._exit(17)
        return super().register(node_id, role)


class BehavioralFailureAdapter(ReferenceAdapter):
    def send(self, sender, recipient, repo, sha, doc, note, idempotency_key):
        result = super().send(sender, recipient, repo, sha, doc, note, idempotency_key)
        if result.status == "ok":
            evidence = dict(result.evidence)
            evidence["message"] = dict(evidence["message"])
            evidence["message"]["kind"] = "wrong_kind"
            return AdapterResult("ok", evidence)
        return result


class NoEventAdapter(ReferenceAdapter):
    """Fabricates the successful message protocol without creating an event ledger."""

    def __init__(self, root):
        super().__init__(root)
        self._fabricated_messages = []

    def send(self, sender, recipient, repo, sha, doc, note, idempotency_key):
        if (sender, recipient) != (public_ids.worker('alpha'), "bob"):
            return super().send(sender, recipient, repo, sha, doc, note, idempotency_key)
        message = {
            "id": "fabricated-" + idempotency_key,
            "kind": "message_envelope",
            "sender": sender,
            "recipient": recipient,
            "repo": repo,
            "sha": sha,
            "doc": doc,
            "note": note,
        }
        self._fabricated_messages.append(message)
        return AdapterResult("ok", message)

    def present(self, recipient):
        if recipient == "bob":
            return AdapterResult(
                "ok",
                {
                    "messages": list(self._fabricated_messages),
                    "receipt": {"kind": "delivery_receipt"},
                },
            )
        return super().present(recipient)

    def acknowledge(self, recipient, item_ids):
        if recipient == "bob":
            acknowledged = set(item_ids)
            self._fabricated_messages = [
                message
                for message in self._fabricated_messages
                if message["id"] not in acknowledged
            ]
            return AdapterResult("ok", {"kind": "ack_receipt", "item_ids": list(item_ids)})
        return super().acknowledge(recipient, item_ids)


def conformant(root):
    return ReferenceAdapter(root)


def behavioral_failure(root):
    return BehavioralFailureAdapter(root)


def no_event(root):
    return NoEventAdapter(root)


def dead(root):
    adapter = FirstCallAdapter(root)
    adapter.outcome = "dead"
    return adapter


def intentional_silence(root):
    adapter = FirstCallAdapter(root)
    adapter.outcome = "intentional_silence"
    return adapter


def no_result(root):
    adapter = FirstCallAdapter(root)
    adapter.outcome = "no_result"
    return adapter


def malformed(root):
    adapter = FirstCallAdapter(root)
    adapter.outcome = "malformed"
    return adapter


def hang(root):
    adapter = FirstCallAdapter(root)
    adapter.outcome = "hang"
    return adapter


def process_death(root):
    adapter = FirstCallAdapter(root)
    adapter.outcome = "process_death"
    return adapter
