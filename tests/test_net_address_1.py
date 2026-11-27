"""A group send must use the recipients shown before the roster changes."""

import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from tests import test_net_ask_1
from tests.test_net_who_1 import _lane
from tests.test_net_who_1 import _claim_car
from floati.root import resolve_command_root
from floati.registry import Registry
from floati.records import validate_record
from floati.errors import ProtocolRefusal
from pathlib import Path
from tests.readiness import READY_SECONDS
from tests.schema_validation import validate_json_schema
from floati import cli, role_addressing
from floati.addressing import _digest
from floati.puddle_quota_snapshot import QuotaUnavailable, read_puddle_quota_snapshot


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

    def assign_reviewers(self, green_harness="Claude"):
        fixture = self.fleet
        completed, artifact = fixture.invoke_in_process(
            "node", "add", "--root", str(fixture.fleet), "--node", "green",
            "--harness", green_harness, "--lifetime", "permanent",
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

    def quota_v2_fixture(self):
        self.fleet.fleet = self.fleet.base / "fleet-sample"
        self.fleet.initialize_fleet()
        self.assign_reviewers("Codex")
        root = resolve_command_root(str(self.fleet.fleet))
        export = self.fleet.base / "quota"
        export.mkdir(mode=0o700)
        snapshot = export / "puddle-quota-v2.json"
        observed = "2026-11-26T12:00:00Z"
        accounts = ["acct-" + suffix * 32 for suffix in ("a", "b")]
        bindings = [
            {"tenant_id": root.tenant_id, "node_id": node, "harness": "codex",
             "account_ref": account, "binding_id": f"b-{node}-1",
             "valid_from": observed, "expires_at": "2026-11-26T12:02:00Z"}
            for node, account in zip(("blue", "green"), accounts)
        ]
        readings = [
            {"harness": "codex", "account_ref": account, "reading_id": "r-" + digit * 32,
             "observed_at": observed, "value": {
                 "status": "MEASURED", "window_kind": "account_primary",
                 "window_seconds": 18000, "remaining_bps": remaining,
                 "resets_at": reset, "expires_at": expiry,
             }}
            for account, digit, remaining, reset, expiry in (
                (accounts[0], "2", 2300, "2026-11-26T12:15:00Z", "2026-11-26T12:02:00Z"),
                (accounts[1], "3", 6700, "2026-11-26T12:01:00Z", "2026-11-26T12:01:00Z"),
            )
        ]
        document = {"schema_version": 2, "tenant_id": root.tenant_id, "readings": readings}
        args = self.arguments("role:reviewer", "ask")
        return snapshot, document, bindings, args

    def quota_v2_publish(self, snapshot, document):
        if snapshot.exists() or snapshot.is_symlink():
            snapshot.unlink()
        snapshot.write_text(json.dumps(document), encoding="utf-8")
        snapshot.chmod(0o400)

    def quota_v2_call(self, bindings, args, stamp="2026-11-26T12:00:30Z"):
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        with patch.object(role_addressing, "datetime") as clock, patch.object(
            role_addressing, "checked_account_bindings", return_value=bindings, create=True,
        ):
            clock.now.return_value = at
            return self.fleet.invoke_in_process(*args)

    def test_quota_v2_frozen_preview_vectors(self):
        snapshot, document, bindings, args = self.quota_v2_fixture()
        preview = [*args, "--preview", "--puddle-quota-snapshot", str(snapshot)]

        def check(rows, checked, expected, stamp="2026-11-26T12:00:30Z", age=30):
            self.quota_v2_publish(snapshot, dict(document, readings=rows))
            completed, artifact = self.quota_v2_call(checked, preview, stamp)
            evidence = artifact["evidence"]
            if expected in {"blue", "green"}:
                self.assertEqual(0, completed.returncode, artifact)
                self.assertEqual(3, evidence["preview_version"])
                self.assertEqual([expected], evidence["recipients"])
                self.assertEqual("puddle_quota_snapshot_v2", evidence["route"]["source"])
                self.assertEqual(age, evidence["route"]["age_seconds"])
                compared = {b["account_ref"] for b in checked}
                self.assertEqual(sorted((r for r in rows if r["account_ref"] in compared),
                                        key=lambda r: r["account_ref"]),
                                 evidence["route"]["readings"])
                self.assertEqual({b["node_id"]: b["binding_id"] for b in checked},
                                 {n: b["binding_id"] for n, b in evidence["route"]["candidates"].items()})
            else:
                self.assertEqual(35, completed.returncode, artifact)
                self.assertEqual("UNKNOWN", evidence["outcome"])
                self.assertEqual(expected, evidence["code"])
            return evidence

        blue, green = document["readings"]
        with self.subTest(vector="Q01"):
            check([green, blue], bindings, "green")
        with self.subTest(vector="Q02"):
            swapped = [dict(b, account_ref=bindings[1 - i]["account_ref"],
                            binding_id=b["binding_id"] + "-new") for i, b in enumerate(bindings)]
            check([blue, green], swapped, "blue")
        with self.subTest(vector="Q03"):
            for checked in (bindings[:1], [*bindings, bindings[1]],
                            [bindings[0], dict(bindings[1], account_ref="Green")],
                            [bindings[0], dict(bindings[1], tenant_id="other-fleet")],
                            [bindings[0], dict(bindings[1], expires_at="2026-11-26T12:02:01Z")]):
                check([blue, green], checked, "quota_account_unbound")
        with self.subTest(vector="Q04"):
            check([blue], bindings, "quota_reading_missing")
            unknown = dict(green, reading_id="r-" + "4" * 32,
                           value={"status": "UNKNOWN", "reason": "source_unavailable"})
            self.assertEqual("source_unavailable",
                             check([blue, unknown], bindings, "quota_reading_unknown")["reason"])
            check([blue, green, dict(unknown, account_ref="acct-" + "c" * 32)], bindings, "green")
        with self.subTest(vector="Q05"):
            check([blue, green], bindings, "green", "2026-11-26T12:00:00Z", 0)
            check([blue, green], bindings, "green", "2026-11-26T12:00:59.999999Z", 59)
            check([blue, green], bindings, "quota_reading_stale", "2026-11-26T12:01:00Z")
            check([blue, dict(green, observed_at="2026-11-26T12:00:31Z")],
                  bindings, "quota_reading_future")
        with self.subTest(vector="Q06"):
            zero = dict(blue, value=dict(blue["value"], remaining_bps=0))
            for remaining, expected in ((1, "green"), (0, "quota_headroom_tied")):
                check([zero, dict(green, value=dict(green["value"], remaining_bps=remaining))],
                      bindings, expected)
            check([blue, green], [dict(bindings[0], account_ref=green["account_ref"]), bindings[1]],
                  "quota_headroom_tied")
        with self.subTest(vector="Q07"):
            for kind, duration in (("account_weekly", 604800), ("account_primary", 14400)):
                check([blue, dict(green, value=dict(green["value"], window_kind=kind,
                                                  window_seconds=duration))],
                      bindings, "quota_window_incomparable")
        with self.subTest(vector="Q08"):
            for invalid in ([blue, green, blue], [blue, dict(green, reading_id=blue["reading_id"])],
                            [blue, dict(green, email="name@example.invalid")],
                            *([blue, dict(green, value=dict(green["value"], **change))]
                              for change in ({"remaining_bps": 10001}, {"remaining_bps": True},
                                             {"expires_at": "2026-11-26T12:01:01Z"}))):
                check(invalid, bindings, "quota_snapshot_invalid")
            self.quota_v2_publish(snapshot, dict(document, tenant_id="other-fleet"))
            completed, artifact = self.quota_v2_call(bindings, preview)
            self.assertEqual(35, completed.returncode, artifact)
            self.assertEqual("quota_tenant_mismatch", artifact["evidence"]["code"])
        self.assertEqual([], self.fleet.events())

    def test_quota_v2_confirm_rechecks_then_recovers_recorded_question(self):
        snapshot, document, bindings, args = self.quota_v2_fixture()
        self.quota_v2_publish(snapshot, document)
        selection = [*args, "--puddle-quota-snapshot", str(snapshot)]
        completed, artifact = self.quota_v2_call(bindings, [*selection, "--preview"])
        self.assertEqual(0, completed.returncode, artifact)
        path = snapshot.parent / "preview.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        preview = artifact["evidence"]
        confirm = [*selection, "--recipient-preview", str(path)]
        swapped = [dict(b, account_ref=bindings[1 - i]["account_ref"],
                        binding_id=b["binding_id"] + "-new") for i, b in enumerate(bindings)]
        other_reading = deepcopy(document)
        other_reading["readings"][0]["reading_id"] = "r-" + "4" * 32
        for checked, payload, time, rc, code in (
            (swapped, document, "12:00:31", 20, "address_preview_changed"),
            ([bindings[0], dict(bindings[1], binding_id="b-green-2")],
             document, "12:00:31", 20, "address_preview_changed"),
            (bindings, other_reading, "12:00:31", 20, "address_preview_changed"),
            (bindings, document, "12:00:29", 20, "address_preview_changed"),
            (bindings, document, "12:01:00", 35, "quota_reading_stale"),
        ):
            with self.subTest(code=code, time=time):
                self.quota_v2_publish(snapshot, payload)
                completed, artifact = self.quota_v2_call(checked, confirm, f"2026-11-26T{time}Z")
                self.assertEqual(rc, completed.returncode, artifact)
                self.assertEqual(code, artifact["evidence"]["code"])
                self.assertEqual([], self.fleet.events())
        refreshed = [dict(b, valid_from="2026-11-26T12:00:30Z",
                          expires_at="2026-11-26T12:02:30Z") for b in bindings]
        completed, artifact = self.quota_v2_call(refreshed, confirm, "2026-11-26T12:00:31Z")
        self.assertEqual(34, completed.returncode, artifact)
        question_id = artifact["evidence"]["results"][0]["question_id"]
        snapshot.unlink()
        completed, artifact = self.quota_v2_call([], confirm, "2026-11-26T12:02:00Z")
        self.assertEqual(34, completed.returncode, artifact)
        self.assertEqual(question_id, artifact["evidence"]["results"][0]["question_id"])
        questions = [row for row in self.fleet.events() if row["kind"] == "ask_question"]
        self.assertEqual([question_id], [row["id"] for row in questions])
        self.assertEqual(preview, questions[0]["address_preview"])
        root = resolve_command_root(str(self.fleet.fleet))
        altered = deepcopy(questions[0])
        altered["address_preview"]["route"]["age_seconds"] = 29
        payload = {k: v for k, v in altered["address_preview"].items() if k != "digest"}
        altered["address_preview"]["digest"] = _digest(payload)
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_record(altered, root.tenant_id, {"ask_question"}, integrity=False)
        self.assertEqual("address_preview_invalid", caught.exception.code)

    def test_quota_v2_input_refusals_do_not_fall_back(self):
        snapshot, document, bindings, args = self.quota_v2_fixture()
        preview = [*args, "--preview"]
        selection = [*preview, "--puddle-quota-snapshot", str(snapshot)]

        def refuse(argv, reason, stamp="2026-11-26T12:00:30Z"):
            completed, artifact = self.quota_v2_call(bindings, argv, stamp)
            self.assertEqual(35, completed.returncode, artifact)
            self.assertEqual("UNKNOWN", artifact["evidence"]["outcome"])
            self.assertEqual(reason, artifact["evidence"]["code"])

        refuse(preview, "quota_snapshot_not_selected")
        refuse(selection, "quota_snapshot_missing")
        v1 = snapshot.with_name("puddle-quota-v1.json")
        sample = {"status": "MEASURED", "reading_id": "legacy", "remaining_bps": 9000,
                  "measured_at": "2026-11-26T12:00:00Z", "expires_at": "2026-11-26T12:02:00Z"}
        self.quota_v2_publish(v1, {"schema_version": 1, "readings": {"codex": sample}})
        snapshot.symlink_to(v1)
        refuse(selection, "quota_snapshot_unreadable")
        self.quota_v2_publish(snapshot, dict(document, schema_version=3))
        refuse(selection, "quota_snapshot_version_unsupported")
        self.quota_v2_publish(snapshot, document)
        with self.assertRaisesRegex(QuotaUnavailable, "^quota_snapshot_invalid$"):
            read_puddle_quota_snapshot(snapshot)
        preview_path = snapshot.parent / "preview.json"
        completed, artifact = self.quota_v2_call(bindings, selection)
        self.assertEqual(0, completed.returncode, artifact)
        preview_path.write_text(json.dumps(artifact), encoding="utf-8")
        refuse([*args, "--recipient-preview", str(preview_path), "--puddle-quota-snapshot", str(v1)],
               "v2_evidence_required")

        alias = self.fleet.base / "alias"
        alias.symlink_to(snapshot.parent, target_is_directory=True)
        refuse([*preview, "--puddle-quota-snapshot", str(alias / snapshot.name)],
               "quota_snapshot_unreadable")
        snapshot.chmod(0o444)
        refuse(selection, "quota_snapshot_unreadable")
        snapshot.chmod(0o400)
        refuse(selection, "quota_reading_stale", "2026-11-26T12:01:00Z")
        snapshot.unlink()
        raw = json.dumps(document)
        for altered in (raw.replace("2300", "2.3e3"), raw.replace("2300", "2300.0"),
                        raw.replace('"schema_version": 2', '"schema_version": 2, "schema_version": 2'),
                        "\ufeff" + raw):
            snapshot.write_text(altered, encoding="utf-8")
            snapshot.chmod(0o400)
            refuse(selection, "quota_snapshot_invalid")
            snapshot.unlink()
        self.quota_v2_publish(snapshot, {"schema_version": 2, "readings": {}})
        refuse(selection, "quota_snapshot_invalid")
        self.quota_v2_publish(v1, document)
        refuse([*preview, "--puddle-quota-snapshot", str(v1)], "quota_snapshot_invalid")

        root = resolve_command_root(str(self.fleet.fleet))
        Registry(root).retire("blue")
        completed, artifact = self.quota_v2_call([], self.arguments("blue", "ask"))
        self.assertEqual(20, completed.returncode, artifact)
        self.quota_v2_publish(snapshot, document)
        completed, artifact = self.quota_v2_call([], selection)
        self.assertEqual(35, completed.returncode, artifact)
        self.assertEqual("quota_account_unbound", artifact["evidence"]["code"])
        completed, artifact = self.quota_v2_call([], self.arguments("green", "ask"))
        self.assertEqual(34, completed.returncode, artifact)
        Registry(root).retire("green")
        refuse(selection, "quota_role_empty")

    def test_quota_v2_t3_members_preserve_ordinary_role_routing(self):
        snapshot, document, bindings, args = self.quota_v2_fixture()
        blue, green = document["readings"]
        bound_green = dict(green, reading_id="r-" + "5" * 32, t3_provider_instance={
            "instance_ref": "2a8d85f356f5a2f54d16e576866dce9739af0cc9d11d39702682c1a4d319e1e3",
            "verified_at": "2026-11-26T12:00:00Z",
        })
        preview = [*args, "--preview", "--puddle-quota-snapshot", str(snapshot)]
        for row, rc in (
            (bound_green, 0),
            (green, 0),
            (dict(bound_green, t3_provider_instance=None), 35),
            (dict(bound_green, t3_provider_instance=dict(
                bound_green["t3_provider_instance"], providerInstanceId="codex-green")), 35),
        ):
            with self.subTest(status=rc, reading=row):
                self.quota_v2_publish(snapshot, dict(document, readings=[blue, row]))
                completed, artifact = self.quota_v2_call(bindings, preview)
                self.assertEqual(rc, completed.returncode, artifact)
                if rc == 0:
                    self.assertEqual(["green"], artifact["evidence"]["recipients"])
                    self.assertEqual(row, artifact["evidence"]["route"]["readings"][1])
                else:
                    self.assertEqual("quota_snapshot_invalid", artifact["evidence"]["code"])

    def test_quota_v2_t3_frozen_target_vectors(self):
        from floati.t3_quota_readout import T3Instance, T3Inventory, target_quota

        snapshot, document, _, _ = self.quota_v2_fixture()
        root = resolve_command_root(str(self.fleet.fleet))
        start = datetime(2026, 11, 26, 11, tzinfo=timezone.utc)
        blue, green = document["readings"]
        references = (
            "93d1a5add3586df103c734078d7b86270d6abcb177dd859587a902e5f2588f8a",
            "2a8d85f356f5a2f54d16e576866dce9739af0cc9d11d39702682c1a4d319e1e3",
        )
        bound_blue, bound_green = [
            dict(row, reading_id="r-" + digit * 32, t3_provider_instance={
                "instance_ref": instance_ref, "verified_at": "2026-11-26T12:00:00Z",
            }) for row, digit, instance_ref in zip((blue, green), ("4", "5"), references)
        ]
        blue_instance = T3Instance("codex-blue", "codex", start)
        green_instance = T3Instance("codex-green", "codex", start)
        inventory = T3Inventory("env-local", True, (blue_instance, green_instance))
        current = inventory

        def readout(rows, *, target="codex-green", account=green["account_ref"],
                    minute=0, second=30):
            self.quota_v2_publish(snapshot, dict(document, readings=rows))
            return target_quota(
                root, snapshot, "env-local", target, lambda environment: current,
                account_ref=account, at=datetime(2026, 11, 26, 12, minute, second, tzinfo=timezone.utc),
            )

        def unknown(result, reason):
            self.assertEqual("UNKNOWN", result["outcome"])
            self.assertEqual(reason, result["code"])
            self.assertNotIn("remaining_bps", result)

        with self.subTest(vector="T01"):
            for node, row, expected in (("codex-green", bound_green, 6700),
                                        ("codex-blue", bound_blue, 2300)):
                result = readout([bound_blue, bound_green], target=node, account=row["account_ref"])
                self.assertEqual("MEASURED", result["outcome"])
                self.assertEqual(expected, result["remaining_bps"])
                self.assertEqual(30, result["age_seconds"])
                self.assertEqual(row["reading_id"], result["reading_id"])
        with self.subTest(vector="T02"):
            unknown(readout([bound_blue, green]), "t3_instance_binding_missing")
        with self.subTest(vector="T03"):
            unknown(readout([bound_blue]), "t3_instance_binding_mismatch")
        with self.subTest(vector="T04"):
            unknown(readout([bound_blue, bound_green], minute=1, second=0), "quota_reading_stale")
            stale = dict(bound_green, reading_id="r-" + "6" * 32, t3_provider_instance=dict(
                bound_green["t3_provider_instance"], verified_at="2026-11-26T11:58:30Z"))
            unknown(readout([bound_blue, stale]), "t3_instance_binding_stale")
        with self.subTest(vector="T05"):
            current = T3Inventory("env-local", True, (blue_instance,))
            unknown(readout([bound_blue, bound_green]), "t3_instance_removed")
            current = T3Inventory("env-local", False, (blue_instance,))
            unknown(readout([bound_blue, bound_green]), "t3_instance_inventory_unavailable")
            recreated = T3Instance("codex-green", "codex", start.replace(hour=12, second=15))
            current = T3Inventory("env-local", True, (blue_instance, recreated))
            unknown(readout([bound_blue, bound_green]), "t3_instance_binding_changed")

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
        arguments[-1] = str(int(READY_SECONDS * 2))
        path, preview = self.preview(arguments)
        process = self.fleet.spawn(*arguments, "--recipient-preview", str(path))
        question = self.fleet.wait_for_question(process)
        completed, reply = self.fleet.invoke(*self.fleet.reply_argv(
            question["id"], disposition="answer", key="group-answer", note="Reviewed",
        ))
        self.assertEqual(0, completed.returncode, reply)
        artifact = self.fleet.finish(process)
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
