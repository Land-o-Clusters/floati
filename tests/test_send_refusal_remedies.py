from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati import cli
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records
from floati.registry import Registry
from floati.root import FloatiRoot


class SendRefusalRemedyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        registry = Registry(self.root)
        for node in ("sender", "recipient", "other"):
            registry.register(node, "worker")
        self.log = EventLog(self.root, registry)

    def send(self, sender="sender", recipient="recipient", **kwargs):
        return self.log.send(sender, recipient, "floati", "a" * 40, "docs/evidence/control.md", "notice", **kwargs)

    def denied(self, reply_to, *, sender="sender", recipient="recipient"):
        before = self.log.path.read_bytes() if self.log.path.exists() else b""
        with self.assertRaises(ProtocolRefusal) as caught:
            self.send(sender, recipient, reply_to=reply_to)
        self.assertEqual(before, self.log.path.read_bytes() if self.log.path.exists() else b"")
        denial = read_records(self.root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"})[-1]
        return caught.exception, denial

    def test_own_outgoing_names_case_and_omit_fallback_without_incoming_mail(self):
        outgoing = self.send()
        refusal, denial = self.denied(outgoing["id"])
        self.assertEqual("reply_to_parties_mismatch", refusal.code)
        self.assertEqual("omit --reply-to to start a new thread", refusal.remedy)
        self.assertIn(outgoing["id"], refusal.detail)
        self.assertIn("sent by you", refusal.detail)
        self.assertEqual("own_outgoing", denial["detail"]["case"])
        self.assertEqual(refusal.remedy, denial["detail"]["remedy"])

    def test_remedy_uses_newest_recipient_to_sender_envelope_and_never_sends_it(self):
        outgoing = self.send()
        older = self.send("recipient", "sender")
        newest = self.send("recipient", "sender")
        self.send("other", "sender")
        refusal, denial = self.denied(outgoing["id"])
        self.assertEqual("retry send with --reply-to " + newest["id"], refusal.remedy)
        self.assertNotIn(older["id"], refusal.remedy)
        self.assertEqual(outgoing["id"], denial["detail"]["requested_reply_to"])
        self.assertEqual(4, len(self.log.records()), "the remedy is advice, never an automatic send")

    def test_other_parties_refusal_is_distinct_from_own_outgoing(self):
        unrelated = self.send("recipient", "other")
        refusal, denial = self.denied(unrelated["id"])
        self.assertEqual("reply_to_parties_mismatch", refusal.code)
        self.assertIn("detail", denial, "denial must retain its refusal case")
        self.assertEqual("other_parties", denial["detail"]["case"])
        self.assertIn("between other parties", refusal.detail)
        self.assertEqual("omit --reply-to to start a new thread", refusal.remedy)

    def test_own_outgoing_to_another_recipient_still_names_authorship(self):
        outgoing = self.send("sender", "other")
        refusal, denial = self.denied(outgoing["id"])
        self.assertIn("detail", denial)
        self.assertEqual("own_outgoing", denial["detail"]["case"])
        self.assertIn("sent by you", refusal.detail)

    def test_incoming_from_a_different_party_is_other_parties(self):
        incoming = self.send("other", "sender")
        refusal, denial = self.denied(incoming["id"])
        self.assertIn("detail", denial)
        self.assertEqual("other_parties", denial["detail"]["case"])
        self.assertEqual("omit --reply-to to start a new thread", refusal.remedy)

    def test_unknown_id_is_named_and_remedy_uses_incoming_or_omit_fallback(self):
        unknown = "msg-018f0f23abcd71238000000000000000"
        refusal, denial = self.denied(unknown)
        self.assertEqual("reply_to_unknown", refusal.code)
        self.assertIn(unknown, refusal.detail)
        self.assertEqual("omit --reply-to to start a new thread", refusal.remedy)
        self.assertEqual("unknown_reply", denial["detail"]["case"])
        incoming = self.send("recipient", "sender")
        refusal, _denial = self.denied(unknown)
        self.assertEqual("retry send with --reply-to " + incoming["id"], refusal.remedy)

    def test_cli_prints_one_refusal_with_the_durable_case_and_remedy(self):
        outgoing = self.send()
        output = io.StringIO()
        arguments = ["send", "--root", str(self.root.path), "--from", "sender", "--to", "recipient",
                     "--repo", "floati", "--sha", "a" * 40, "--doc", "docs/evidence/control.md",
                     "--note", "notice", "--reply-to", outgoing["id"]]
        with mock.patch("floati.cli._require_banked_sha"), contextlib.redirect_stdout(output):
            status = cli.main(arguments)
        self.assertEqual(20, status)
        artifact = json.loads(output.getvalue())
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("omit --reply-to to start a new thread", artifact["evidence"]["remedy"])
        denial = read_records(self.root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"})[-1]
        self.assertEqual(denial["detail"], artifact["evidence"]["refusal_detail"])
        self.assertEqual(1, len(self.log.records()))

    def test_valid_reply_keeps_the_original_envelope_bytes_and_replays_exactly(self):
        incoming = self.send("recipient", "sender")
        before = self.log.path.read_bytes()
        reply = self.send(reply_to=incoming["id"], idempotency_key="valid-reply")
        expected = json.dumps(reply["message"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        after = self.log.path.read_bytes()
        self.assertTrue(after.startswith(before), "reply must preserve all prior event bytes")
        appended = after[len(before):].splitlines(keepends=True)
        self.assertEqual(2, len(appended), "one reply appends its envelope and bound observation")
        self.assertEqual(expected, appended[0], "the authored envelope bytes stay exact")
        observation = json.loads(appended[1])
        self.assertEqual("send_readiness_observation", observation["kind"])
        self.assertEqual(reply["message"]["id"], observation["envelope_id"])
        self.assertEqual(reply["recipient_readiness"], observation["readiness"])
        for field in ("sender", "recipient", "idempotency_key", "timestamp"):
            self.assertEqual(reply["message"][field], observation[field])
        self.assertNotIn("detail", reply["message"])
        self.assertEqual(reply, self.send(reply_to=incoming["id"], idempotency_key="valid-reply"))
        self.assertEqual(after, self.log.path.read_bytes())



if __name__ == "__main__":
    unittest.main()
