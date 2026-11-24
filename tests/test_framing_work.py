from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.host_paths import worker_workspace_root
from floati.planes import AuthorityGrantStore
from floati.records import validate_record
from floati.registry import Registry
from floati.root import FloatiRoot


NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)
SHA_A = "a" * 40
SHA_B = "b" * 40


class CanonicalFramingTests(unittest.TestCase):
    def test_sorted_compact_utf8_frame_is_shared_for_any_record(self) -> None:
        from floati.framing import encode_frame

        mail = {"z": 1, "kind": "message_envelope", "label": "buoy"}
        work = {"z": 1, "kind": "work_item", "label": "buoy"}

        self.assertEqual(
            b'{"kind":"message_envelope","label":"buoy","z":1}\n',
            encode_frame(mail),
        )
        self.assertEqual(
            b'{"kind":"work_item","label":"buoy","z":1}\n',
            encode_frame(work),
        )

    def test_decode_refuses_incomplete_blank_and_malformed_frames_distinctly(self) -> None:
        from floati.framing import FrameError, decode_frames

        cases = (
            (b'{"a":1}', "incomplete_frame"),
            (b"\n", "blank_frame"),
            (b"not-json\n", "malformed_json"),
        )
        for data, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(FrameError) as caught:
                    decode_frames(data)
                self.assertEqual(code, caught.exception.code)


class WorkLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.registry = Registry(self.root)
        self.registry.register(public_ids.worker('alpha'), "Codex")
        self.registry.register("bravo", "Codex")

    @staticmethod
    def binding(sha: str, doc: str) -> dict[str, str]:
        return {"repo": "floati", "sha": sha, "doc": doc}

    def test_notification_crash_boundaries_finish_once_and_reads_stay_inert(self) -> None:
        from floati.work import WorkLog
        from floati.events import EventLog
        from floati.work_notification import WorkNotifications, project_notification_findings

        grants = AuthorityGrantStore(self.root)
        grants.grant_exact("notify-work", public_ids.worker('alpha'), 1, NOW)
        work = WorkLog(self.root)
        for boundary in ("before-send", "after-send", "uncrashed"):
            with self.subTest(boundary=boundary):
                if boundary == "before-send":
                    target = patch.object(EventLog, "send", side_effect=RuntimeError("crash"))
                elif boundary == "after-send":
                    target = patch.object(WorkNotifications, "sent", side_effect=RuntimeError("crash"))
                else:
                    from contextlib import nullcontext
                    target = nullcontext()
                with target:
                    if boundary == "uncrashed":
                        work.add(boundary, "bravo", [self.binding(SHA_A, "AGENTS.md")], dispatcher="bravo", now=NOW)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "crash"):
                            work.add(boundary, "bravo", [self.binding(SHA_A, "AGENTS.md")], dispatcher="bravo", now=NOW)
                item = next(row for row in work.show() if row["title"] == boundary)
                journal = WorkNotifications(self.root)
                before = journal.records(item["id"])
                mail_before = EventLog(self.root).event_records()
                shown = work.show(item["id"])[0]
                self.assertEqual(before, shown["notifications"])
                findings = project_notification_findings(self.root, now=NOW)
                pending = [f for f in findings if f["code"] == "work_notification_pending" and f["subject"] == item["id"]]
                self.assertEqual(boundary != "uncrashed", bool(pending))
                self.assertEqual(mail_before, EventLog(self.root).event_records())
                self.assertEqual(before, journal.records(item["id"]))
                work.notify(item["id"], "bravo", now=NOW)
                work.notify(item["id"], "bravo", now=NOW)
                sent = [r for r in journal.records(item["id"]) if r["phase"] == "sent"]
                self.assertEqual(1, len(sent))
                envelopes = [r for r in EventLog(self.root).event_records() if r["kind"] == "message_envelope"
                             and r.get("idempotency_key") == "work-add-" + item["id"] + "-" + public_ids.worker('alpha')]
                self.assertEqual([sent[0]["envelope_id"]], [r["id"] for r in envelopes])

    def test_notification_orphan_is_counted_and_duplicate_precheck_leaves_no_intent(self) -> None:
        from floati.work import WorkLog
        from floati.work_notification import WorkNotifications, project_notification_findings

        work = WorkLog(self.root)
        with patch("floati.work.transact", side_effect=RuntimeError("before item")):
            with self.assertRaisesRegex(RuntimeError, "before item"):
                work.add("orphan", "bravo", [], dispatcher="bravo", now=NOW)
        self.assertEqual([], work.show())
        result = work.notify(None, "bravo", now=NOW)
        self.assertEqual([], result["notifications"])
        orphans = [f for f in project_notification_findings(self.root, now=NOW)
                   if f["code"] == "work_notification_intent_orphan"]
        self.assertEqual(1, len(orphans))
        self.assertEqual(("ok", 1), (orphans[0]["severity"], orphans[0]["count"]))
        work.add("orphan", "bravo", [], dispatcher="bravo", now=NOW)
        directory = self.home / "receipts/work-notifications"
        before = sorted(p.name for p in directory.iterdir())
        with self.assertRaises(ProtocolRefusal) as caught:
            work.add("orphan", "bravo", [], dispatcher="bravo", now=NOW)
        self.assertEqual("work_duplicate_open", caught.exception.code)
        self.assertEqual(before, sorted(p.name for p in directory.iterdir()))

    def test_non_sender_claim_reports_pending_without_authoring_dispatcher_mail(self) -> None:
        from floati.work import WorkLog
        from floati.events import EventLog

        actor = public_ids.worker('alpha')
        AuthorityGrantStore(self.root).grant_exact("notify-work", actor, 1, NOW)
        work = WorkLog(self.root)
        with patch.object(EventLog, "send", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                work.add("pending claim", "bravo", [self.binding(SHA_A, "AGENTS.md")], dispatcher="bravo", now=NOW)
        item = work.show()[0]
        findings = []
        claimed = work.claim(item["id"], actor, "notify-work", 1, now=NOW,
                             notification_findings=findings)
        self.assertEqual(claimed, validate_record(claimed, self.root.tenant_id,
                         frozenset({"work_transition"}), integrity=False))
        pending = next(f for f in findings if f["code"] == "work_notification_pending")
        self.assertEqual("bravo", pending["sender"])
        self.assertEqual("floati work notify --id " + item["id"] + " --as bravo", pending["remedy"])
        self.assertEqual([], EventLog(self.root).event_records())
        work.notify(item["id"], "bravo", now=NOW)
        # A claim removes add eligibility; replay derives recipients now, never
        # snapshots the old eligible list into the item or journal.
        self.assertEqual([], EventLog(self.root).event_records())

    def test_notification_send_then_roll_uses_archived_envelope_without_duplicate(self) -> None:
        from contextlib import nullcontext
        from floati.work import WorkLog
        from floati.work_notification import WorkNotifications, project_notification_findings
        from floati.events import EventLog
        from floati.bus_epoch import roll_bus_epoch, ROLL_AUTHORITY_SUBJECT

        grants = AuthorityGrantStore(self.root)
        grants.grant_exact("notify-work", public_ids.worker('alpha'), 1, NOW)
        grants.grant_exact(ROLL_AUTHORITY_SUBJECT, public_ids.worker('alpha'), 1, datetime.now(timezone.utc))
        work = WorkLog(self.root)
        for interrupted in (True, False):
            with self.subTest(interrupted=interrupted):
                title = "rolled-gap-" + str(interrupted)
                target = (patch.object(WorkNotifications, "sent", side_effect=RuntimeError("crash"))
                          if interrupted else nullcontext())
                with target:
                    if interrupted:
                        with self.assertRaisesRegex(RuntimeError, "crash"):
                            work.add(title, "bravo", [self.binding(SHA_A, "AGENTS.md")], dispatcher="bravo", now=NOW)
                    else:
                        work.add(title, "bravo", [self.binding(SHA_A, "AGENTS.md")], dispatcher="bravo", now=NOW)
                item = next(i for i in work.show() if i["title"] == title)
                key = "work-add-" + item["id"] + "-" + public_ids.worker('alpha')
                original = next(r for r in EventLog(self.root).event_records()
                                if r["kind"] == "message_envelope" and r.get("idempotency_key") == key)
                roll_bus_epoch(self.root, actor=public_ids.worker('alpha'), idempotency_key=title)
                self.assertFalse(any(r["kind"] == "message_envelope" and r.get("idempotency_key") == key
                                     for r in EventLog(self.root).event_records()))
                work.notify(item["id"], "bravo", now=NOW)
                work.notify(item["id"], "bravo", now=NOW)
                sent = [r for r in WorkNotifications(self.root).records(item["id"]) if r["phase"] == "sent"]
                self.assertEqual([original["id"]], [r["envelope_id"] for r in sent])
                self.assertFalse(any(r["kind"] == "message_envelope" and r.get("idempotency_key") == key
                                     for r in EventLog(self.root).event_records()))
                self.assertFalse(any(f["code"] in {"work_notification_pending", "work_notification_conflict"}
                                     for f in project_notification_findings(self.root, now=NOW)))

    def test_notification_send_refusal_is_degraded_and_preserves_remedy(self) -> None:
        from floati.work import WorkLog
        from floati.events import EventLog

        AuthorityGrantStore(self.root).grant_exact("notify-work", public_ids.worker('alpha'), 1, NOW)
        findings = []
        with patch.object(EventLog, "send", side_effect=ProtocolRefusal("recipient_unregistered", "gone", remedy="register the recipient")):
            item = WorkLog(self.root).add("refused notice", "bravo", [self.binding(SHA_A, "AGENTS.md")], dispatcher="bravo", now=NOW,
                                          notification_findings=findings)
        self.assertEqual(item, validate_record(item, self.root.tenant_id,
                         frozenset({"work_item"}), integrity=False))
        finding = next(f for f in findings if f["code"] == "work_notification_refused")
        self.assertEqual("recipient_unregistered", finding["refusal_code"])
        self.assertEqual("register the recipient", finding["remedy"])

    def test_completion_crash_retries_same_transition_and_return_mail(self) -> None:
        from contextlib import nullcontext
        from floati.work import WorkLog
        from floati.events import EventLog
        from floati.work_notification import WorkNotifications
        from floati.jsonl import read_records_snapshot

        actor = public_ids.worker('alpha')
        AuthorityGrantStore(self.root).grant_exact("completion-work", actor, 1, NOW)
        work = WorkLog(self.root)
        for boundary in ("before-send", "after-send", "uncrashed"):
            with self.subTest(boundary=boundary):
                item = work.add("finish-" + boundary, actor, [], dispatcher="bravo", now=NOW)
                work.claim(item["id"], actor, "completion-work", 1, now=NOW)
                binding = [self.binding(SHA_A, "AGENTS.md")]
                target = (patch.object(EventLog, "send", side_effect=RuntimeError("crash"))
                          if boundary == "before-send" else
                          patch.object(WorkNotifications, "sent", side_effect=RuntimeError("crash"))
                          if boundary == "after-send" else nullcontext())
                with target:
                    if boundary == "uncrashed":
                        work.complete(item["id"], actor, binding, now=NOW)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "crash"):
                            work.complete(item["id"], actor, binding, now=NOW)
                retried = work.complete(item["id"], actor, binding, now=NOW)
                self.assertEqual(retried, validate_record(retried, self.root.tenant_id,
                                 frozenset({"work_transition"}), integrity=False))
                work.notify(item["id"], actor, now=NOW)
                transitions = [r for r in read_records_snapshot(self.root, work.relative_path,
                               allowed_kinds={"work_item", "work_transition"})
                               if r["kind"] == "work_transition" and r["action"] == "complete"
                               and r["work_item_id"] == item["id"]]
                self.assertEqual([retried["id"]], [r["id"] for r in transitions])
                sent = [r for r in work.show(item["id"])[0]["notifications"] if r["phase"] == "sent"]
                self.assertEqual([retried["id"]], [r["transition_id"] for r in sent])
                envelopes = [r for r in EventLog(self.root).event_records() if r["kind"] == "message_envelope"
                             and r.get("idempotency_key") == "work-complete-" + item["id"] + "-bravo"]
                self.assertEqual([sent[0]["envelope_id"]], [r["id"] for r in envelopes])

    def test_native_oldest_claim_retains_the_pending_sender_finding(self) -> None:
        from floati.work import WorkLog
        from floati.events import EventLog

        actor = public_ids.worker('alpha')
        AuthorityGrantStore(self.root).grant_exact("oldest-work", actor, 1, NOW)
        work = WorkLog(self.root)
        with patch.object(EventLog, "send", side_effect=RuntimeError("crash")):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                work.add("oldest pending", actor, [self.binding(SHA_A, "AGENTS.md")], dispatcher="bravo", now=NOW)
        claimed = work.claim_owned_oldest(actor, "oldest-work", 1, now=NOW)
        self.assertEqual("claimed", claimed["state"])
        self.assertEqual(["bravo"], [f["sender"] for f in claimed["findings"] if f["code"] == "work_notification_pending"])

    def test_identical_open_work_is_refused_without_another_item(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        first = work.add("prepare input", public_ids.worker('alpha'), [])
        with self.assertRaises(ProtocolRefusal) as caught:
            work.add("prepare input", public_ids.worker('alpha'), [])
        self.assertEqual("work_duplicate_open", caught.exception.code)
        self.assertEqual([first["id"]], [item["id"] for item in work.show()])

    def test_work_ledger_is_distinct_append_only_and_completes_sparse_items(self) -> None:
        from floati.jsonl import read_records
        from floati.work import WorkLog

        work = WorkLog(self.root)
        item_a = work.add("frame mail", public_ids.worker('alpha'), [self.binding(SHA_A, "docs/a.md")])
        item_b = work.add("build board", public_ids.worker('alpha'), [])
        item_c = work.add("study acp", "bravo", [])

        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )
        claimed = work.claim(
            item_b["id"],
            public_ids.worker('alpha'),
            "work-claims",
            grant["epoch"],
            now=NOW,
        )
        completed = work.complete(
            item_b["id"],
            public_ids.worker('alpha'),
            [self.binding(SHA_B, "docs/evidence/b.md")],
            now=NOW,
        )

        self.assertEqual("work_transition", claimed["kind"])
        self.assertEqual("claim", claimed["action"])
        self.assertEqual("complete", completed["action"])
        states = {item["id"]: item for item in work.show()}
        self.assertEqual("open", states[item_a["id"]]["state"])
        self.assertEqual("completed", states[item_b["id"]]["state"])
        self.assertEqual("open", states[item_c["id"]]["state"])
        self.assertEqual(
            [self.binding(SHA_B, "docs/evidence/b.md")],
            states[item_b["id"]]["artifact_bindings"],
        )
        durable = read_records(
            self.root,
            "work/items.jsonl",
            allowed_kinds={"work_item", "work_transition"},
        )
        self.assertEqual(5, len(durable))
        self.assertEqual(
            ["work_item", "work_item", "work_item", "work_transition", "work_transition"],
            [record["kind"] for record in durable],
        )
        self.assertFalse((self.home / "events.jsonl").exists())

    def test_claim_requires_exact_active_authority_plane_binding(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        item = work.add("bind authority", public_ids.worker('alpha'), [])
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )
        path = self.home / "work" / "items.jsonl"
        before = path.read_bytes()

        cases = (
            ("bravo", "work-claims", grant["epoch"], "authority_holder_mismatch"),
            (public_ids.worker('alpha'), "work-claims", grant["epoch"] + 1, "authority_epoch_mismatch"),
            (public_ids.worker('alpha'), "other-subject", grant["epoch"], "authority_missing"),
        )
        for actor, subject, epoch, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    work.claim(item["id"], actor, subject, epoch, now=NOW)
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(before, path.read_bytes())

    def test_claimed_item_reports_collision_before_other_authority(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        item = work.add("collision", public_ids.worker('alpha'), [])
        grants = AuthorityGrantStore(self.root)
        first = grants.grant_exact("work-claims", public_ids.worker('alpha'), 1, NOW)
        work.claim(item["id"], public_ids.worker('alpha'), "work-claims", first["epoch"], now=NOW)
        second = grants.grant_exact("work-claims", "bravo", 2, NOW)
        with self.assertRaises(ProtocolRefusal) as caught:
            work.claim(item["id"], "bravo", "work-claims", second["epoch"], now=NOW)
        self.assertEqual("work_already_claimed", caught.exception.code)

    def test_eligibility_reports_superseding_sibling_grant(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        item = work.add("available work", public_ids.worker('alpha'), [])
        grants = AuthorityGrantStore(self.root)
        grants.grant_exact("work-claims", public_ids.worker('alpha'), 1, NOW)
        grants.grant_exact("work-claims", "bravo", 2, NOW)
        shown = work.show(item["id"], actor=public_ids.worker('alpha'), now=NOW)[0]
        self.assertFalse(shown["eligibility"]["eligible"])
        self.assertEqual("authority_superseded", shown["eligibility"]["code"])
        self.assertEqual([{"code": "authority_superseded", "subject": "work-claims",
                           "holder": "bravo", "epoch": 2}], shown["eligibility"]["findings"])
        expired = work.show(item["id"], actor=public_ids.worker('alpha'), now=NOW + timedelta(days=370))[0]
        self.assertEqual("authority_missing", expired["eligibility"]["code"])
        self.assertEqual([], expired["eligibility"]["findings"])
        grants.grant_exact("other-work", public_ids.worker('alpha'), 1, NOW)
        usable = work.show(item["id"], actor=public_ids.worker('alpha'), now=NOW)[0]["eligibility"]
        self.assertTrue(usable["eligible"])
        self.assertEqual([{"subject": "other-work", "epoch": 1}], usable["authorities"])
        self.assertEqual("authority_superseded", usable["findings"][0]["code"])

    def test_invalid_work_transition_refuses_without_mutation(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        item = work.add("ordered transitions", public_ids.worker('alpha'), [])
        path = self.home / "work" / "items.jsonl"
        before = path.read_bytes()

        with self.assertRaises(ProtocolRefusal) as caught:
            work.complete(item["id"], public_ids.worker('alpha'), [], now=NOW)
        self.assertEqual("work_not_claimed", caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_owned_oldest_selection_and_claim_share_one_transaction(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        oldest_alice = work.add(public_ids.compose('oldest ', public_ids.worker('alpha')), public_ids.worker('alpha'), [])
        work.add("bravo work", "bravo", [])
        work.add(public_ids.compose('newer ', public_ids.worker('alpha')), public_ids.worker('alpha'), [])
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )

        claimed = work.claim_owned_oldest(
            public_ids.worker('alpha'), "work-claims", grant["epoch"], now=NOW
        )

        self.assertEqual(oldest_alice["id"], claimed["id"])
        self.assertEqual("claimed", claimed["state"])
        states = {item["id"]: item for item in work.show()}
        self.assertEqual("claimed", states[oldest_alice["id"]]["state"])
        self.assertEqual(
            ["claimed", "open"],
            sorted(
                item["state"]
                for item in states.values()
                if item["owner"] == public_ids.worker('alpha')
            ),
        )

    def test_workspace_mapping_is_derived_from_the_new_work_id_and_legacy_items_remain_readable(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        legacy = work.add("legacy sparse work", public_ids.worker('alpha'), [])
        live = work.add(
            "create one local artifact",
            public_ids.worker('alpha'),
            [],
            provision_workspace=True,
        )

        self.assertNotIn("workspace", legacy)
        self.assertEqual(
            str(worker_workspace_root() / live["id"]),
            live["workspace"],
        )
        projected = {item["id"]: item for item in work.show()}
        self.assertIsNone(projected[legacy["id"]]["workspace"])
        self.assertEqual(live["workspace"], projected[live["id"]]["workspace"])

    def test_dependency_edges_are_bounded_existing_and_unique(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        prerequisite = work.add("prepare input", public_ids.worker('alpha'), [])

        for needs, code in (
            (["work-018f0f23abcd71238000000000000000"], "work_dependency_unknown"),
            ([prerequisite["id"], prerequisite["id"]], "needs_invalid"),
            (["not-a-work-id"], "needs_invalid"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    work.add("dependent", public_ids.worker('alpha'), [], needs=needs)
                self.assertEqual(code, caught.exception.code)

    def test_blocked_claims_refuse_until_dependencies_complete(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        prerequisite = work.add("prepare input", public_ids.worker('alpha'), [])
        dependent = work.add(
            "consume input", public_ids.worker('alpha'), [], needs=[prerequisite["id"]]
        )
        unrelated = work.add("independent work", public_ids.worker('alpha'), [])
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )

        projected = {item["id"]: item for item in work.show()}
        self.assertEqual("ready", projected[prerequisite["id"]]["readiness"])
        self.assertEqual("blocked", projected[dependent["id"]]["readiness"])
        self.assertEqual([prerequisite["id"]], projected[dependent["id"]]["needs"])

        with self.assertRaises(ProtocolRefusal) as caught:
            work.claim(
                dependent["id"], public_ids.worker('alpha'), "work-claims", grant["epoch"], now=NOW
            )
        self.assertEqual("work_dependencies_blocked", caught.exception.code)

        selected = work.claim_owned_oldest(
            public_ids.worker('alpha'), "work-claims", grant["epoch"], now=NOW
        )
        self.assertEqual(prerequisite["id"], selected["id"])
        work.complete(prerequisite["id"], public_ids.worker('alpha'), [], now=NOW)

        projected = {item["id"]: item for item in work.show()}
        self.assertEqual("done", projected[prerequisite["id"]]["readiness"])
        self.assertEqual("ready", projected[dependent["id"]]["readiness"])
        self.assertEqual("ready", projected[unrelated["id"]]["readiness"])

    def test_projection_rejects_future_dependency_edges_in_raw_evidence(self) -> None:
        from floati.work import WorkLog

        work = WorkLog(self.root)
        first = work.add("first", public_ids.worker('alpha'), [])
        second = work.add("second", public_ids.worker('alpha'), [])
        path = self.home / "work" / "items.jsonl"
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        records[0]["needs"] = [second["id"]]
        path.write_text(
            "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in records),
            encoding="utf-8",
        )

        with self.assertRaises(IntegrityFailure) as caught:
            work.show(first["id"])

        self.assertEqual("consumption_state_unavailable", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
