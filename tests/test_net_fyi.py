"""Constructed non-waking mail properties on isolated fixture fleets."""
from __future__ import annotations

import io
import json
import contextlib
import itertools
from pathlib import Path
import unittest
from unittest import mock

from floati import codex_wait, cursor_wait
from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.records import validate_record
from floati.tui import model_from_root
from floati.tui_render import render_plain_dump
from tests import test_codex_wait as baseline
from tests import test_wake_daemon as daemon_baseline
from tests.schema_validation import validate_json_schema
from tests.waiter_timing import scripted_readiness


class FyiStopTests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.node = self.f.participant.binding.node_id

    def send(self, key, message_class=None):
        options = {} if message_class is None else {"message_class": message_class}
        try:
            return EventLog(self.f.root).send("architect", self.node, "floati", "a" * 40,
                "docs/evidence/ping.md", "mail property", idempotency_key=key, **options)
        except TypeError as exc:
            self.fail("sender cannot mark non-waking mail: " + str(exc))

    def run_stop(self, harness, session, inject=None):
        out, err = io.StringIO(), io.StringIO()
        elapsed = [0.0]
        def tick(seconds):
            elapsed[0] += seconds
            if inject is not None:
                inject(elapsed[0])
        options = dict(stdout=out, stderr=err, sleep=tick,
            wait_readable=scripted_readiness(tick))
        if harness == "codex":
            code = codex_wait.run_stop_waiter(bus_home=self.f.bus_home,
                hook_payload={"cwd": str(self.f.workspace), "session_id": session},
                monotonic=lambda: elapsed[0], **options)
        else:
            self.f.arm_session(session)
            with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.f.workspace):
                code = cursor_wait.run_cursor_stop_wait(root=self.f.bus_home, node=self.node,
                    runtime=Path(__file__).resolve().parents[1],
                    deadline_seconds=2, poll_seconds=1, hook_timeout_seconds=10,
                    payload={"status": "completed", "conversation_id": session, "loop_count": 0},
                    clock=lambda: elapsed[0], **options)
        self.assertEqual(0, code, err.getvalue())
        return out.getvalue(), elapsed[0]

    def assert_idle_stop(self, harness):
        sent = []
        def inject(_elapsed):
            if not sent:
                sent.append(self.send("idle-" + harness, "fyi"))
        first, elapsed = self.run_stop(harness, "idle-" + harness, inject)
        self.assertTrue(sent)
        self.assertGreaterEqual(elapsed, 2)
        self.assertNotIn(sent[0]["id"], first, "FYI woke an already-idle seat")
        second, _ = self.run_stop(harness, "idle-" + harness)
        self.assertIn(sent[0]["id"], second, "next Stop did not present pending FYI")

    def test_fyi_arriving_during_idle_codex_waits_for_next_stop(self):
        self.assert_idle_stop("codex")

    def test_fyi_arriving_during_idle_cursor_waits_for_next_stop(self):
        self.assert_idle_stop("cursor")

    def assert_mixed_stop(self, harness):
        sent = []
        def inject(elapsed):
            if not sent:
                sent.append(self.send("mixed-fyi-" + harness, "fyi"))
                sent.append(self.send("mixed-normal-" + harness))
        body, elapsed = self.run_stop(harness, "mixed-" + harness, inject)
        self.assertEqual(2, len(sent))
        self.assertIn(sent[1]["id"], body)
        self.assertNotIn(sent[0]["id"], body)
        self.assertLessEqual(elapsed, 2, "ordinary mail lost its existing wake bound")
        next_body, _ = self.run_stop(harness, "mixed-" + harness)
        self.assertIn(sent[0]["id"], next_body)

    def test_normal_after_fyi_wakes_codex_without_presenting_fyi_early(self):
        self.assert_mixed_stop("codex")

    def test_normal_after_fyi_wakes_cursor_without_presenting_fyi_early(self):
        self.assert_mixed_stop("cursor")

    def test_legacy_envelope_remains_valid_and_wakes_unchanged(self):
        message = self.send("legacy")
        self.assertNotIn("message_class", message["message"])
        envelope = dict(message["message"])
        validate_record(envelope, self.f.root.tenant_id, {"message_envelope"}, integrity=True)
        validate_json_schema(envelope, Path(__file__).resolve().parents[1] /
            "schemas/v0/message-envelope.schema.json")
        body, elapsed = self.run_stop("codex", "legacy")
        self.assertEqual(0, elapsed)
        self.assertIn(message["id"], body)

    def test_inbox_preserves_class_and_board_pending_at_pause_clears_after_ack(self):
        message = self.send("board-fyi", "fyi")
        model = model_from_root(self.f.root)
        node = next(row for row in model.nodes if row["node_id"] == self.node)
        self.assertEqual(1, node.get("pending_at_pause"))
        self.assertIn("PENDING-AT-PAUSE 1", render_plain_dump(model, width=200))
        # Cached redraw must preserve the classification too.
        self.assertIn("PENDING-AT-PAUSE 1", render_plain_dump(model_from_root(self.f.root), width=200))
        messages, receipt = EventLog(self.f.root).present(self.node)
        self.assertEqual("fyi", messages[0]["message_class"])
        self.assertEqual([message["id"]], receipt["item_ids"])
        SparseCursor(self.f.root).ack(self.node, [message["id"]], acting_session_id="manual-inbox")
        self.assertNotIn("PENDING-AT-PAUSE", render_plain_dump(model_from_root(self.f.root), width=200))

    def test_class_is_additive_validated_and_bound_to_idempotency(self):
        message = self.send("class-key", "fyi")
        self.assertEqual(message, self.send("class-key", "fyi"))
        envelope = dict(message["message"])
        validate_json_schema(envelope, Path(__file__).resolve().parents[1] /
            "schemas/v0/message-envelope.schema.json")
        for message_class in (None, "normal"):
            with self.subTest(message_class=message_class), self.assertRaises(ProtocolRefusal) as caught:
                self.send("class-key", message_class)
            self.assertEqual("idempotency_conflict", caught.exception.code)
        self.send("normal-key")
        self.assertEqual(self.send("normal-key"), self.send("normal-key", "normal"))
        with self.assertRaises(ProtocolRefusal) as caught:
            self.send("invalid-class", "urgent")
        self.assertEqual("message_class_invalid", caught.exception.code)

    def test_every_legacy_optional_field_shape_accepts_the_additive_class(self):
        envelope = dict(self.send("shape-seed")["message"])
        envelope.pop("attempt_binding", None)
        optional = {"reply_to": envelope["id"], "worker_session_id": "worker-session",
            "attempt_binding": "absent_legacy"}
        from floati.addressing import recipient_preview
        optional["address_preview"] = recipient_preview(self.f.root, "architect", "everyone")
        schema = Path(__file__).resolve().parents[1] / "schemas/v0/message-envelope.schema.json"
        for included in itertools.product((False, True), repeat=len(optional)):
            original = dict(envelope, **{key: value for (key, value), keep in zip(optional.items(), included) if keep})
            for message_class in (None, "normal", "fyi"):
                record = dict(original)
                if message_class is not None:
                    record["message_class"] = message_class
                with self.subTest(included=included, message_class=message_class):
                    self.assertEqual(record, validate_record(record, self.f.root.tenant_id,
                        {"message_envelope"}, integrity=True))
                    validate_json_schema(record, schema)
        for invalid in (None, "urgent", 1, [], {}):
            with self.subTest(invalid=invalid):
                record = dict(envelope, message_class=invalid)
                with self.assertRaises(Exception) as caught:
                    validate_record(record, self.f.root.tenant_id, {"message_envelope"}, integrity=False)
                self.assertEqual("message_class_invalid", caught.exception.code)
                with self.assertRaises(AssertionError):
                    validate_json_schema(record, schema)

    def test_cli_send_and_inbox_carry_the_class(self):
        from floati.cli import main
        output = io.StringIO()
        args = ["send", "--root", str(self.f.root.path), "--from", "architect", "--to", self.node,
            "--repo", "floati", "--sha", "a" * 40, "--doc", "docs/evidence/ping.md",
            "--note", "CLI class property", "--idempotency-key", "cli-fyi", "--message-class", "fyi"]
        with mock.patch("floati.cli._require_banked_sha"), contextlib.redirect_stdout(output):
            code = main(args)
        self.assertEqual(0, code, output.getvalue())
        self.assertEqual("fyi", json.loads(output.getvalue())["evidence"]["message"]["message_class"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["inbox", "--root", str(self.f.root.path), "--as", self.node, "--session", "cli-stop"])
        self.assertEqual(0, code, output.getvalue())
        self.assertEqual("fyi", json.loads(output.getvalue())["evidence"]["messages"][0]["message_class"])

    def test_group_send_keeps_fyi_class_and_exact_saved_preview(self):
        from floati import cli
        from floati.addressing import recipient_preview
        preview = recipient_preview(self.f.root, "architect", "everyone")
        saved = self.f.workspace / "fyi-group-preview.json"
        saved.write_text(json.dumps({"artifact_version": 0, "command": "send",
            "status": "ok", "evidence": preview}))
        args = cli._parser().parse_args(["send", "--root", str(self.f.root.path),
            "--from", "architect", "--to", "everyone", "--repo", "floati",
            "--sha", "a" * 40, "--doc", "docs/evidence/ping.md", "--note", "group FYI",
            "--idempotency-key", "group-fyi", "--message-class", "fyi",
            "--recipient-preview", str(saved)])
        with mock.patch("floati.cli._require_banked_sha"):
            _status, evidence, code = cli._send(args)
        self.assertEqual(0, code)
        self.assertEqual(preview["recipients"], [row["recipient"] for row in evidence["results"]])
        for row in evidence["results"]:
            message = row["receipt"]["message"]
            self.assertEqual("fyi", message.get("message_class"))
            self.assertEqual(preview, message["address_preview"])
        with mock.patch("floati.cli._require_banked_sha"):
            args.message_class = "normal"
            with self.assertRaises(ProtocolRefusal) as caught:
                cli._send(args)
        self.assertEqual("idempotency_conflict", caught.exception.code)

    def test_existing_python_send_caller_may_omit_new_class_option(self):
        from floati import cli
        args = cli._parser().parse_args(["send", "--root", str(self.f.root.path),
            "--from", "architect", "--to", self.node, "--repo", "floati", "--sha", "a" * 40,
            "--doc", "docs/evidence/ping.md", "--note", "existing caller",
            "--idempotency-key", "existing-caller"])
        del args.message_class
        with mock.patch("floati.cli._require_banked_sha"):
            _status, evidence, code = cli._send(args)
        self.assertEqual(0, code)
        self.assertNotIn("message_class", evidence["message"])

    def test_board_counts_fyi_appended_after_an_empty_cached_redraw(self):
        model_from_root(self.f.root)
        self.send("cached-tail", "fyi")
        model = model_from_root(self.f.root)
        self.assertIn("PENDING-AT-PAUSE 1", render_plain_dump(model, width=200))


class FyiEpochTests(unittest.TestCase):
    def test_epoch_rotation_inside_idle_stop_does_not_make_fyi_wake_eligible(self):
        from tests.test_epoch_pending_waiter import EpochPendingWaiterTests
        f = EpochPendingWaiterTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        elapsed, sent = [0.0], []
        def tick(seconds):
            elapsed[0] += seconds
            if not sent:
                sent.append(EventLog(f.root).send("actor-a", f.node, "floati", "a" * 40,
                    "docs/evidence/ping.md", "pending across roll", message_class="fyi",
                    idempotency_key="epoch-fyi"))
                policy = f.root.path / "state/ledger-policy.json"
                policy.parent.mkdir(parents=True, exist_ok=True)
                policy.write_text(json.dumps({"schema_version": 0, "max_bytes": 1, "max_age_days": 7}))
        out, err = io.StringIO(), io.StringIO()
        code = codex_wait.run_stop_waiter(bus_home=f.root.path,
            hook_payload={"cwd": str(f.workspace), "session_id": f.session},
            stdout=out, stderr=err, monotonic=lambda: elapsed[0], sleep=tick,
            wait_readable=scripted_readiness(tick))
        self.assertEqual(0, code, err.getvalue())
        self.assertTrue(sent)
        self.assertNotIn(sent[0]["id"], out.getvalue(), "rotation turned an idle poll into a new Stop")
        carried = next(row for row in EventLog(f.root).event_records() if row["id"] == sent[0]["id"])
        self.assertEqual("fyi", carried["message_class"])
        out = io.StringIO()
        code = codex_wait.run_stop_waiter(bus_home=f.root.path,
            hook_payload={"cwd": str(f.workspace), "session_id": f.session}, stdout=out, stderr=err)
        self.assertEqual(0, code, err.getvalue())
        self.assertIn(sent[0]["id"], out.getvalue())


class FyiDaemonTests(unittest.TestCase):
    def setUp(self):
        f = daemon_baseline._WakeDaemonFixture()
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.bind()
        f.consent()
        self.f = f

    def test_idle_daemon_never_dispatches_fyi_but_normal_still_wakes(self):
        f = self.f
        node = f.coordinate.node_id
        try:
            fyi = EventLog(f.root).send("sender", node, "floati", "a" * 40,
                "docs/evidence/ping.md", "non-waking", message_class="fyi",
                idempotency_key="daemon-fyi")
        except TypeError as exc:
            self.fail("sender cannot mark non-waking mail: " + str(exc))
        daemon = f.daemon()
        for now in (100.0, 110.0, 120.0):
            self.assertEqual("idle", daemon.run_cycle(now)["state"])
        self.assertEqual(0, len(f.adapter.calls), "FYI dispatched an idle adapter")
        normal = f.send_unbound("daemon-normal")
        self.assertEqual("woke", daemon.run_cycle(130.0)["state"])
        self.assertEqual(1, len(f.adapter.calls))
        self.assertNotIn(fyi["id"], f.adapter.calls[0][1])
        self.assertIn(normal["id"], f.adapter.calls[0][1])
        messages, _ = EventLog(f.root).present(node)
        self.assertIn(fyi["id"], [row["id"] for row in messages])

    def test_bound_fyi_and_already_presented_fyi_cannot_dispatch(self):
        from floati.wake_hold import WakeHoldController
        f = self.f
        EventLog(f.root).send("sender", f.coordinate.node_id, "floati", "a" * 40,
            "docs/evidence/ping.md", "bound non-waking", message_class="fyi",
            worker_session_id="cursor-session-1", idempotency_key="bound-fyi")
        daemon = f.daemon()
        self.assertEqual("idle", daemon.run_cycle(100.0)["state"])
        EventLog(f.root).present(f.coordinate.node_id, worker_session_id="cursor-session-1")
        with mock.patch("floati.wake_hold._claim_holder_released", return_value=True):
            decision = WakeHoldController(f.root).evaluate(f.coordinate.node_id,
                worker_session_id="cursor-session-1", idempotency_key="held-fyi")
        self.assertFalse(decision["wake_required"], "released claimant resurrected FYI as waking work")
        self.assertEqual("idle", daemon.run_cycle(110.0)["state"])
        self.assertEqual([], f.adapter.calls)


class FyiBridgeTests(unittest.TestCase):
    def assert_legacy_bridge(self, with_normal):
        from tests.test_waiter_lock_retry_bridge import WaiterLockRetryBridgeTests
        f = WaiterLockRetryBridgeTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        sent = []
        def native_hold(workspace, session, deadline):
            elapsed, out, err = [0.0], io.StringIO(), io.StringIO()
            def tick(seconds):
                elapsed[0] += seconds
                if not sent:
                    sent.append(EventLog(f.root).send("sender", "seat", "floati", "a" * 40,
                        "docs/evidence/ping.md", "legacy idle FYI", message_class="fyi",
                        idempotency_key="bridge-fyi"))
                    if with_normal:
                        sent.append(EventLog(f.root).send("sender", "seat", "floati", "a" * 40,
                            "docs/evidence/ping.md", "legacy waking message", idempotency_key="bridge-normal"))
            code = codex_wait.run_stop_waiter(bus_home=f.root.path,
                hook_payload={"cwd": workspace, "session_id": session}, stdout=out, stderr=err,
                monotonic=lambda: elapsed[0], sleep=tick, wait_readable=scripted_readiness(tick))
            self.assertEqual(0, code, err.getvalue())
            self.assertNotIn(sent[0]["id"], out.getvalue())
            return out.getvalue()
        with mock.patch.object(f.bridge, "native_dispatch", return_value=None),\
                mock.patch.object(f.bridge, "hold_for_fresh_work", side_effect=native_hold):
            f.invoke()
        self.assertNotIn(sent[0]["id"], f.output.getvalue(), "legacy idle re-peek injected FYI")
        self.assertFalse(any(sent[0]["id"] in row["item_ids"] for row in f.deliveries()),
            "FYI received a presentation receipt during the idle hold")
        if with_normal:
            self.assertIn(sent[1]["id"], f.output.getvalue())
            self.assertIn("legacy waking message", f.output.getvalue())
        else:
            self.assertEqual("", f.output.getvalue())
        f.output = io.StringIO()
        f.invoke()
        self.assertIn(sent[0]["id"], f.output.getvalue(), "next real Stop did not present the FYI")

    def test_legacy_bridge_does_not_inject_fyi_after_idle_wait_deadline(self):
        self.assert_legacy_bridge(False)

    def test_legacy_bridge_wakes_on_normal_without_injecting_fyi_early(self):
        self.assert_legacy_bridge(True)
