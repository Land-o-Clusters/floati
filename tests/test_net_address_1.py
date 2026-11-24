"""A group send must use the recipients shown before the roster changes."""

import json
import unittest
from datetime import datetime, timezone

from tests import test_net_ask_1
from tests.test_net_who_1 import _lane
from tests.test_net_who_1 import _claim_car
from floati.root import resolve_command_root
from floati.registry import Registry
from floati.records import validate_record
from floati.errors import ProtocolRefusal
from pathlib import Path
from tests.schema_validation import validate_json_schema
from floati import cli


class NetAddress1Tests(unittest.TestCase):
    def setUp(self):
        self.fleet = test_net_ask_1.NetAsk1Tests()
        self.fleet.setUp()
        self.addCleanup(self.fleet.doCleanups)

    def test_exact_recipient_callers_can_omit_new_preview_options(self):
        """Optional addressing fields must not break pre-existing Python callers."""
        for command in ("send", "ask"):
            for omitted in (("preview",), ("recipient_preview",),
                            ("preview", "recipient_preview")):
                with self.subTest(command=command, omitted=omitted):
                    args = cli._parser().parse_args(self.arguments("blue", command))
                    args.idempotency_key = command + "-" + "-".join(omitted)
                    for field in omitted:
                        delattr(args, field)
                    _status, evidence, code = getattr(cli, "_" + command)(args)
                    self.assertEqual(0 if command == "send" else 34, code)
                    if command == "send":
                        self.assertEqual("blue", evidence["recipient"])
                    else:
                        self.assertEqual("ask_timeout", evidence["code"])
        events = self.fleet.events()
        for kind in ("message_envelope", "ask_question"):
            rows = [row for row in events if row["kind"] == kind]
            self.assertEqual(3, len(rows))
            self.assertTrue(all(row["recipient"] == "blue" for row in rows))
            self.assertTrue(all("address_preview" not in row for row in rows))

    def test_group_callers_without_preview_options_get_the_required_preview_refusal(self):
        """Missing optional fields must never bypass the mandatory group preview."""
        for command in ("send", "ask"):
            with self.subTest(command=command):
                args = cli._parser().parse_args(self.arguments(command=command))
                del args.preview
                del args.recipient_preview
                with self.assertRaises(ProtocolRefusal) as caught:
                    getattr(cli, "_" + command)(args)
                self.assertEqual("address_preview_required", caught.exception.code)
        self.assertEqual([], self.fleet.events())

    def test_node_joining_after_preview_never_receives_the_group_send(self):
        """A changed roster must refuse send and ask before either appends."""
        for command in ("send", "ask"):
            with self.subTest(case=command):
                if command == "ask":
                    self.fleet = test_net_ask_1.NetAsk1Tests()
                    self.fleet.setUp()
                    self.addCleanup(self.fleet.doCleanups)
                fixture = self.fleet
                arguments = self.arguments(command=command)
                snapshot, preview = self.preview(arguments)
                self.assertEqual(["blue"], preview["recipients"])
                added, artifact = fixture.invoke(
                    "node", "add", "--root", str(fixture.fleet),
                    "--node", "green", "--harness", "Codex", "--lifetime", "permanent",
                )
                self.assertEqual(0, added.returncode, artifact)
                completed, artifact = fixture.invoke(
                    *arguments, "--recipient-preview", str(snapshot),
                )
                self.assertEqual(20, completed.returncode, artifact)
                self.assertEqual("address_preview_changed", artifact["evidence"]["code"])
                self.assertEqual([], fixture.events())

    def arguments(self, address="everyone", command="send"):
        fixture = self.fleet
        arguments = [
            command, "--root", str(fixture.fleet),
            "--from", "red", "--to", address,
            "--repo", "floati", "--sha", fixture.sha,
            "--repository", str(fixture.repository),
            "--doc", "note.md", "--note", "Review the local change",
            "--idempotency-key", "group-message",
        ]
        if command == "ask":
            arguments.extend(["--session", "sess-red", "--wait-seconds", "0"])
        return arguments

    def preview(self, arguments):
        completed, artifact = self.fleet.invoke(*arguments, "--preview")
        self.assertEqual(0, completed.returncode, artifact)
        path = self.fleet.base / "preview.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        return path, artifact["evidence"]

    def test_preview_is_read_only_and_send_records_the_exact_list(self):
        """An unbound fanout or an envelope losing its list breaks this contract."""
        arguments = self.arguments()
        before = self.fleet.events()
        path, preview = self.preview(arguments)
        self.assertEqual(["blue"], preview["recipients"])
        self.assertEqual(before, self.fleet.events())
        for _ in range(2):
            sent, artifact = self.fleet.invoke(*arguments, "--recipient-preview", str(path))
            self.assertEqual(0, sent.returncode, artifact)
        messages = [row for row in self.fleet.events() if row["kind"] == "message_envelope"]
        self.assertEqual(["blue"], [row["recipient"] for row in messages])
        self.assertEqual(preview, messages[0]["address_preview"])

    def test_group_requires_preview_and_person_names_its_missing_source(self):
        """No implicit fanout or inferred owner may produce an envelope."""
        for address, code in (("everyone", "address_preview_required"),
                              ("person:chris", "address_person_source_missing")):
            completed, artifact = self.fleet.invoke(*self.arguments(address))
            self.assertEqual(20, completed.returncode, artifact)
            self.assertEqual(code, artifact["evidence"]["code"])
            if address.startswith("person:"):
                self.assertIn("recorded", artifact["evidence"]["detail"])
        self.assertEqual([], self.fleet.events())

    def test_tampered_preview_cannot_add_a_recipient(self):
        """Untrusted preview edits must refuse with a code before append."""
        arguments = self.arguments()
        path, _ = self.preview(arguments)
        original = path.read_text(encoding="utf-8")
        for case in ("added recipient", "non-string command"):
            with self.subTest(case=case):
                artifact = json.loads(original)
                if case == "added recipient":
                    artifact["evidence"]["recipients"].append("red")
                else:
                    artifact["command"] = []
                path.write_text(json.dumps(artifact), encoding="utf-8")
                completed, result = self.fleet.invoke(*arguments, "--recipient-preview", str(path))
                self.assertEqual(20, completed.returncode, result)
                self.assertEqual("address_preview_invalid", result["evidence"]["code"])
                self.assertEqual([], self.fleet.events())

    def test_repository_group_uses_open_recorded_memberships(self):
        """A node with two open repositories must still match either one."""
        root = resolve_command_root(str(self.fleet.fleet))
        now = datetime.now(timezone.utc)
        _lane(root, "blue", "first", "floati", now)
        _lane(root, "blue", "second", "other", now)
        _lane(root, "red", "closed", "unused", now, closed=True)
        path, preview = self.preview(self.arguments("repo:floati"))
        self.assertEqual(["blue"], preview["recipients"])
        sent, artifact = self.fleet.invoke(
            *self.arguments("repo:floati"), "--recipient-preview", str(path),
        )
        self.assertEqual(0, sent.returncode, artifact)
        missing, artifact = self.fleet.invoke(*self.arguments("repo:unused"), "--preview")
        self.assertEqual(20, missing.returncode, artifact)
        self.assertEqual("address_empty", artifact["evidence"]["code"])

    def test_ask_group_records_each_question_and_its_timeout(self):
        """Group asks need per-recipient question and terminal receipts."""
        added, artifact = self.fleet.invoke(
            "node", "add", "--root", str(self.fleet.fleet), "--node", "green",
            "--harness", "Codex", "--lifetime", "permanent",
        )
        self.assertEqual(0, added.returncode, artifact)
        arguments = self.arguments(command="ask")
        path, preview = self.preview(arguments)
        self.assertEqual([], self.fleet.events())
        completed, artifact = self.fleet.invoke(*arguments, "--recipient-preview", str(path))
        self.assertEqual(34, completed.returncode, artifact)
        questions = [row for row in self.fleet.events() if row["kind"] == "ask_question"]
        self.assertEqual(["blue", "green"], [row["recipient"] for row in questions])
        self.assertTrue(all(row["address_preview"] == preview for row in questions))
        self.assertEqual(["blue", "green"], artifact["evidence"]["recipients"])
        self.assertEqual(2, len(artifact["evidence"]["results"]))

    def test_retiring_the_last_recipient_invalidates_the_preview(self):
        """A vanished group must refuse as changed rather than reuse stale recipients."""
        arguments = self.arguments()
        path, _ = self.preview(arguments)
        root = resolve_command_root(str(self.fleet.fleet))
        Registry(root).retire("blue")
        completed, artifact = self.fleet.invoke(*arguments, "--recipient-preview", str(path))
        self.assertEqual(20, completed.returncode, artifact)
        self.assertEqual("address_preview_changed", artifact["evidence"]["code"])
        self.assertEqual([], self.fleet.events())

    def test_claimed_car_membership_is_recorded_without_a_lane(self):
        """Repository groups must include claimed cars, not infer names or require lanes."""
        root = resolve_command_root(str(self.fleet.fleet))
        _claim_car(root, "blue", "car-branch", datetime.now(timezone.utc))
        _, preview = self.preview(self.arguments("repo:floati"))
        self.assertEqual(["blue"], preview["recipients"])

    def test_group_ask_allows_answers_while_waiting(self):
        """Holding the group membership barrier during the wait would deadlock replies."""
        arguments = self.arguments(command="ask")
        arguments[-1] = "5"
        path, preview = self.preview(arguments)
        process = self.fleet.spawn(*arguments, "--recipient-preview", str(path))
        question = self.fleet.wait_for_question(process)
        completed, reply = self.fleet.invoke(*self.fleet.reply_argv(
            question["id"], disposition="answer", key="group-answer", note="Reviewed",
        ))
        self.assertEqual(0, completed.returncode, reply)
        artifact = self.fleet.finish(process, timeout=5)
        self.assertEqual(0, process.returncode, artifact)
        self.assertEqual(preview["recipients"], artifact["evidence"]["recipients"])
        result = artifact["evidence"]["results"][0]
        self.assertEqual("answered", result["outcome"])
        self.assertEqual(reply["evidence"]["answer_id"], result["answer_id"])
        self.assertEqual(reply["evidence"]["cost"], result["cost"])
        self.assertEqual(question["sender"], result["cost"]["attributed_to"])

    def test_envelope_reader_rejects_a_recipient_outside_the_preview(self):
        """Durable metadata cannot claim an audience different from its concrete recipient."""
        arguments = self.arguments()
        path, _ = self.preview(arguments)
        completed, artifact = self.fleet.invoke(*arguments, "--recipient-preview", str(path))
        self.assertEqual(0, completed.returncode, artifact)
        envelope = next(row for row in self.fleet.events() if row["kind"] == "message_envelope")
        validate_json_schema(envelope, Path("schemas/v0/message-envelope.schema.json"))
        envelope["recipient"] = "red"
        root = resolve_command_root(str(self.fleet.fleet))
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_record(envelope, root.tenant_id, frozenset({"message_envelope"}), integrity=False)
        self.assertEqual("address_preview_invalid", caught.exception.code)

    def test_pending_epoch_recovery_blocks_group_writes(self):
        """The exclusive membership barrier must not grant recovery-writer privileges."""
        from floati.bus_epoch import EPOCH_MARKER_PREFIX, EPOCH_MARKER_SUFFIX

        for command in ("send", "ask"):
            arguments = self.arguments(command=command)
            path, _ = self.preview(arguments)
            root = resolve_command_root(str(self.fleet.fleet))
            marker = root.tenant_home / (EPOCH_MARKER_PREFIX + "pending" + EPOCH_MARKER_SUFFIX)
            marker.write_text("{}", encoding="utf-8")
            try:
                completed, artifact = self.fleet.invoke(*arguments, "--recipient-preview", str(path))
            finally:
                marker.unlink()
            self.assertEqual(20, completed.returncode, artifact)
            self.assertEqual("epoch_roll_recovery_required", artifact["evidence"]["code"])
            self.assertEqual([], self.fleet.events())

    def test_group_send_preserves_due_epoch_maintenance(self):
        """Frozen-audience fanout must not disable the ordinary send maintenance boundary."""
        root = resolve_command_root(str(self.fleet.fleet))
        policy = root.tenant_home / "state/ledger-policy.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(json.dumps({"schema_version": 0, "max_bytes": 1, "max_age_days": 7}),
                          encoding="utf-8")
        arguments = self.arguments()
        path, _ = self.preview(arguments)
        for key in ("group-first", "group-second"):
            arguments[-1] = key
            completed, artifact = self.fleet.invoke(*arguments, "--recipient-preview", str(path))
            self.assertEqual(0, completed.returncode, artifact)
        self.assertEqual(1, len(list(root.tenant_home.glob("archive-*"))))
        messages = [row for row in self.fleet.events() if row["kind"] == "message_envelope"]
        self.assertEqual(["blue", "blue"], [row["recipient"] for row in messages])


if __name__ == "__main__":
    unittest.main()
