"""A group send must use the recipients shown before the roster changes."""

import json
import unittest
from datetime import datetime, timedelta, timezone

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

    def assign_reviewers(self):
        fixture = self.fleet
        completed, artifact = fixture.invoke_in_process(
            "node", "add", "--root", str(fixture.fleet), "--node", "green",
            "--harness", "Claude", "--lifetime", "permanent",
        )
        self.assertEqual(0, completed.returncode, artifact)
        for node in ("blue", "green"):
            completed, artifact = fixture.invoke_in_process(
                "node", "role", "--root", str(fixture.fleet), "--node", node,
                "--template", "reviewer", "--answer", "repo=floati", "--answer",
                "never_touch=other", "--answer", "reports_to=red",
            )
            self.assertEqual(0, completed.returncode, artifact)

    def test_role_ask_records_the_unique_fresh_quota_winner(self):
        self.assign_reviewers()
        now = datetime.now(timezone.utc)
        measured_at = (now - timedelta(seconds=10)).isoformat()
        expires_at = (now + timedelta(minutes=5)).isoformat()
        snapshot = self.fleet.base / "puddle-quota-v1.json"
        readings = {
            harness: {"status": "MEASURED", "remaining_bps": remaining,
                      "measured_at": measured_at, "expires_at": expires_at,
                      "reading_id": reading_id}
            for harness, remaining, reading_id in (
                ("codex", 2300, "sample-a"), ("claude", 6700, "sample-b"),
            )
        }

        def publish():
            snapshot.write_text(json.dumps({"schema_version": 1, "readings": readings}),
                                encoding="utf-8")

        publish()
        arguments = self.arguments("role:reviewer", "ask") + [
            "--puddle-quota-snapshot", str(snapshot),
        ]
        completed, artifact = self.fleet.invoke_in_process(*arguments, "--preview")
        self.assertEqual(0, completed.returncode, artifact)
        preview = artifact["evidence"]
        self.assertEqual(["green"], preview["recipients"])
        route = preview["route"]
        self.assertEqual(readings["claude"], route["readings"]["claude"])
        age = datetime.fromisoformat(route["evaluated_at"]) - datetime.fromisoformat(measured_at)
        self.assertEqual(int(age.total_seconds()), route["age_seconds"])
        self.assertEqual([], self.fleet.events())

        path = self.fleet.base / "preview.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        readings["codex"]["remaining_bps"] = 9000
        publish()
        send = [*arguments, "--recipient-preview", str(path)]
        completed, artifact = self.fleet.invoke_in_process(*send)
        self.assertEqual(20, completed.returncode, artifact)
        self.assertEqual("address_preview_changed", artifact["evidence"]["code"])
        self.assertEqual([], self.fleet.events())

        snapshot.unlink()
        completed, artifact = self.fleet.invoke_in_process(*send)
        self.assertEqual(35, completed.returncode, artifact)
        self.assertEqual("UNKNOWN", artifact["evidence"]["outcome"])
        self.assertEqual("quota_snapshot_missing", artifact["evidence"]["code"])
        self.assertEqual([], self.fleet.events())

        readings["codex"]["remaining_bps"] = 2300
        publish()
        completed, artifact = self.fleet.invoke_in_process(*send)
        self.assertEqual(34, completed.returncode, artifact)
        question_id = artifact["evidence"]["results"][0]["question_id"]
        snapshot.unlink()
        completed, artifact = self.fleet.invoke_in_process(*send)
        self.assertEqual(34, completed.returncode, artifact)
        self.assertEqual(question_id, artifact["evidence"]["results"][0]["question_id"])
        questions = [row for row in self.fleet.events() if row["kind"] == "ask_question"]
        self.assertEqual(["green"], [row["recipient"] for row in questions])
        self.assertEqual(preview, questions[0]["address_preview"])

    def test_role_ask_without_fresh_comparable_quota_leaves_the_choice_to_sender(self):
        self.assign_reviewers()
        arguments = self.arguments("role:reviewer", "ask") + ["--preview"]
        snapshot = self.fleet.base / "puddle-quota-v1.json"
        now = datetime.now(timezone.utc)
        sample = {"status": "MEASURED", "remaining_bps": 6000, "reading_id": "sample-a",
                  "measured_at": (now - timedelta(minutes=2)).isoformat(),
                  "expires_at": (now + timedelta(minutes=5)).isoformat()}
        for name, payload, reason in (
            ("no opt-in", None, "quota_snapshot_not_selected"),
            ("missing", None, "quota_snapshot_missing"),
            ("wrong version", {"schema_version": 2, "readings": {}}, "quota_snapshot_invalid"),
            ("missing harness", {"schema_version": 1, "readings": {"codex": sample}},
             "quota_reading_missing"),
            ("stale", {"schema_version": 1, "readings": {"codex": sample, "claude": dict(
                sample, expires_at=(now - timedelta(minutes=1)).isoformat())}}, "quota_reading_stale"),
            ("tied", {"schema_version": 1, "readings": {"codex": sample, "claude": sample}},
             "quota_headroom_tied"),
        ):
            with self.subTest(name=name):
                if payload is not None:
                    snapshot.write_text(json.dumps(payload), encoding="utf-8")
                opt_in = [] if name == "no opt-in" else ["--puddle-quota-snapshot", str(snapshot)]
                completed, artifact = self.fleet.invoke_in_process(*arguments, *opt_in)
                self.assertEqual(35, completed.returncode, artifact)
                evidence = artifact["evidence"]
                self.assertEqual("UNKNOWN", evidence["outcome"])
                self.assertEqual(reason, evidence["code"])
                self.assertEqual(["blue", "green"], evidence["candidates"])
        self.assertEqual([], self.fleet.events())
        completed, artifact = self.fleet.invoke_in_process(*self.arguments("blue", "ask"))
        self.assertEqual(34, completed.returncode, artifact)
        questions = [row for row in self.fleet.events() if row["kind"] == "ask_question"]
        self.assertEqual(["blue"], [row["recipient"] for row in questions])

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
