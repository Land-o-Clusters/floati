"""Behavioral regressions for native automatic epoch activity boundaries."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati.bus_epoch import epoch_guard, ledger_roll_policy, roll_bus_epoch
from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import (
    CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant, write_workspace_map)
from floati.cursor import SparseCursor
from floati.epoch_rotation import maintenance, observe_policy
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_control import WakeController
from tests.temp_roots import REAL_TEMP_ROOT
from tests.waiter_timing import scripted_readiness


class EpochRotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.home / "fleet-home", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")

    def policy(self, *, size=1, days=7):
        path = self.root.path / "state/ledger-policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 0, "max_bytes": size, "max_age_days": days}))

    def send(self, key, **options):
        return EventLog(self.root).send("sender", "recipient", "floati", "a" * 40,
            "AGENTS.md", "fixture " + key, idempotency_key=key, **options)

    def archives(self):
        return list(self.root.path.glob("archive-*"))

    def test_native_send_rotates_without_grant_and_preserves_unread(self):
        self.policy()
        first, second = self.send("first"), self.send("second")
        self.assertEqual(1, len(self.archives()))
        self.assertEqual([first["id"], second["id"]], EventLog(self.root).unacked_ids("recipient"))
        first_frame = json.loads((self.root.path / "events.jsonl").read_bytes().splitlines()[0])
        self.assertEqual("sender", first_frame["actor"])
        self.assertEqual("send", first_frame["automatic"]["trigger"])
        self.assertEqual(["bytes"], first_frame["automatic"]["due_reasons"])

    def test_manual_roll_still_requires_its_exact_grant(self):
        self.send("manual")
        with self.assertRaises(ProtocolRefusal):
            roll_bus_epoch(self.root, actor="sender", idempotency_key="no-grant")
        self.assertEqual([], self.archives())

    def test_compact_carry_commits_to_every_retained_envelope_before_new_sends(self):
        from floati.epoch_teardown import carried_envelope_digests
        first = self.send("carried")
        self.policy()
        with mock.patch("floati.epoch_teardown.MAX_RECORD_BYTES", 400):
            second = self.send("also-carried")
        later = self.send("later")
        events = EventLog(self.root).strict_event_records()
        self.assertEqual({first["id"], second["id"]}, set(carried_envelope_digests(events)))
        self.assertNotIn(later["id"], carried_envelope_digests(events))
        changed = [dict(row, note="changed") if row["id"] == first["id"] else row for row in events]
        with self.assertRaises(IntegrityFailure) as caught:
            carried_envelope_digests(changed)
        self.assertEqual("epoch_carry_verification_invalid", caught.exception.code)
        from floati.framing import encode_frame
        from floati.send_prepare import prepare
        with mock.patch("floati.send_prepare.STEP_FRAMES", 1):
            for rows in (events, changed):
                EventLog(self.root).path.write_bytes(b"".join(map(encode_frame, rows)))
                self.assertEqual("pending", prepare(self.root, status=True)["state"])
                for _ in rows[:-1]:
                    self.assertEqual("in_progress", prepare(self.root)["state"])
                if rows is events:
                    self.assertEqual("done", prepare(self.root)["state"])
                else:
                    with self.assertRaises(IntegrityFailure) as caught:
                        prepare(self.root)
                    self.assertEqual("epoch_carry_verification_invalid", caught.exception.code)

    def test_invalid_native_send_cannot_rotate_a_due_normal_epoch(self):
        self.policy()
        self.send("due")
        before = (self.root.path / "events.jsonl").read_bytes()
        bad = (
            ("sender", "missing", "floati", "a" * 40, "AGENTS.md", "bad"),
            ("sender", "recipient", "floati", "bad-sha", "AGENTS.md", "bad"),
            ("sender", "recipient", "floati", "a" * 40, "../escape", "bad"),
        )
        for args in bad:
            with self.subTest(args=args):
                with self.assertRaises((ProtocolRefusal, ValueError)):
                    EventLog(self.root).send(*args)
                self.assertEqual(before, (self.root.path / "events.jsonl").read_bytes())
                self.assertEqual([], self.archives())
        with self.assertRaises(TypeError):
            EventLog(self.root).send("sender")
        self.assertEqual([], self.archives())

    def test_committed_send_result_survives_post_success_maintenance_failure(self):
        self.policy()
        self.send("first")
        diagnostic = io.StringIO()
        with mock.patch("floati.epoch_rotation.maintenance",
                side_effect=IntegrityFailure("fixture_maintenance_failed", "private fixture detail")), (
                mock.patch("sys.stderr", diagnostic)):
            message = self.send("committed")
        self.assertIn(message["id"], EventLog(self.root).unacked_ids("recipient"))
        artifact = json.loads(diagnostic.getvalue())
        self.assertEqual("maintenance_failed", artifact["status"])
        self.assertEqual("fixture_maintenance_failed", artifact["code"])
        self.assertEqual("send", artifact["trigger"])
        self.assertIn(message["id"], artifact["operation_receipt_ids"])
        self.assertTrue(artifact["operation_result_preserved"])
        self.assertNotIn("private fixture detail", diagnostic.getvalue())

    def test_invalid_policy_refuses_before_a_native_send_commits(self):
        self.send("first")
        before = (self.root.path / "events.jsonl").read_bytes()
        path = self.root.path / "state/ledger-policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema_version":0,"max_bytes":false,"max_age_days":7}')
        with self.assertRaises(ProtocolRefusal) as caught:
            self.send("blocked")
        self.assertEqual("ledger_policy_invalid", caught.exception.code)
        self.assertEqual(before, (self.root.path / "events.jsonl").read_bytes())

    def test_policy_symlink_retains_native_refusal_and_remedy_at_activity_boundaries(self):
        import floati.epoch_rotation as rotation
        self.policy(size=1024)
        path = self.root.path / "state/ledger-policy.json"
        path.rename(path.with_name("real-policy.json"))
        path.symlink_to("real-policy.json")
        with self.assertRaises(ProtocolRefusal) as native:
            ledger_roll_policy(self.root)
        expected = (native.exception.code, native.exception.remedy)
        self.assertEqual("ledger_policy_symlink", expected[0])
        self.assertTrue(expected[1])
        for operation in (
                lambda: observe_policy(self.root, now=datetime.now(timezone.utc)),
                lambda: self.send("policy-symlink-refused")):
            with self.assertRaises(ProtocolRefusal) as caught:
                operation()
            self.assertEqual(expected, (caught.exception.code, caught.exception.remedy))
        failures = []
        native_stop = rotation.stop_maintenance
        def capture_native_failure(root, **options):
            try:
                return native_stop(root, **options)
            except ProtocolRefusal as failure:
                failures.append((failure.code, failure.remedy))
                raise
        with mock.patch.object(rotation, "stop_maintenance", capture_native_failure):
            result = self._run_policy_stop()
        self.assertEqual([expected], failures)
        self.assertEqual("integrity_failure", result["kind"])
        self.assertEqual(expected[0], result["code"])
        self.assertFalse((self.root.path / "events.jsonl").exists())
        self.assertEqual([], self.archives())

    def test_policy_directory_uses_native_defaults_for_observation_send_and_stop(self):
        path = self.root.path / "state/ledger-policy.json"
        path.mkdir(parents=True)
        expected = ledger_roll_policy(self.root)
        self.assertEqual("shipped_default", expected["source"])
        observation = observe_policy(self.root, now=datetime.now(timezone.utc))
        self.assertEqual(expected, observation["policy"])
        message = self.send("policy-directory")
        EventLog(self.root).present("recipient")
        SparseCursor(self.root).ack("recipient", [message["id"]], acting_session_id="history")
        result = self._run_policy_stop()
        self.assertEqual("exhausted", result["kind"])
        self.assertTrue(path.is_dir())
        self.assertEqual([], self.archives())

    def _run_policy_stop(self):
        workspace = self.home / "policy-workspace"
        workspace.mkdir()
        write_workspace_map(self.root.path, workspace, "recipient")
        participant = resolve_participant(self.root.path, workspace)
        consent = CodexWaitConsentLedger(self.root).arm(participant.binding,
            hook_timeout_seconds=2, wait_deadline_seconds=1, idempotency_key="policy-stop-consent")
        CodexWaitSessionLedger(self.root).arm(participant.binding, consent, "policy-stop-session",
            idempotency_key="policy-stop-claim")
        clock = {"mono": 0.0}
        def sleep(seconds):
            clock["mono"] += seconds
        result = {}
        code = run_stop_waiter(bus_home=self.root.path,
            hook_payload={"cwd": str(workspace), "session_id": "policy-stop-session"},
            stdout=io.StringIO(), stderr=io.StringIO(),
            monotonic=lambda: clock["mono"], wait_readable=scripted_readiness((sleep), interval=(1.0)), sleep=sleep, _waiter_result=result)
        self.assertEqual(0, code)
        return result

    def test_first_frame_hint_preserves_native_integrity_codes_before_send(self):
        from floati.jsonl import MAX_RECORD_BYTES
        self.send("seed-frame")
        path = self.root.path / "events.jsonl"
        frame = path.read_bytes().splitlines(keepends=True)[0]
        invalid_timestamp = dict(json.loads(frame), timestamp="not-a-timestamp")
        mutations = (
            (b"{bad}\n", "malformed_json"),
            (frame[:-1], "incomplete_jsonl_line"),
            (b"[]\n", "record_not_object"),
            (b"\n", "blank_jsonl_line"),
            (b'{"kind":"one","kind":"two"}\n', "duplicate_json_key"),
            (b'{"value":"\xff"}\n', "malformed_json"),
            ((json.dumps(invalid_timestamp) + "\n").encode(), "timestamp_invalid"),
            (b"x" * MAX_RECORD_BYTES + b"\n", "record_too_large"),
        )
        for payload, code in mutations:
            with self.subTest(code=code, payload_bytes=len(payload)):
                path.write_bytes(payload)
                with self.assertRaises(IntegrityFailure) as hint_failure:
                    observe_policy(self.root, now=datetime.now(timezone.utc))
                self.assertEqual(code, hint_failure.exception.code)
                with self.assertRaises(IntegrityFailure) as send_failure:
                    self.send("must-not-commit")
                self.assertEqual(code, send_failure.exception.code)
                self.assertEqual(payload, path.read_bytes())
                self.assertEqual([], self.archives())

    def test_incomplete_first_frame_observation_retains_measured_offset_and_witness(self):
        path = self.root.path / "events.jsonl"
        path.write_bytes(b'{"private_fixture":"incomplete"')
        original = path.stat()
        with self.assertRaises(IntegrityFailure) as caught:
            observe_policy(self.root, now=datetime.now(timezone.utc))
        failure = caught.exception
        self.assertEqual("incomplete_jsonl_line", failure.code)
        self.assertEqual({"path": "events.jsonl", "byte_offset": 0}, failure.waiter_input_identity)
        self.assertEqual((path, (original.st_dev, original.st_ino, original.st_mode,
            original.st_size, original.st_mtime_ns, original.st_ctime_ns, original.st_nlink)),
            failure.waiter_input_witness)

    def test_first_frame_failure_derives_parent_payload_identity_without_disk_witness(self):
        import floati.epoch_rotation as rotation
        payload = b"{bad}\n" + b"{\"later\":true}\n"
        with self.assertRaises(IntegrityFailure) as caught:
            rotation._first_stamp(self.root, payload)
        failure = caught.exception
        self.assertEqual("malformed_json", failure.code)
        self.assertEqual((self.root.path / "events.jsonl", id(payload)), failure._waiter_payload_source)
        self.assertIsNone(getattr(failure, "waiter_input_witness", None))

    def test_first_frame_failure_cannot_rebind_foreign_or_copied_child_source(self):
        import floati.epoch_rotation as rotation
        import floati.jsonl as jsonl
        payload = b"{bad}\n" + b"{\"later\":true}\n"
        for case in ("foreign-coordinate", "copied-child"):
            with self.subTest(case=case):
                failure = IntegrityFailure("malformed_json", "private fixture parser failure")
                retained = []
                def foreign_decode(path, tenant, kinds, child, **options):
                    if case == "foreign-coordinate":
                        source = (self.home / "foreign.jsonl", id(child))
                    else:
                        copied = bytes(bytearray(child))
                        self.assertIsNot(child, copied)
                        retained.append(copied)
                        source = (path, id(copied))
                    failure._waiter_payload_source = source
                    raise failure
                with mock.patch.object(jsonl, "_decode_path_records", foreign_decode):
                    with self.assertRaises(IntegrityFailure) as caught:
                        rotation._first_stamp(self.root, payload)
                self.assertIs(failure, caught.exception)
                self.assertNotEqual((self.root.path / "events.jsonl", id(payload)), failure._waiter_payload_source)
                self.assertIsNone(getattr(failure, "waiter_input_witness", None))

    def test_stop_first_frame_failure_keeps_native_outer_code_and_inner_parser_check(self):
        from floati.jsonl import read_records_snapshot
        path = self.root.path / "events.jsonl"
        path.write_bytes(b'{"private_fixture":"incomplete"')
        result = self._run_policy_stop()
        self.assertEqual("integrity_failure", result["kind"])
        self.assertEqual("consumption_state_unavailable", result["code"])
        self.assertIn("consumption_state_unavailable", result["decision"]["reason"])
        self.assertIn("incomplete_jsonl_line", result["decision"]["reason"])
        rows = read_records_snapshot(self.root, "receipts/wake-waiter-exit/recipient.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"})
        diagnostic = rows[-1]["integrity_failure"]
        self.assertEqual("incomplete_jsonl_line", diagnostic["check_id"])
        self.assertEqual("events.jsonl", diagnostic["input_identity"]["path"])
        self.assertEqual(0, diagnostic["input_identity"]["byte_offset"])
        self.assertNotIn("private_fixture", json.dumps(result))

    def test_unknown_future_first_frame_skips_age_and_allows_native_send(self):
        from floati.framing import encode_frame
        future = encode_frame({"schema_version": 99, "id": "future-first",
            "tenant_id": self.root.tenant_id, "kind": "future_epoch_fixture",
            "timestamp": "2000-01-01T00:00:00.000Z"})
        path = self.root.path / "events.jsonl"
        path.write_bytes(future)
        observation = observe_policy(self.root, now=datetime.now(timezone.utc))
        self.assertIsNone(observation["epoch_started_at"])
        self.assertNotIn("age", observation["due_reasons"])
        message = self.send("after-future-first")
        self.assertTrue(path.read_bytes().startswith(future))
        self.assertIn(message["id"], EventLog(self.root).unacked_ids("recipient"))
        self.assertEqual([], self.archives())

    def test_committed_ack_result_survives_post_success_maintenance_failure(self):
        message = self.send("acknowledge")
        EventLog(self.root).present("recipient")
        self.policy()
        diagnostic = io.StringIO()
        with mock.patch("floati.epoch_rotation.maintenance",
                side_effect=IntegrityFailure("fixture_maintenance_failed", "private fixture detail")), (
                mock.patch("sys.stderr", diagnostic)):
            receipt = SparseCursor(self.root).ack("recipient", [message["id"]], acting_session_id="fixture")
        self.assertEqual([], EventLog(self.root).unacked_ids("recipient"))
        artifact = json.loads(diagnostic.getvalue())
        self.assertEqual("ack", artifact["trigger"])
        self.assertEqual([receipt["id"]], artifact["operation_receipt_ids"])
        self.assertTrue(artifact["operation_result_preserved"])

    def test_nested_native_call_never_upgrades_shared_epoch(self):
        self.policy()
        first = self.send("first")
        with epoch_guard(self.root):
            second = self.send("nested")
        self.assertEqual([], self.archives())
        self.assertEqual([first["id"], second["id"]], EventLog(self.root).unacked_ids("recipient"))

    def test_anchored_unread_set_is_not_rolled_repeatedly(self):
        self.policy()
        self.send("first")
        self.send("second")
        for key in ("third", "fourth", "fifth"):
            self.send(key)
        self.assertEqual(1, len(self.archives()))
        self.assertEqual(5, len(EventLog(self.root).unacked_ids("recipient")))
        result = maintenance(self.root, actor="sender", trigger="send")
        self.assertFalse(result["changed"])
        self.assertEqual("nonreclaimable", result["action"])

    def test_native_ack_makes_closed_history_reclaimable(self):
        self.policy()
        self.send("first")
        self.send("second")
        rows, _receipt = EventLog(self.root).present("recipient")
        SparseCursor(self.root).ack("recipient", [row["id"] for row in rows], acting_session_id="fixture")
        self.assertEqual([], EventLog(self.root).unacked_ids("recipient"))
        self.assertGreaterEqual(len(self.archives()), 2)

    def test_public_activity_recovers_existing_commit_before_one_retry(self):
        from floati.bus_epoch import roll_automatic_epoch
        self.policy()
        first = self.send("pending-recovery")
        def fault(boundary):
            if boundary == "marker_committed_durable":
                raise RuntimeError("fixture committed interruption")
        def interrupted(root, *, actor, planner):
            return roll_automatic_epoch(root, actor=actor, planner=planner, fault=fault)
        with mock.patch("floati.bus_epoch.roll_automatic_epoch", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                maintenance(self.root, actor="sender", trigger="send")
        self.assertTrue(list(self.root.path.glob(".floati-epoch-roll-*")))
        second = self.send("after-recovery")
        self.assertEqual([], list(self.root.path.glob(".floati-epoch-roll-*")))
        self.assertEqual([first["id"], second["id"]], EventLog(self.root).unacked_ids("recipient"))
        rows = EventLog(self.root).strict_event_records()
        self.assertEqual(1, sum(row["kind"] == "message_envelope"
                               and row.get("idempotency_key") == "after-recovery" for row in rows))

    def test_cached_epoch_age_reaches_due_without_any_file_change(self):
        moment = datetime.now(timezone.utc)
        self.send("aged", now=moment - timedelta(days=7) + timedelta(seconds=1))
        before = observe_policy(self.root, now=moment)
        after = observe_policy(self.root, now=moment + timedelta(seconds=2))
        self.assertNotIn("age", before["due_reasons"])
        self.assertIn("age", after["due_reasons"])

    def test_age_uses_current_anchor_instead_of_old_carried_message(self):
        self.policy()
        self.send("old", now=datetime.now(timezone.utc) - timedelta(days=30))
        self.send("establish-anchor")
        observation = observe_policy(self.root, now=datetime.now(timezone.utc))
        self.assertNotIn("age", observation["due_reasons"])

    def test_epoch_and_daemon_defaults_remain_distinct(self):
        from floati.bus_epoch import lifecycle_ledger_roll_policy
        self.assertEqual(7, ledger_roll_policy(self.root)["max_age_days"])
        self.assertEqual(30, lifecycle_ledger_roll_policy(self.root)["max_age_days"])
        self.policy(size=1024, days=11)
        self.assertEqual(ledger_roll_policy(self.root), lifecycle_ledger_roll_policy(self.root))

    def test_idle_waiter_external_parent_churn_does_not_replay_selected_ledgers(self):
        from floati import codex_wait
        Registry(self.root).register("idle-worker", "worker")
        workspace = self.home / "idle-workspace"
        workspace.mkdir()
        write_workspace_map(self.root.path, workspace, "idle-worker")
        participant = resolve_participant(self.root.path, workspace)
        CodexWaitConsentLedger(self.root).arm(participant.binding, hook_timeout_seconds=10,
            wait_deadline_seconds=2, idempotency_key="generation-consent")
        EventLog(self.root).send("sender", "idle-worker", "floati", "a" * 40, "AGENTS.md",
            "native generation fixture", idempotency_key="generation-held")
        payload = {"cwd": str(workspace), "session_id": "generation-session"}
        first = {}
        run_stop_waiter(bus_home=self.root.path, hook_payload=payload,
            stdout=io.StringIO(), stderr=io.StringIO(), _waiter_result=first)
        self.assertEqual("fresh_work", first["kind"])
        paths = {self.root.path / "events.jsonl",
            self.root.path / "receipts/deliveries/idle-worker.jsonl"}
        counts = {path: 0 for path in paths}
        native_read = Path.read_bytes
        native_generation = codex_wait._WaiterInputsWatch._generation
        generations, mutations, clock = [], [], [0.0]
        def observed_read(path):
            resolved = path.resolve(strict=False)
            if resolved in counts:
                counts[resolved] += 1
            return native_read(path)
        def observed_generation(watch):
            observed = native_generation(watch)
            generations.append(True)
            if len(generations) in (1, 3):
                (self.home / ("unrelated-sibling-" + str(len(generations)))).mkdir()
                mutations.append(True)
            return observed
        result = {}
        with mock.patch.object(Path, "read_bytes", observed_read), (
                mock.patch.object(codex_wait._WaiterInputsWatch, "_generation", observed_generation)):
            run_stop_waiter(bus_home=self.root.path, hook_payload=payload,
                stdout=io.StringIO(), stderr=io.StringIO(), monotonic=lambda: clock[0],
                wait_readable=scripted_readiness((lambda seconds: clock.__setitem__(0, clock[0] + seconds)), interval=(1.0)), sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                poll_interval_seconds=1.0, _waiter_result=result)
        self.assertTrue(mutations)
        self.assertEqual("exhausted", result["kind"])
        self.assertEqual({path: 1 for path in paths}, counts)

    def test_generation_boundary_still_invalidates_selected_changes_and_ancestor_replacement(self):
        import hashlib
        from floati import codex_wait
        native_generation = codex_wait._WaiterInputsWatch._generation
        session = "generation-control-session"
        for case in ("tenant-membership", "selected-parent-membership", "selected-rewrite",
                "ancestor-replacement", "ancestor-symlink", "ancestor-mode", "ancestor-disappearance", "authority-pause"):
            with self.subTest(case=case):
                # Destructive ancestry controls must not leave unavailable map
                # entries in the next case's native participant resolution.
                root = FloatiRoot.open_direct_home(self.home / ("fleet-" + case), create=True)
                for node in ("sender", "recipient"):
                    Registry(root).register(node, "Codex")
                EventLog(root).send("sender", "recipient", "floati", "a" * 40,
                    "AGENTS.md", "fixture generation-controls", idempotency_key="generation-controls")
                parent = self.home / ("caller-" + case)
                workspace = parent / "workspace"
                workspace.mkdir(parents=True)
                write_workspace_map(root.path, workspace, "recipient")
                participant = resolve_participant(root.path, workspace)
                self.assertIsNotNone(participant)
                (root.path / "receipts").mkdir(exist_ok=True)
                injected = []
                def observed_generation(watch):
                    observed = native_generation(watch)
                    if not injected:
                        injected.append(True)
                        if case == "tenant-membership":
                            (root.path / "new-selected-directory").mkdir()
                        elif case == "selected-parent-membership":
                            (root.path / "receipts/new-unselected-child").mkdir()
                        elif case == "selected-rewrite":
                            path = root.path / "events.jsonl"
                            before = path.stat()
                            path.write_bytes(path.read_bytes().replace(b"fixture", b"changed", 1))
                            self.assertEqual(before.st_ino, path.stat().st_ino)
                            self.assertEqual(before.st_size, path.stat().st_size)
                        elif case in ("ancestor-replacement", "ancestor-symlink", "ancestor-disappearance"):
                            before = workspace.stat()
                            displaced = parent.with_name(parent.name + "-displaced")
                            parent.rename(displaced)
                            if case == "ancestor-replacement":
                                parent.mkdir()
                                (displaced / "workspace").rename(workspace)
                                self.assertEqual(before.st_ino, workspace.stat().st_ino)
                            elif case == "ancestor-symlink":
                                parent.symlink_to(displaced, target_is_directory=True)
                                self.assertEqual(before.st_ino, workspace.stat().st_ino)
                        elif case == "ancestor-mode":
                            import stat
                            parent.chmod(parent.stat().st_mode ^ stat.S_IWGRP)
                        else:
                            WakeController(root).pause("recipient", session, idempotency_key="generation-pause")
                    return observed
                with mock.patch.object(codex_wait._WaiterInputsWatch, "_generation", observed_generation):
                    watch = codex_wait._WaiterInputsWatch(participant,
                        hashlib.sha256(session.encode()).hexdigest(), io.StringIO(), workspace=workspace)
                try:
                    self.assertTrue(injected)
                    self.assertTrue(watch.generation_unstable)
                    self.assertTrue(watch.changed()[1])
                finally:
                    watch.close()

    def test_generation_boundary_unreadable_external_ancestor_never_becomes_clean(self):
        import hashlib
        from floati import codex_wait
        parent = self.home / "unreadable-parent"
        workspace = parent / "workspace"
        workspace.mkdir(parents=True)
        write_workspace_map(self.root.path, workspace, "recipient")
        participant = resolve_participant(self.root.path, workspace)
        native_generation = codex_wait._WaiterInputsWatch._generation
        native_lstat = Path.lstat
        unavailable = []
        def observed_generation(watch):
            observed = native_generation(watch)
            unavailable.append(True)
            return observed
        def unavailable_lstat(path, *args, **kwargs):
            if unavailable and path == parent:
                raise PermissionError("private fixture ancestor metadata unavailable")
            return native_lstat(path, *args, **kwargs)
        with mock.patch.object(codex_wait._WaiterInputsWatch, "_generation", observed_generation), (
                mock.patch.object(Path, "lstat", unavailable_lstat)):
            watch = codex_wait._WaiterInputsWatch(participant,
                hashlib.sha256(b"unreadable-generation-session").hexdigest(), io.StringIO(), workspace=workspace)
            try:
                self.assertTrue(watch.generation_unstable)
                self.assertTrue(watch.changed()[1])
            finally:
                watch.close()

    def test_stop_checks_age_after_an_unchanged_idle_sleep(self):
        self._run_stop_age_authority_case("current")

    def test_legacy_stop_without_matching_claim_can_maintain_due_epoch(self):
        self._run_stop_age_authority_case("legacy")

    def test_stop_consent_replacement_under_ex_prevents_due_rotation(self):
        self._run_stop_age_authority_case("consent_withdrawn")

    def test_stop_session_supersession_under_ex_prevents_due_rotation(self):
        self._run_stop_age_authority_case("superseded_session")

    def _run_stop_age_authority_case(self, authority_case):
        import floati.epoch_rotation as rotation
        import floati.jsonl as jsonl
        from floati.bus_epoch import _require_owned_epoch_guard
        moment = datetime.now(timezone.utc) - timedelta(minutes=1)
        clock = {"wall": moment, "mono": 0.0, "sleeps": 0}
        authority_checks = []
        legacy_reads = []
        outcomes = []
        native_maintenance = rotation.stop_maintenance
        native_snapshot = jsonl.read_records_snapshot
        notification = []
        class ClockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.fromtimestamp(clock["wall"].timestamp(), tz=tz or timezone.utc)
        with mock.patch.object(rotation, "datetime", ClockDateTime):
            message = self.send("near-age", now=moment - timedelta(days=7) + timedelta(seconds=5))
            EventLog(self.root).present("recipient")
            SparseCursor(self.root).ack("recipient", [message["id"]], acting_session_id="history")
            workspace = self.home / "workspace"
            workspace.mkdir()
            write_workspace_map(self.root.path, workspace, "recipient")
            participant = resolve_participant(self.root.path, workspace)
            consent = CodexWaitConsentLedger(self.root).arm(participant.binding,
                hook_timeout_seconds=360, wait_deadline_seconds=300, idempotency_key="stop-consent")
            CodexWaitSessionLedger(self.root).arm(participant.binding, consent, "stop-session",
                idempotency_key="stop-claim")
            claim_path = CodexWaitSessionLedger(self.root)._relative("recipient")
            def legacy_snapshot(root, relative, **options):
                # Bootstrap retains its genuine claim; only the authority reader
                # sees the supported legacy empty-claim snapshot. Rotation and
                # its private native ledgers remain real and unmodified.
                if relative == claim_path:
                    legacy_reads.append(True)
                    return []
                return native_snapshot(root, relative, **options)
            def observe_maintenance(root, *, actor, check_authority):
                def checked_authority():
                    authority_checks.append(True)
                    if len(authority_checks) == 2 and authority_case in (
                            "consent_withdrawn", "superseded_session"):
                        _require_owned_epoch_guard(root, exclusive=True)
                        if authority_case == "consent_withdrawn":
                            CodexWaitConsentLedger(root).arm(participant.binding,
                                hook_timeout_seconds=360, wait_deadline_seconds=300,
                                idempotency_key="replacement-consent")
                        else:
                            CodexWaitSessionLedger(root).arm(participant.binding, consent,
                                "replacement-session", take_over=True,
                                idempotency_key="replacement-claim")
                    if authority_case == "legacy":
                        with mock.patch.object(jsonl, "read_records_snapshot", legacy_snapshot):
                            check_authority()
                    else:
                        check_authority()
                outcome = native_maintenance(root, actor=actor, check_authority=checked_authority)
                outcomes.append(outcome)
                return outcome
            def sleep(seconds):
                clock["mono"] += seconds
                clock["wall"] += timedelta(seconds=6)
                clock["sleeps"] += 1
                if clock["sleeps"] == 2:
                    notification.append(self.send("post-age-exact-notification"))
            result = {}
            with mock.patch.object(rotation, "stop_maintenance", observe_maintenance):
                code = run_stop_waiter(bus_home=self.root.path,
                    hook_payload={"cwd": str(workspace), "session_id": "stop-session"},
                    stdout=io.StringIO(), stderr=io.StringIO(),
                    monotonic=lambda: clock["mono"], wait_readable=scripted_readiness((sleep), interval=(1.0)), sleep=sleep, _waiter_result=result)
        self.assertEqual(0, code)
        if authority_case in ("consent_withdrawn", "superseded_session"):
            self.assertEqual(2, len(authority_checks))
            self.assertEqual([], self.archives())
            self.assertEqual("authority_changed", result.get("kind"))
            self.assertEqual(authority_case, result.get("reason"))
        else:
            self.assertGreaterEqual(len(authority_checks), 3)
            if authority_case == "legacy":
                self.assertGreaterEqual(len(legacy_reads), 3)
            self.assertTrue(any(outcome["changed"] for outcome in outcomes))
            self.assertEqual(1, len(self.archives()))
            self.assertEqual("fresh_work", result.get("kind"), result)
            self.assertEqual([notification[0]["id"]], result["item_ids"])
            self.assertEqual(result["item_ids"], result["receipt"]["item_ids"])


if __name__ == "__main__":
    unittest.main()
