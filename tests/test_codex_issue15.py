"""Issue 15 evidence separates product probes from owner-observed model turns.

The instrument never runs a native provider in this suite. Accelerated Stop
windows and a fake queue exercise product behavior; they cannot measure a day
of native Codex idling or establish that a queued wake executed a model turn.
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

try:
    from scripts import measure_codex_issue15 as measurement
except ImportError:
    measurement = None
from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/codex-issue15-product-probe-2026-10-26.json"
OWNER_CLOCK_FIXTURE = ROOT / "tests/fixtures/codex-issue15-owner-clock-2026-10-31.json"
SHORT_CONFIGURATION = {"hook_timeout_seconds": 400, "wait_deadline_seconds": 300}


class CodexIssue15Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if measurement is None:
            raise AssertionError("Issue 15 needs its scratch-only measurement instrument")
        cls.caller_home = os.environ.get("HOME")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no native provider")):
            cls.product_probe = measurement.collect_product_probe()
        cls.home_after_probe = os.environ.get("HOME")

    def setUp(self):
        self.assertIsNotNone(measurement,
            "Issue 15 needs a scratch-only instrument that records native evidence separately from accelerated product probes")
        self.receipt = copy.deepcopy(self.product_probe)
        probe = mock.patch.object(measurement, "collect_product_probe",
            side_effect=lambda: copy.deepcopy(self.receipt))
        probe.start()
        self.addCleanup(probe.stop)

    def native_trace(self, receipt, *, seconds=86400, turns=True, configuration=None):
        if configuration is not None:
            origin = datetime(2026, 10, 26, tzinfo=timezone.utc)
            events = []
            def event(kind, offset, **values):
                events.append(dict(type=kind, at=(origin + timedelta(seconds=offset)).isoformat(),
                    session_id="short-native-session", scratch_root=receipt["root"],
                    scratch_home=receipt["home"], scratch_configuration=dict(configuration), **values))
            event("observation_started", 0)
            event("stop_started", 0, invocation_id="short-stop-1")
            if turns:
                for ordinal in range(1, 4):
                    event("stop_exhausted", ordinal * 302 - 2, invocation_id="short-stop-" + str(ordinal))
                    event("model_turn_started", ordinal * 302 - 1, turn_id="native-turn-" + str(ordinal))
                    event("stop_started", ordinal * 302, invocation_id="short-stop-" + str(ordinal + 1))
            event("observation_ended", seconds)
            return {"source": "instrumented_codex_stop_and_native_turns", "events": events,
                "scratch_configuration": dict(configuration),
                "observer_window": {"source": "owned_native_child_observer", "elapsed_monotonic_seconds": seconds}}
        events = [
            {"type": "observation_started", "at": "2026-10-25T00:00:00Z"},
            {"type": "stop_started", "at": "2026-10-25T00:00:00Z", "invocation_id": "stop-1"},
        ]
        if turns:
            events.extend([
                {"type": "stop_exhausted", "at": "2026-10-25T23:58:20Z", "invocation_id": "stop-1"},
                {"type": "model_turn_started", "at": "2026-10-25T23:58:21Z", "turn_id": "turn-after-deadline"},
                {"type": "stop_started", "at": "2026-10-25T23:58:22Z", "invocation_id": "stop-2"},
            ])
        events.append({"type": "observation_ended", "at":
                       "2026-10-26T00:00:00Z" if seconds == 86400 else "2026-10-25T00:01:00Z"})
        for row in events:
            row.update(session_id="owner-scratch-session", scratch_root=receipt["root"],
                       scratch_home=receipt["home"])
        return {"source": "instrumented_codex_stop_and_native_turns", "events": events}

    def test_product_probe_exercises_two_default_windows_and_replays_native_receipts(self):
        receipt = self.receipt
        self.assertEqual("accelerated_product_probe", receipt["observation_kind"])
        self.assertEqual(86400, receipt["defaults"]["hook_timeout_seconds"])
        self.assertEqual(86300, receipt["defaults"]["wait_deadline_seconds"])
        windows = receipt["stop_windows"]
        self.assertEqual(2, len(windows))
        self.assertEqual([86300, 86300], [row["elapsed_clock_seconds"] for row in windows])
        self.assertEqual(["block", "block"], [row["decision"]["decision"] for row in windows])
        self.assertTrue(all("end this turn to re-arm" in row["decision"]["reason"] for row in windows))
        self.assertEqual([86300, 86300], [row["waited_seconds"] for row in receipt["exhaustion_receipts"]])
        self.assertEqual(2, len({row["id"] for row in receipt["exhaustion_receipts"]}))
        self.assertEqual("new_stop_invocation_has_a_fresh_window",
                         measurement.classify_receipt(receipt)["product_rearm"])

    def test_fake_daemon_does_not_spend_model_turns_while_idle_and_queues_only_for_mail(self):
        receipt = self.receipt
        daemon = receipt["daemon"]
        self.assertEqual("fake_queue_adapter", daemon["adapter_kind"])
        self.assertGreaterEqual(len(daemon["idle_cycles"]), 2)
        self.assertEqual({"idle"}, {row["state"] for row in daemon["idle_cycles"]})
        self.assertEqual(0, daemon["calls_before_mail"])
        self.assertEqual(1, len(daemon["adapter_calls"]))
        self.assertEqual("queued", daemon["mail_cycle"]["state"])
        self.assertEqual(["queued"], [row["outcome"] for row in daemon["wake_receipts"]])
        classification = measurement.classify_receipt(receipt)
        self.assertEqual("no_request_without_mail_queued_for_mail", classification["daemon"])
        self.assertIsNone(classification["model_turns_per_day"]["value"])
        self.assertEqual("unknown", classification["model_turns_per_day"]["basis"])
        self.assertEqual("unknown", classification["native_rearm_needs_turn"])

    def test_probe_owns_a_scratch_home_root_and_cleans_them_without_touching_callers_home(self):
        previous = self.caller_home
        receipt = self.receipt
        self.assertEqual(previous, self.home_after_probe)
        scratch = Path(receipt["scratch"])
        self.assertFalse(scratch.exists())
        self.assertTrue(receipt["scratch_removed"])
        self.assertNotEqual(previous, receipt["home"])
        self.assertIn(scratch, Path(receipt["home"]).parents)
        self.assertIn(scratch, Path(receipt["root"]).parents)
        self.assertIn(scratch, Path(receipt["workspace"]).parents)

    def test_deadline_arithmetic_is_explicitly_derived_and_never_a_measured_turn_rate(self):
        result = measurement.classify_receipt(self.receipt)
        rate = result["derived_stop_windows_per_day"]
        self.assertEqual("derived", rate["basis"])
        self.assertAlmostEqual(86400 / 86300, rate["value"])
        self.assertIn("86300", rate["formula"])
        self.assertEqual("unknown", result["model_turns_per_day"]["basis"])

    def test_owner_observations_measure_only_the_named_idle_interval(self):
        receipt = self.receipt
        receipt["native_trace"] = self.native_trace(receipt)
        result = measurement.classify_receipt(receipt)
        self.assertEqual({"basis": "measured", "value": 1.0,
                          "observed_turns": 1, "observed_seconds": 86400},
                         result["model_turns_per_day"])
        self.assertEqual("observed_with_intervening_turn", result["native_rearm_needs_turn"])

    def test_short_owner_interval_is_an_extrapolation_even_with_zero_observed_turns(self):
        receipt = self.receipt
        receipt["native_trace"] = self.native_trace(receipt, seconds=60, turns=False)
        result = measurement.classify_receipt(receipt)
        self.assertEqual("derived", result["model_turns_per_day"]["basis"])
        self.assertEqual(0, result["model_turns_per_day"]["value"])
        self.assertEqual("unknown", result["native_rearm_needs_turn"])

    def test_absent_trace_or_foreign_scratch_cannot_upgrade_fake_evidence_to_native(self):
        receipt = self.receipt
        self.assertEqual("native_trace_source_unavailable",
                         measurement.classify_receipt(receipt)["native_measurement_reason"])
        receipt["native_trace"] = self.native_trace(receipt)
        receipt["native_trace"]["events"][2]["scratch_home"] = "/foreign-home"
        result = measurement.classify_receipt(receipt)
        self.assertEqual("unknown", result["model_turns_per_day"]["basis"])
        self.assertEqual("unknown", result["native_rearm_needs_turn"])

    def test_duplicate_turn_ids_and_repeated_stop_ids_do_not_prove_native_rearm(self):
        receipt = self.receipt
        receipt["native_trace"] = self.native_trace(receipt)
        events = receipt["native_trace"]["events"]
        events.insert(4, dict(events[3]))
        events[-2]["invocation_id"] = "stop-1"
        result = measurement.classify_receipt(receipt)
        self.assertEqual("unknown", result["model_turns_per_day"]["basis"])
        self.assertEqual("unknown", result["native_rearm_needs_turn"])

    def test_queue_acceptance_and_assertion_only_testimony_cannot_prove_a_model_turn(self):
        receipt = self.receipt
        receipt["native_observation"] = {"hook_retrusted_by_owner": True,
                                         "idle_seconds": 86400, "model_turns": 0}
        result = measurement.classify_receipt(receipt)
        self.assertEqual("unknown", result["model_turns_per_day"]["basis"])
        self.assertEqual("unknown", result["native_rearm_needs_turn"])

    def test_recorded_fixture_replays_without_a_native_cli_and_retains_unknowns(self):
        receipt = json.loads(FIXTURE.read_text())
        with mock.patch("subprocess.Popen", side_effect=AssertionError("replay must not launch")):
            self.assertEqual(receipt["classification"], measurement.classify_receipt(receipt))
        self.assertEqual("accelerated_product_probe", receipt["observation_kind"])
        self.assertEqual("unknown", receipt["classification"]["native_rearm_needs_turn"])

    def test_owner_clock_difference_preserves_measured_rearms_and_rejects_foreign_windows(self):
        original = json.loads(OWNER_CLOCK_FIXTURE.read_text())
        for shift, turns, verdict in (
                (0, False, "observed_without_intervening_turn"),
                (0, True, "observed_with_intervening_turn"),
                (-30, False, "unknown"),
                (30, False, "unknown")):
            with self.subTest(monotonic_shift_seconds=shift, intervening_turn=turns):
                receipt = copy.deepcopy(original)
                trace = receipt["native_trace"]
                trace["observer_window"]["elapsed_monotonic_seconds"] += shift
                if turns:
                    # Put a native turn in every exhaustion/rearm gap, so the
                    # control does not rely on which pair the reducer reports.
                    events = trace["events"]
                    for index in range(len(events) - 2, 0, -1):
                        if events[index]["type"] == "stop_exhausted":
                            event = dict(events[index])
                            del event["invocation_id"]
                            event.update(type="model_turn_started", turn_id="fixture-turn-" + str(index),
                                at=(datetime.fromisoformat(event["at"]) + timedelta(seconds=1)).isoformat())
                            events.insert(index + 1, event)
                result = measurement.classify_receipt(receipt)
                self.assertEqual(verdict, result["native_rearm_needs_turn"])
                self.assertEqual("unknown" if shift else "measured", result["native_rearm_needs_turn_basis"])
                if not shift:
                    self.assertEqual(SHORT_CONFIGURATION, result["native_rearm_configuration"])
                    self.assertEqual("derived", result["model_turns_per_day"]["basis"])
                    self.assertEqual(11 if turns else 0, result["model_turns_per_day"]["observed_turns"])
                    self.assertAlmostEqual(3600.174646, result["model_turns_per_day"]["observed_seconds"])
                else:
                    self.assertEqual({"basis": "unknown", "value": None}, result["model_turns_per_day"])

    def test_missing_or_wrong_exhaustion_receipt_cannot_prove_two_rearmed_windows(self):
        receipt = json.loads(FIXTURE.read_text())
        for mutation in ([], [receipt["exhaustion_receipts"][0]] * 2):
            with self.subTest(receipts=mutation):
                damaged = copy.deepcopy(receipt)
                damaged["exhaustion_receipts"] = mutation
                self.assertEqual("cannot-measure:product_rearm_unproved",
                                 measurement.classify_receipt(damaged)["product_rearm"])

    def test_one_capture_command_writes_replayable_evidence_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            output = Path(directory) / "receipt.json"
            stdout = io.StringIO()
            with mock.patch("sys.argv", ["measure_codex_issue15.py", "capture", "--output", str(output)]),\
                    redirect_stdout(stdout):
                measurement.main()
            receipt = json.loads(output.read_text())
            self.assertEqual(receipt["classification"], measurement.classify_receipt(receipt))
            before = output.read_bytes()
            with self.assertRaises(FileExistsError):
                measurement.capture(output)
            self.assertEqual(before, output.read_bytes())

    def test_prepared_stop_wrapper_produces_timestamped_events_only_in_its_scratch_home(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            bank = Path(temporary).resolve() / "bank"
            metadata = measurement.prepare(bank)
            hooks = json.loads((Path(metadata["home"]) / ".codex/hooks.json").read_text())
            self.assertEqual(86400, hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"])
            # Codex 0.164.0's command runner uses ProcessMode::NewSession.
            # A native Stop therefore does not share its parent's group.
            (bank / "owner-process-group.json").write_text(json.dumps({"process_group": os.getpgrp() + 1}))
            payload = {"cwd": metadata["workspace"], "session_id": "native-scratch",
                       "hook_event_name": "Stop", "transcript_path": None,
                       "turn_id": "turn-scratch", "model": "fixture",
                       "permission_mode": "default", "stop_hook_active": False,
                       "last_assistant_message": "Done", "future_extension": {"enabled": True}}
            def waiter(**kwargs):
                kwargs["stdout"].write(json.dumps({"decision": "block", "reason": "end this turn to re-arm"}))
                return 0
            with mock.patch.dict(os.environ, {"HOME": metadata["home"]}),\
                    mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                    mock.patch.object(measurement, "run_stop_waiter", side_effect=waiter),\
                    redirect_stdout(io.StringIO()):
                self.assertEqual(0, measurement.hook(bank))
            rows = [json.loads(line) for line in (bank / "stop-trace.jsonl").read_text().splitlines()]
            self.assertEqual(["stop_started", "stop_exhausted"], [row["type"] for row in rows])
            self.assertEqual({"native-scratch"}, {row["session_id"] for row in rows})
            self.assertTrue(all(row["at"] and row["scratch_home"] == metadata["home"] for row in rows))
            with mock.patch.dict(os.environ, {"HOME": str(bank.parent)}),\
                    mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
                with self.assertRaises(ValueError):
                    measurement.hook(bank)

    def test_observer_shutdown_releases_a_detached_stop_and_fences_later_hooks(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading

        settle = getattr(measurement, "settle_native_hooks", None)
        self.assertTrue(callable(settle), "observer cleanup must release separately grouped native hooks")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            bank = Path(temporary).resolve() / "bank"
            metadata = measurement.prepare(bank, **SHORT_CONFIGURATION)
            (bank / "owner-process-group.json").write_text(json.dumps({"process_group": os.getpgrp() + 1}))
            payload = {"cwd": metadata["workspace"], "session_id": "detached-scratch",
                       "hook_event_name": "Stop", "stop_hook_active": False, "new_field": None}
            entered = threading.Event()
            real_waiter = measurement.run_stop_waiter
            def waiter(**kwargs):
                entered.set()
                return real_waiter(**kwargs)
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": metadata["home"]}),\
                    mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                    mock.patch.object(measurement, "run_stop_waiter", side_effect=waiter),\
                    redirect_stdout(output), ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(measurement.hook, bank)
                self.assertTrue(entered.wait(5), "scratch hook did not enter its waiter")
                try:
                    self.assertTrue(settle(metadata)["owned_hooks_quiescent"])
                    self.assertEqual(0, future.result(timeout=5))
                finally:
                    # Keep a failing regression from parking a test worker.
                    from floati.wake_control import WakeController
                    controller = WakeController(FloatiRoot.open_direct_home(Path(metadata["root"])))
                    if controller.status(measurement.NODE, payload["session_id"])["state"] != "paused":
                        controller.pause(measurement.NODE, payload["session_id"], idempotency_key="test-finally-pause")
            before = (bank / "stop-trace.jsonl").read_bytes()
            with mock.patch.dict(os.environ, {"HOME": metadata["home"]}),\
                    mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()) as later:
                self.assertEqual(0, measurement.hook(bank))
            self.assertEqual({}, json.loads(later.getvalue()))
            self.assertEqual(before, (bank / "stop-trace.jsonl").read_bytes())

    def test_failed_stop_retains_stderr_and_tracebacks_without_changing_its_result(self):
        for failure in ("returned", "raised", "invalid_payload"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
                bank = Path(temporary).resolve() / "bank"
                metadata = measurement.prepare(bank, **SHORT_CONFIGURATION)
                payload = {"cwd": metadata["workspace"], "session_id": "failed-scratch"}
                if failure == "invalid_payload":
                    payload["cwd"] = str(bank.parent)
                def waiter(**kwargs):
                    kwargs["stderr"].write("scratch waiter diagnostic\n")
                    if failure == "raised":
                        raise RuntimeError("scratch waiter exception")
                    kwargs["stdout"].write("{}\n")
                    return 1
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, {"HOME": metadata["home"]}),\
                        mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                        mock.patch.object(measurement, "run_stop_waiter", side_effect=waiter),\
                        redirect_stdout(output), redirect_stderr(errors):
                    if failure == "returned":
                        self.assertEqual(1, measurement.hook(bank))
                        self.assertEqual("{}\n", output.getvalue())
                    else:
                        with self.assertRaises(RuntimeError if failure == "raised" else ValueError):
                            measurement.hook(bank)
                        self.assertEqual("", output.getvalue())
                diagnostics = list((bank / "hook-stderr").glob("*.log"))
                self.assertEqual(1, len(diagnostics), "every failed hook needs retained stderr")
                retained = diagnostics[0].read_text()
                self.assertEqual(retained, errors.getvalue())
                self.assertEqual(0o600, diagnostics[0].stat().st_mode & 0o777)
                if failure == "invalid_payload":
                    self.assertIn("ValueError", retained)
                else:
                    self.assertIn("scratch waiter diagnostic", retained)
                    if failure == "raised":
                        self.assertIn("RuntimeError: scratch waiter exception", retained)

    def test_native_import_preserves_stop_identity_and_bounds_time_by_export_coverage(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            bank = Path(temporary).resolve() / "bank"
            metadata = measurement.prepare(bank)
            coordinate = {"session_id": "native-scratch", "scratch_root": metadata["root"],
                          "scratch_home": metadata["home"]}
            stops = [dict(coordinate, type="stop_started", invocation_id="s1", at="2026-10-25T00:00:00Z")]
            stop_path = bank / "stop-trace.jsonl"
            stop_path.write_text("\n".join(json.dumps(row) for row in stops) + "\n")
            export = Path(metadata["home"]) / "explicit-native-export.jsonl"
            native = [
                {"type": "session_meta", "timestamp": "2026-10-24T23:59:00Z", "payload": {"id": "native-scratch"}},
                {"type": "event_msg", "timestamp": "2026-10-24T23:59:30Z", "payload": {"type": "task_started", "turn_id": "seed"}},
                {"type": "event_msg", "timestamp": "2026-10-25T00:01:00Z", "payload": {"type": "task_complete", "turn_id": "seed"}},
            ]
            export.write_text("\n".join(json.dumps(row) for row in native) + "\n")
            trace = measurement.read_native_trace(bank, export)
            self.assertEqual("2026-10-25T00:01:00+00:00", trace["events"][-1]["at"])
            reduced = measurement.classify_receipt(dict(metadata, native_trace=trace))
            self.assertEqual(60, reduced["model_turns_per_day"]["observed_seconds"])
            self.assertEqual("derived", reduced["model_turns_per_day"]["basis"])
            stops[0]["scratch_home"] = "/foreign-home"
            stop_path.write_text(json.dumps(stops[0]) + "\n")
            self.assertIsNone(measurement.read_native_trace(bank, export))
            stops[0]["scratch_home"] = metadata["home"]
            stop_path.write_text(json.dumps(stops[0]) + "\n")
            native[1]["payload"]["type"] = "unsupported_native_turn_shape"
            export.write_text("\n".join(json.dumps(row) for row in native) + "\n")
            self.assertIsNone(measurement.read_native_trace(bank, export))

    def test_documented_private_tmp_bank_prefix_is_accepted_without_creation(self):
        self.assertEqual(Path("\x2fprivate/tmp/floati-issue15-owner-20261026"),
                         measurement._bank(Path("\x2fprivate/tmp/floati-issue15-owner-20261026")))

    def test_owner_observer_owns_native_child_and_runs_each_mode_against_fake_transport(self):
        from floati.codex_wait_contract import resolve_participant
        from floati.wake_daemon_adapters import record_codex_daemon_binding

        for mode in ("stop", "daemon"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
                bank = Path(temporary).resolve() / "bank"
                output, fixture = bank.parent / "owner.json", bank.parent / "replay.json"
                ticks = [0.0]
                epoch = 1792886400.0
                child = mock.Mock()
                child.returncode = None
                child.poll.side_effect = lambda: child.returncode
                def terminate():
                    child.returncode = -15
                child.terminate.side_effect = terminate
                child.wait.side_effect = lambda **kwargs: child.returncode
                def cleanup(process):
                    process.terminate()
                    process.wait(timeout=1)
                    return {"owned_child_reaped": True, "owned_process_group": 424242,
                            "owned_group_quiescent": True}
                def launch(metadata, environment):
                    self.assertEqual(metadata["home"], environment["HOME"])
                    self.assertEqual(str(Path(metadata["home"]) / ".codex"), environment["CODEX_HOME"])
                    self.assertNotIn("OPENAI_API_KEY", environment)
                    self.assertNotIn("OPENAI_BASE_URL", environment)
                    self.assertNotIn("XDG_CONFIG_HOME", environment)
                    self.assertNotIn("HTTP_PROXY", environment)
                    self.assertEqual(str(bank / "tmp"), environment["TMPDIR"])
                    executable = bank / "fake-codex"
                    executable.write_text("#!/bin/sh\nexit 0\n")
                    executable.chmod(0o700)
                    participant = resolve_participant(Path(metadata["root"]), Path(metadata["workspace"]))
                    record_codex_daemon_binding(participant, "native-scratch")
                    origin = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                    stop = {"type": "stop_started", "at": origin, "invocation_id": "native-stop",
                            "session_id": "native-scratch", "scratch_root": metadata["root"], "scratch_home": metadata["home"]}
                    (bank / "stop-trace.jsonl").write_text(json.dumps(stop) + "\n")
                    native = Path(metadata["home"]) / ".codex/sessions/owned.jsonl"
                    native.parent.mkdir()
                    rows = [
                        {"type": "session_meta", "timestamp": origin, "payload": {"id": "native-scratch"}},
                        {"type": "event_msg", "timestamp": datetime.fromtimestamp(epoch - 1, timezone.utc).isoformat(),
                         "payload": {"type": "task_started", "turn_id": "seed"}},
                        {"type": "event_msg", "timestamp": datetime.fromtimestamp(epoch + 10, timezone.utc).isoformat(),
                         "payload": {"type": "task_complete", "turn_id": "seed"}},
                    ]
                    native.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                    return child
                with mock.patch.object(measurement, "_launch_codex", side_effect=launch),\
                        mock.patch.object(measurement, "_cleanup_native_child", side_effect=cleanup),\
                        mock.patch("floati.wake_daemon_adapters.CODEX_EXECUTABLE", bank / "fake-codex"),\
                        mock.patch.dict(os.environ, {"OPENAI_API_KEY": "fake-not-a-real-key",
                            "OPENAI_BASE_URL": "https://foreign.invalid", "XDG_CONFIG_HOME": "/foreign-config",
                            "HTTP_PROXY": "http://foreign.invalid"}),\
                        mock.patch("subprocess.Popen", side_effect=AssertionError("fake transport only")):
                    receipt = measurement.observe(bank, output, fixture=fixture, duration_seconds=10, mode=mode,
                        clock=lambda: ticks[0], wall_time=lambda: epoch + ticks[0],
                        sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
                observer = receipt["native_observer"]
                self.assertEqual("observed", observer["state"])
                self.assertTrue(observer["owned_child_reaped"])
                self.assertTrue(observer["owned_group_quiescent"])
                self.assertEqual("native-scratch", observer["session_id"])
                self.assertEqual(10, observer["observation_window"]["elapsed_monotonic_seconds"])
                self.assertEqual([], observer["queue_receipts"])
                self.assertEqual("derived", receipt["classification"]["model_turns_per_day"]["basis"])
                if mode == "daemon":
                    self.assertTrue(observer["cycles"])
                    self.assertEqual({"idle"}, {row["artifact"]["state"] for row in observer["cycles"]})
                else:
                    self.assertEqual([], observer["cycles"])
                self.assertEqual(json.loads(output.read_text()), json.loads(fixture.read_text()))
                self.assertTrue(output.with_suffix(".md").is_file())
                child.terminate.assert_called_once()
                child.wait.assert_called_once()

    def test_cleanup_quiesces_the_owned_group_after_its_leader_has_already_exited(self):
        process = mock.Mock()
        process.pid = 424242
        process.returncode = 0
        process._issue15_owned_group = 424242
        process.poll.return_value = 0
        with mock.patch.object(measurement.WorkerRunner, "_terminate_process_group") as quiesce,\
                mock.patch.object(measurement.WorkerRunner, "_process_group_alive", return_value=False),\
                mock.patch.object(measurement.os, "getpgrp", return_value=123):
            result = measurement._cleanup_native_child(process)
        quiesce.assert_called_once_with(424242)
        self.assertTrue(result["owned_child_reaped"])
        self.assertTrue(result["owned_group_quiescent"])
        process.wait.assert_called_once()

    def test_wall_clock_jump_cannot_promote_a_short_native_observation_to_a_measured_day(self):
        receipt = self.receipt
        receipt["native_trace"] = self.native_trace(receipt)
        receipt["native_trace"]["observer_window"] = {
            "source": "owned_native_child_observer", "elapsed_monotonic_seconds": 60}
        result = measurement.classify_receipt(receipt)
        self.assertEqual("unknown", result["model_turns_per_day"]["basis"])
        self.assertIsNone(result["model_turns_per_day"]["value"])

    def test_observe_cli_returns_cannot_measure_status_after_an_unproved_native_observation(self):
        observation = {"classification": {"model_turns_per_day": {"basis": "unknown", "value": None}},
                       "native_observer": {"state": "cannot-measure", "reason": "native_trace_source_unavailable"}}
        with mock.patch("sys.argv", ["measure_codex_issue15.py", "observe", "--bank", "\x2fprivate/tmp/unused-bank",
                                      "--output", "\x2fprivate/tmp/unused-output.json"]),\
                mock.patch.object(measurement, "observe", return_value=observation), redirect_stdout(io.StringIO()):
            self.assertEqual(20, measurement.main())

    def test_exit_or_takeover_during_the_final_sleep_cannot_count_as_a_full_native_interval(self):
        from floati.codex_wait_contract import resolve_participant
        from floati.wake_daemon_adapters import record_codex_daemon_binding

        for failure in ("exit", "takeover"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
                bank = Path(temporary).resolve() / "bank"
                ticks, participant = [0.0], [None]
                child = mock.Mock()
                child.returncode = None
                child.poll.side_effect = lambda: child.returncode
                def launch(metadata, environment):
                    executable = bank / "fake-codex"
                    executable.write_text("#!/bin/sh\nexit 0\n")
                    executable.chmod(0o700)
                    participant[0] = resolve_participant(Path(metadata["root"]), Path(metadata["workspace"]))
                    record_codex_daemon_binding(participant[0], "native-scratch")
                    (bank / "stop-trace.jsonl").write_text("{}\n")
                    return child
                def sleep(seconds):
                    ticks[0] += seconds
                    if failure == "exit":
                        child.returncode = 0
                    else:
                        record_codex_daemon_binding(participant[0], "replacement-session")
                def cleanup(process):
                    process.returncode = process.returncode if process.returncode is not None else -15
                    return {"owned_child_reaped": True, "owned_process_group": 424242,
                            "owned_group_quiescent": True}
                with mock.patch.object(measurement, "_launch_codex", side_effect=launch),\
                        mock.patch.object(measurement, "_cleanup_native_child", side_effect=cleanup),\
                        mock.patch("floati.wake_daemon_adapters.CODEX_EXECUTABLE", bank / "fake-codex"),\
                        mock.patch("subprocess.Popen", side_effect=AssertionError("fake transport only")):
                    receipt = measurement.observe(bank, bank.parent / "owner.json", duration_seconds=10,
                        clock=lambda: ticks[0], wall_time=lambda: 1792886400.0 + ticks[0], sleep=sleep)
                self.assertEqual("cannot-measure", receipt["native_observer"]["state"])
                self.assertEqual("native_child_exited_during_observation" if failure == "exit" else
                                 "native_session_changed_during_observation", receipt["native_observer"]["reason"])
                self.assertEqual("unknown", receipt["classification"]["model_turns_per_day"]["basis"])

    def short_receipt(self, *, seconds=3600, turns=True):
        receipt = copy.deepcopy(self.receipt)
        receipt["scratch_configuration"] = dict(SHORT_CONFIGURATION)
        receipt["native_trace"] = self.native_trace(receipt, seconds=seconds,
            turns=turns, configuration=SHORT_CONFIGURATION)
        return receipt

    def test_short_native_rearms_are_measured_for_the_exact_scratch_configuration(self):
        result = measurement.classify_receipt(self.short_receipt())
        self.assertEqual("observed_with_intervening_turn", result["native_rearm_needs_turn"])
        self.assertEqual("measured", result.get("native_rearm_needs_turn_basis"),
                         "Correlated native re-arms need their own measured basis, independent of daily extrapolation")
        self.assertEqual(SHORT_CONFIGURATION, result.get("native_rearm_configuration"))
        self.assertEqual("derived", result["model_turns_per_day"]["basis"])
        self.assertEqual(72, result["model_turns_per_day"]["value"])

    def test_short_configuration_daily_rate_is_always_derived_even_after_a_day(self):
        result = measurement.classify_receipt(self.short_receipt(seconds=86400))
        self.assertEqual("derived", result["model_turns_per_day"]["basis"],
                         "A shortened-deadline arm cannot stand in for production daily behavior")

    def test_stripping_the_receipt_configuration_cannot_promote_a_short_arm_to_a_measured_day(self):
        receipt = self.short_receipt(seconds=86400)
        del receipt["scratch_configuration"]
        result = measurement.classify_receipt(receipt)
        self.assertEqual("unknown", result["model_turns_per_day"]["basis"])
        self.assertEqual("unknown", result["native_rearm_needs_turn_basis"])
        self.assertIsNone(result["native_rearm_configuration"])

    def test_missing_rearm_stays_unknown_and_configuration_tampering_is_not_native_evidence(self):
        result = measurement.classify_receipt(self.short_receipt(turns=False))
        self.assertEqual("unknown", result.get("native_rearm_needs_turn_basis"))
        receipt = self.short_receipt()
        for mutation in ("event", "trace"):
            with self.subTest(mutation=mutation):
                damaged = copy.deepcopy(receipt)
                target = damaged["native_trace"]["events"][2] if mutation == "event" else damaged["native_trace"]
                target["scratch_configuration"]["wait_deadline_seconds"] = 299
                result = measurement.classify_receipt(damaged)
                self.assertEqual("unknown", result["model_turns_per_day"]["basis"],
                                 "Metadata and producer configuration must match before native classification")

    def test_scratch_preparation_sets_both_hook_and_waiter_without_changing_defaults(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            bank = Path(temporary).resolve() / "short"
            metadata = measurement.prepare(bank, **SHORT_CONFIGURATION)
            self.assertEqual(SHORT_CONFIGURATION, metadata["scratch_configuration"])
            hooks = json.loads((Path(metadata["home"]) / ".codex/hooks.json").read_text())
            self.assertEqual(400, hooks["hooks"]["Stop"][0]["hooks"][0]["timeout"])
            participant = resolve_participant(Path(metadata["root"]), Path(metadata["workspace"]))
            consent = CodexWaitConsentLedger(FloatiRoot.open_direct_home(Path(metadata["root"]))).require_armed(participant.binding)
            self.assertEqual(SHORT_CONFIGURATION, {key: consent[key] for key in SHORT_CONFIGURATION})

    def test_two_banks_share_no_scratch_state_and_each_hook_records_its_own_configuration(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            parent = Path(temporary).resolve()
            first = measurement.prepare(parent / "first", **SHORT_CONFIGURATION)
            second = measurement.prepare(parent / "second")
            for key in ("home", "root", "workspace", "hook_trace"):
                self.assertNotEqual(first[key], second[key])
            untouched = (parent / "second/bank.json").read_bytes()
            payload = {"cwd": first["workspace"], "session_id": "only-first"}
            def waiter(**kwargs):
                kwargs["stdout"].write(json.dumps({"decision": "block", "reason": "end this turn to re-arm"}))
                return 0
            with mock.patch.dict(os.environ, {"HOME": first["home"]}),\
                    mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                    mock.patch.object(measurement, "run_stop_waiter", side_effect=waiter), redirect_stdout(io.StringIO()):
                self.assertEqual(0, measurement.hook(parent / "first"))
            rows = [json.loads(line) for line in Path(first["hook_trace"]).read_text().splitlines()]
            self.assertTrue(all(row.get("scratch_configuration") == SHORT_CONFIGURATION for row in rows))
            self.assertFalse(Path(second["hook_trace"]).exists())
            self.assertEqual(untouched, (parent / "second/bank.json").read_bytes())

    def test_observe_cli_passes_the_short_configuration_to_the_owned_observer(self):
        observation = {"classification": {}, "native_observer": {"state": "observed"}}
        argv = ["measure_codex_issue15.py", "observe", "--bank", "\x2fprivate/tmp/unused-short-bank",
                "--output", "\x2fprivate/tmp/unused-short.json", "--duration-seconds", "3600",
                "--wait-deadline-seconds", "300", "--hook-timeout-seconds", "400"]
        with mock.patch("sys.argv", argv), mock.patch.object(measurement, "observe", return_value=observation) as observer,\
                redirect_stdout(io.StringIO()):
            try:
                result = measurement.main()
            except SystemExit as exc:
                self.fail("The owner CLI must accept explicit scratch window options: " + str(exc))
        self.assertEqual(0, result)
        self.assertEqual(300, observer.call_args.kwargs["wait_deadline_seconds"])
        self.assertEqual(400, observer.call_args.kwargs["hook_timeout_seconds"])
        self.assertEqual(3600, observer.call_args.kwargs["duration_seconds"])

    def test_native_import_binds_producer_configuration_to_real_scratch_consent(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            bank = Path(temporary).resolve() / "short"
            metadata = measurement.prepare(bank, **SHORT_CONFIGURATION)
            receipt = self.short_receipt()
            events = receipt["native_trace"]["events"]
            stops = [dict(row, scratch_home=metadata["home"], scratch_root=metadata["root"])
                     for row in events if row["type"] in {"stop_started", "stop_exhausted"}]
            Path(metadata["hook_trace"]).write_text("\n".join(json.dumps(row) for row in stops) + "\n")
            native_log = Path(metadata["home"]) / "native-export.jsonl"
            native_rows = [{"type": "session_meta", "timestamp": events[0]["at"],
                            "payload": {"id": "short-native-session"}}]
            native_rows += [{"type": "event_msg", "timestamp": row["at"],
                             "payload": {"type": "task_started", "turn_id": row["turn_id"]}}
                            for row in events if row["type"] == "model_turn_started"]
            native_log.write_text("\n".join(json.dumps(row) for row in native_rows) + "\n")
            trace = measurement.read_native_trace(bank, native_log)
            self.assertEqual(SHORT_CONFIGURATION, trace.get("scratch_configuration"))
            self.assertTrue(all(row.get("scratch_configuration") == SHORT_CONFIGURATION for row in trace["events"]))
            stops[0]["scratch_configuration"] = {"hook_timeout_seconds": 86400, "wait_deadline_seconds": 86300}
            Path(metadata["hook_trace"]).write_text("\n".join(json.dumps(row) for row in stops) + "\n")
            self.assertIsNone(measurement.read_native_trace(bank, native_log))
            damaged = json.loads((bank / "bank.json").read_text())
            damaged["scratch_configuration"]["wait_deadline_seconds"] = 299
            (bank / "bank.json").write_text(json.dumps(damaged))
            with self.assertRaisesRegex(ValueError, "actual scratch waiter consent"):
                measurement.read_native_trace(bank, native_log)

