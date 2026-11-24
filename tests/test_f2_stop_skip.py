"""F2-STOP-SKIP-1: overdue attention is unknown, never proof of deafness."""

from datetime import timedelta
import json
from pathlib import Path
from unittest import mock

from floati.doctor import Doctor
from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
from floati.events import EventLog
from floati.stop_hold import StopHoldLedger
from floati.waiter_consent import arm_waiter_consent
from floati.wake_health import WakeHealthProjection
from tests import test_doctor_delivery as native_mail


class StopSkipAttentionTests(native_mail.DeliveryHealthTestBase):
    def setUp(self):
        super().setUp()
        self.registry.register("sender", "worker")
        self.registry.register("seat", "Cursor")

    def doctor(self):
        with mock.patch("floati.doctor._utc_now", return_value=native_mail.NOW):
            return Doctor(Path.cwd(), self.home, ref="HEAD").artifact()

    def attention(self, now=native_mail.NOW):
        return WakeHealthProjection(self.root).seat_attention("seat", now)

    def native_hold(self, minutes_before_now, node="seat", close=False):
        workspace = Path(self.temp.name).resolve() / (node + "-workspace")
        workspace.mkdir(exist_ok=True)
        arm_waiter_consent(self.root.path, workspace, node, "Cursor", 1800, 2)
        binding = resolve_participant(self.root.path, workspace).binding
        consent = CodexWaitConsentLedger(self.root).require_armed(binding)
        ledger = StopHoldLedger(self.root)
        start = ledger.start(binding, consent, "cursor-session",
                             started_at=native_mail._ts(minutes_before_now), deadline_seconds=2)
        if close:
            ledger.close(start, timestamp=start["started_at"])
        return start

    def assert_quiet(self):
        self.assertNotEqual("unknown", self.attention()["state"])

    def test_exact_threshold_is_quiet_and_one_millisecond_later_is_unknown(self):
        self._send_at(15, "sender", "seat")
        self.assert_quiet()
        self.assertEqual("unknown", self.attention(native_mail.NOW + timedelta(milliseconds=1))["state"])

    def test_fresh_mail_does_not_claim_a_skipped_stop(self):
        self._send_at(1, "sender", "seat")
        self.assert_quiet()

    def test_no_mail_never_raises_attention(self):
        self.assert_quiet()

    def test_delivered_mail_never_raises_attention_even_without_ack(self):
        self._send_at(16, "sender", "seat")
        self._drain_at(0, "seat")
        self.assert_quiet()

    def test_another_recipients_mail_is_not_this_seats_attention(self):
        self.registry.register("other", "Cursor")
        self._send_at(16, "sender", "other")
        self.assert_quiet()

    def test_worker_and_smoke_cannot_be_stop_skipped(self):
        for harness in ("worker", "smoke"):
            with self.subTest(harness=harness):
                self.registry.register(harness, harness)
                self._send_at(16, "sender", harness)
                fact = WakeHealthProjection(self.root).seat_attention(harness, native_mail.NOW)
                self.assertEqual("not_applicable", fact["state"])

    def test_a_start_older_than_waiting_mail_does_not_suppress_unknown(self):
        start = self.native_hold(17)
        self._send_at(16, "sender", "seat")
        fact = self.attention()
        self.assertEqual("unknown", fact["state"])
        self.assertEqual(start["started_at"], fact["last_hold_started_at"])

    def test_a_start_since_mail_suppresses_even_after_native_exit(self):
        self._send_at(16, "sender", "seat")
        start = self.native_hold(1, close=True)
        fact = self.attention()
        self.assertEqual("hold_started_since_waiting_mail", fact["reason"])
        self.assertNotEqual("unknown", fact["state"])
        self.assertEqual(1, fact["undelivered_count"], "a hold does not prove delivery")
        self.assertEqual(start["started_at"], fact["last_hold_started_at"])

    def test_future_start_is_not_observed_hold_testimony(self):
        self._send_at(16, "sender", "seat")
        self.native_hold(-1)
        self.assertEqual("unknown", self.attention()["state"])
        self.assertIsNone(self.attention()["last_hold_started_at"])

    def test_fresh_start_after_stale_start_suppresses_attention(self):
        self.native_hold(17, close=True)
        self._send_at(16, "sender", "seat")
        start = self.native_hold(1)
        self.assert_quiet()
        self.assertEqual(start["started_at"], self.attention()["last_hold_started_at"])

    def test_start_at_exact_mail_time_is_a_witness_since_mail(self):
        self._send_at(16, "sender", "seat")
        self.native_hold(16)
        self.assert_quiet()

    def test_fresh_mail_and_unreadable_ledger_do_not_raise_overdue_attention(self):
        self._send_at(1, "sender", "seat")
        with mock.patch.object(StopHoldLedger, "read", side_effect=PermissionError("scratch denial")):
            self.assert_quiet()

    def test_nonordinary_native_hold_path_is_honestly_unknown(self):
        self._send_at(16, "sender", "seat")
        path = self.root.tenant_home / StopHoldLedger.relative("seat")
        path.mkdir(parents=True)
        fact = self.attention()
        self.assertEqual("unknown", fact["state"])
        self.assertEqual("hold_evidence_unreadable", fact["reason"])

    def test_other_nodes_fresh_hold_cannot_suppress_this_seat(self):
        self.registry.register("other", "Cursor")
        self._send_at(16, "sender", "seat")
        self.native_hold(1, node="other")
        self.assertEqual("unknown", self.attention()["state"])

    def test_unreadable_hold_is_named_unknown_instead_of_absent(self):
        self._send_at(16, "sender", "seat")
        with mock.patch.object(StopHoldLedger, "read", side_effect=PermissionError("scratch denial")):
            artifact, code = self.doctor()
        finding = next(row for row in artifact["findings"] if row["code"] == "seat_attention_unknown")
        fact = finding["seat_attention"]
        self.assertEqual("unknown", fact["hold_evidence_state"])
        self.assertEqual("hold_evidence_unreadable", fact["reason"])
        self.assertIn("unknown (hold testimony unreadable)", fact["detail"])
        self.assertNotEqual(33, code)

    def test_shared_reader_preserves_exact_bytes_and_threshold_judgment(self):
        self._send_at(16, "sender", "seat")
        start = self.native_hold(17, close=True)
        path = self.root.tenant_home / StopHoldLedger.relative("seat")
        before = path.read_bytes()
        original = StopHoldLedger.read
        with mock.patch.object(StopHoldLedger, "read", autospec=True, side_effect=original) as read:
            fact = self.attention()
        read.assert_called_once()
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(start["id"], fact["sources"][-1]["record_id"])
        self.assertEqual(15, fact["threshold_minutes"])
        self.assertIn("judgment", fact["threshold_basis"])
        self.assertNotIn("deaf", fact["detail"])

    def test_doctor_and_native_board_share_the_fact_and_render_ambiguity(self):
        from floati.tui import model_from_root
        from floati.tui_render import render_frame, render_plain_dump
        from tests.schema_validation import validate_json_schema
        from floati.cli import _status
        from types import SimpleNamespace

        self._send_at(16, "sender", "seat")
        artifact, _code = self.doctor()
        finding = next(row for row in artifact["findings"] if row["code"] == "seat_attention_unknown")
        from floati.tui_doctor import render_doctor
        self.assertIn("pending approval card", render_doctor(artifact))
        model = model_from_root(self.root, native_mail.NOW)
        seat = next(node for node in model.nodes if node["node_id"] == "seat")
        self.assertEqual(finding["seat_attention"], seat["seat_attention"])
        for rendered in (render_plain_dump(model, width=100),
                         render_frame(model, 100, 100, selected=0, color=False, detail_open=False)):
            self.assertIn("SEAT ATTENTION UNKNOWN", rendered)
            self.assertIn("Floati cannot tell a long-running turn from skipped Stop", rendered)
            self.assertIn("pending approval card", rendered)
        with mock.patch("floati.cli._current_time", return_value=native_mail.NOW):
            _state, status, _code = _status(SimpleNamespace(root=str(self.root.path), json=True))
        validate_json_schema({"artifact_version": 0, "command": "status", "status": "ok", "evidence": status},
                             Path("schemas/v1/fleet-status-artifact.schema.json"))

    def test_status_and_supervise_share_exact_native_attention_at_one_clock(self):
        from floati.projection import FleetProjection
        from floati.supervisor import Supervisor
        from tests.test_supervisor import tree_digest

        self._send_at(16, "sender", "seat")
        before = tree_digest(self.home)
        status = FleetProjection(self.root).snapshot(native_mail.NOW)
        supervised = Supervisor(self.root).snapshot(native_mail.NOW)
        self.assertEqual(before, tree_digest(self.home))
        status_seat = next(node for node in status["nodes"] if node["node_id"] == "seat")
        supervised_seat = next(node for node in supervised["nodes"] if node["node_id"] == "seat")
        self.assertIn("seat_attention", supervised_seat)
        self.assertEqual(status_seat["seat_attention"], supervised_seat["seat_attention"])
        self.assertEqual("unknown", supervised_seat["seat_attention"]["state"])
        self.assertEqual(960.0, supervised_seat["seat_attention"]["oldest_waiting_age_seconds"])
        self.assertIn("cannot tell a long-running turn", supervised_seat["seat_attention"]["detail"])

        start = self.native_hold(1)
        before = tree_digest(self.home)
        status = FleetProjection(self.root).snapshot(native_mail.NOW)
        supervised = Supervisor(self.root).snapshot(native_mail.NOW)
        self.assertEqual(before, tree_digest(self.home))
        status_seat = next(node for node in status["nodes"] if node["node_id"] == "seat")
        supervised_seat = next(node for node in supervised["nodes"] if node["node_id"] == "seat")
        self.assertEqual(status_seat["seat_attention"], supervised_seat["seat_attention"])
        self.assertEqual("hold_started_since_waiting_mail", supervised_seat["seat_attention"]["reason"])
        self.assertEqual(start["started_at"], supervised_seat["seat_attention"]["last_hold_started_at"])
        self.assertEqual(1, supervised_seat["seat_attention"]["undelivered_count"])

    def test_doctor_health_and_attention_share_one_native_observation_clock(self):
        self._send_at(16, "sender", "seat")
        ticks = iter(range(1000))

        def advancing_clock():
            return native_mail.NOW + timedelta(milliseconds=next(ticks))

        original_fact = WakeHealthProjection.fact
        original_attention = WakeHealthProjection.seat_attention
        with mock.patch("floati.doctor._utc_now", side_effect=advancing_clock),\
                mock.patch.object(WakeHealthProjection, "fact", autospec=True,
                                  side_effect=original_fact) as facts,\
                mock.patch.object(WakeHealthProjection, "seat_attention", autospec=True,
                                  side_effect=original_attention) as attention:
            artifact, code = Doctor(Path.cwd(), self.home, ref="HEAD").artifact()
        self.assertNotEqual(33, code, artifact)
        self.assertTrue(any(row["code"] == "seat_attention_unknown" for row in artifact["findings"]))
        fact_clocks = {call.args[1]: call.args[2] for call in facts.call_args_list}
        attention_clocks = {call.args[1]: call.args[2] for call in attention.call_args_list}
        self.assertEqual({"sender", "seat"}, set(fact_clocks))
        self.assertEqual(fact_clocks, attention_clocks)
        self.assertEqual(1, len(set(attention_clocks.values())))

    def test_board_derives_one_threshold_deadline_then_returns_to_blocking(self):
        from floati.tui import _seat_attention_timeout, model_from_root

        self._send_at(15, "sender", "seat")
        model = model_from_root(self.root, native_mail.NOW)
        self.assertAlmostEqual(0.001, _seat_attention_timeout(model, native_mail.NOW))
        overdue = model_from_root(self.root, native_mail.NOW + timedelta(milliseconds=1))
        self.assertIsNone(_seat_attention_timeout(overdue, native_mail.NOW + timedelta(milliseconds=1)))

    def test_native_status_cache_refreshes_mail_attention_and_threshold_once(self):
        from floati.projection import FleetProjection
        from floati.supervisor import Supervisor

        projection = FleetProjection(self.root)
        quiet = projection.status_artifact(native_mail.NOW)
        quiet_seat = next(node for node in quiet["nodes"] if node["node_id"] == "seat")
        self.assertEqual("no_waiting_mail", quiet_seat["seat_attention"]["reason"])
        self._send_at(0, "sender", "seat")
        fresh = projection.status_artifact(native_mail.NOW)
        fresh_seat = next(node for node in fresh["nodes"] if node["node_id"] == "seat")
        supervised = Supervisor(self.root).snapshot(native_mail.NOW, mail=False)
        supervised_seat = next(node for node in supervised["nodes"] if node["node_id"] == "seat")
        self.assertEqual(supervised_seat["seat_attention"], fresh_seat["seat_attention"])
        self.assertEqual("fresh_waiting_mail", fresh_seat["seat_attention"]["reason"])
        self.assertIsNotNone(fresh_seat["seat_attention"]["next_check_at"])
        path = projection._status_snapshot_store().path
        captured = path.read_bytes()
        identity = (path.stat().st_ino, path.stat().st_mtime_ns)
        self.assertEqual(fresh, projection.status_artifact(native_mail.NOW))
        self.assertEqual(captured, path.read_bytes())
        self.assertEqual(identity, (path.stat().st_ino, path.stat().st_mtime_ns),
                         "stable status must reuse its persisted cache")

        later = native_mail.NOW + timedelta(minutes=15, milliseconds=1)
        overdue = projection.status_artifact(later)
        overdue_seat = next(node for node in overdue["nodes"] if node["node_id"] == "seat")
        supervised = Supervisor(self.root).snapshot(later, mail=False)
        supervised_seat = next(node for node in supervised["nodes"] if node["node_id"] == "seat")
        self.assertEqual(supervised_seat["seat_attention"], overdue_seat["seat_attention"])
        self.assertEqual("unknown", overdue_seat["seat_attention"]["state"])
        self.assertIsNone(overdue_seat["seat_attention"]["next_check_at"])
        captured = path.read_bytes()
        self.assertEqual(overdue["observed_at"], json.loads(captured)["payload"]["built_at"])
        identity = (path.stat().st_ino, path.stat().st_mtime_ns)
        self.assertEqual(overdue, projection.status_artifact(later))
        self.assertEqual(captured, path.read_bytes())
        self.assertEqual(identity, (path.stat().st_ino, path.stat().st_mtime_ns))

    def test_native_send_after_no_mail_cache_sets_deadline_and_refreshes_unknown(self):
        from floati.events import EVENT_KINDS
        from floati.jsonl import read_records_snapshot
        from floati.projection import FleetProjection
        from floati.snapshot import SnapshotStore
        from floati.tui import _seat_attention_timeout, model_from_root

        initial = model_from_root(self.root, native_mail.NOW)
        seat = next(node for node in initial.nodes if node["node_id"] == "seat")
        self.assertEqual("no_waiting_mail", seat["seat_attention"]["reason"])
        cached = SnapshotStore(self.root, reader="board", key="full-redraw",
                               discover_sources=FleetProjection(self.root)._status_sources).load()
        self.assertFalse(cached.payload["time_sensitive"])
        with mock.patch("floati.tui._model_from_root_full", side_effect=AssertionError("cache absent")):
            self.assertEqual(initial, model_from_root(self.root, native_mail.NOW))

        self._send_at(0, "sender", "seat")
        frames = read_records_snapshot(self.root, "events.jsonl", allowed_kinds=EVENT_KINDS)
        self.assertEqual(["message_envelope", "send_readiness_observation"],
                         [frame["kind"] for frame in frames])
        after_send = model_from_root(self.root, native_mail.NOW)
        seat = next(node for node in after_send.nodes if node["node_id"] == "seat")
        self.assertIsNotNone(seat["seat_attention"]["next_check_at"])
        self.assertEqual("fresh_waiting_mail", seat["seat_attention"]["reason"])
        self.assertEqual(1, seat["inbox_depth"])
        self.assertAlmostEqual(900.001, _seat_attention_timeout(after_send, native_mail.NOW))

        overdue_at = native_mail.NOW + timedelta(minutes=15, milliseconds=1)
        overdue = model_from_root(self.root, overdue_at)
        seat = next(node for node in overdue.nodes if node["node_id"] == "seat")
        self.assertEqual("unknown", seat["seat_attention"]["state"])
        self.assertIsNone(_seat_attention_timeout(overdue, overdue_at))
        with mock.patch("floati.doctor._utc_now", return_value=overdue_at):
            artifact, _code = Doctor(Path.cwd(), self.home, ref="HEAD").artifact()
        finding = next(row for row in artifact["findings"] if row["code"] == "seat_attention_unknown")
        self.assertEqual(finding["seat_attention"], seat["seat_attention"])

    def test_pre_stop_attention_cache_rebuilds_native_mail_deadline_once(self):
        from floati.projection import FleetProjection
        from floati.snapshot import SnapshotStore
        from floati.tui import _model_from_root_full, _seat_attention_timeout, model_from_root

        model_from_root(self.root, native_mail.NOW)
        store = SnapshotStore(self.root, reader="board", key="full-redraw",
                              discover_sources=FleetProjection(self.root)._status_sources)
        legacy = store.load().payload
        for node in legacy["model"]["nodes"]:
            node.pop("seat_attention")
        self.assertFalse(legacy["time_sensitive"])
        # Pre-STOP nodes lack this additive fact. The native writer retains
        # the real ledger anchors and checksum; no reader-code pin exists.
        store.refresh(legacy, expected=store.capture())
        self.assertEqual(legacy, store.load().payload)
        self.assertTrue(all(not records for records in store.load().tails.values()))

        self._send_at(0, "sender", "seat")
        with mock.patch("floati.tui._model_from_root_full", wraps=_model_from_root_full) as full:
            refreshed = model_from_root(self.root, native_mail.NOW)
            self.assertIsNotNone(_seat_attention_timeout(refreshed, native_mail.NOW))
            self.assertAlmostEqual(900.001, _seat_attention_timeout(refreshed, native_mail.NOW))
            seat = next(node for node in refreshed.nodes if node["node_id"] == "seat")
            self.assertEqual("fresh_waiting_mail", seat["seat_attention"]["reason"])
            self.assertEqual(1, full.call_count)
            self.assertEqual(refreshed, model_from_root(self.root, native_mail.NOW))
            self.assertEqual(1, full.call_count, "upgraded cache must be reusable")

            overdue_at = native_mail.NOW + timedelta(minutes=15, milliseconds=1)
            overdue = model_from_root(self.root, overdue_at)
            seat = next(node for node in overdue.nodes if node["node_id"] == "seat")
            self.assertEqual("unknown", seat["seat_attention"]["state"])
            self.assertIsNone(_seat_attention_timeout(overdue, overdue_at))
            self.assertEqual(2, full.call_count)
            self.assertEqual(overdue, model_from_root(self.root, overdue_at))
            self.assertEqual(2, full.call_count, "threshold crossing must not busy-loop")

    def test_interactive_board_refreshes_once_when_mail_becomes_overdue(self):
        import io
        import os
        from floati.tui import model_from_root, run_board

        class TTY(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 91

        self._send_at(15, "sender", "seat")
        times = iter([native_mail.NOW, native_mail.NOW + timedelta(milliseconds=1)])
        model_loader = mock.Mock(side_effect=lambda: model_from_root(self.root, next(times)))
        timeouts = []
        def read_event(timeout):
            timeouts.append(timeout)
            return "" if len(timeouts) == 1 else "q"

        output = TTY()
        with mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}),\
             mock.patch("floati.tui.termios.tcgetattr", return_value=[]),\
             mock.patch("floati.tui.termios.tcsetattr"),\
             mock.patch("floati.tui.tty.setcbreak"),\
             mock.patch("floati.tui.datetime") as clock,\
             mock.patch("floati.tui.shutil.get_terminal_size", return_value=os.terminal_size((300, 100))):
            clock.now.return_value = native_mail.NOW
            clock.fromisoformat.side_effect = native_mail.datetime.fromisoformat
            code = run_board(model_loader=model_loader, input_stream=TTY(), output_stream=output,
                             terminal_response=b"\x1b[?1;2c", read_event=read_event)
        self.assertEqual(0, code)
        self.assertEqual(2, model_loader.call_count)
        self.assertAlmostEqual(0.001, timeouts[0])
        self.assertIsNone(timeouts[1])
        self.assertIn("SEAT ATTENTION UNKNOWN", output.getvalue())

    def test_native_doctor_names_aged_mail_without_hold_as_unknown_attention(self):
        self._send_at(16, "sender", "seat")
        artifact, code = self.doctor()
        findings = [row for row in artifact["findings"]
                    if row["code"] == "seat_attention_unknown"]
        self.assertEqual(1, len(findings), artifact)
        finding = findings[0]
        self.assertEqual("seat", finding["subject"])
        self.assertEqual("warning", finding["severity"])
        self.assertIn("16m", finding["detail"])
        self.assertIn("Floati cannot tell a long-running turn from skipped Stop", finding["detail"])
        self.assertIn("pending approval card", finding["remediation"])
        self.assertNotEqual(33, code)
