"""A failed automatic carry must retain the original selected-byte read witness."""
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from floati import bus_epoch, codex_wait, epoch_carry, jsonl
from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant, write_workspace_map
from floati.epoch_rotation import stop_maintenance
from floati.errors import IntegrityFailure
from floati.events import EVENT_KINDS, EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_lock_retry import waiter_lock_window
from tests.temp_roots import REAL_TEMP_ROOT
from tests.waiter_timing import scripted_readiness


def measured(path):
    status = path.stat()
    return (status.st_dev, status.st_ino, status.st_mode, status.st_size,
            status.st_mtime_ns, status.st_ctime_ns, status.st_nlink)


class EpochSelectedFailureWitnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        EventLog(self.root).send("sender", "recipient", "floati", "a" * 40,
            "AGENTS.md", "valid old native prefix", idempotency_key="old-prefix",
            now=datetime.now(timezone.utc) - timedelta(days=8))
        self.path = self.root.path / "events.jsonl"
        self.healthy = self.path.read_bytes()
        self.path.write_bytes(self.healthy + b"{")
        self.original = measured(self.path)

    def assert_failure_witness(self, failure):
        self.assertEqual("incomplete_jsonl_line", failure.code)
        self.assertEqual((self.path, self.original), getattr(failure, "waiter_input_witness", None))
        self.assertEqual({"path": "events.jsonl", "byte_offset": len(self.healthy)},
                         getattr(failure, "waiter_input_identity", None))
        self.assertEqual([], list(self.root.path.glob("archive-*")))
        self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(self.root)["classification"])

    def test_later_bad_frame_keeps_original_selected_read_witness(self):
        with self.assertRaises(IntegrityFailure) as caught:
            stop_maintenance(self.root, actor="recipient", check_authority=lambda: None)
        self.assert_failure_witness(caught.exception)
        self.assertEqual(self.healthy + b"{", self.path.read_bytes())
        self.assertEqual(self.original, measured(self.path))

    def test_later_framing_error_offsets_name_the_actual_bad_frame(self):
        for tail, code in ((b"\n", "blank_jsonl_line"), (b"{]\n", "malformed_json")):
            with self.subTest(code=code):
                self.path.write_bytes(self.healthy + tail)
                original = measured(self.path)
                with self.assertRaises(IntegrityFailure) as caught:
                    stop_maintenance(self.root, actor="recipient", check_authority=lambda: None)
                self.assertEqual(code, caught.exception.code)
                self.assertEqual((self.path, original), caught.exception.waiter_input_witness)
                self.assertEqual({"path": "events.jsonl", "byte_offset": len(self.healthy)},
                                 caught.exception.waiter_input_identity)
                self.assertEqual(self.healthy + tail, self.path.read_bytes())
                self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(self.root)["classification"])

    def test_repair_after_selected_read_does_not_replace_original_failure_witness(self):
        replacement = self.base / "repaired-events.jsonl"
        replacement.write_bytes(self.healthy)
        native_decode = epoch_carry._decode_path_records
        repaired = []

        def repair_then_decode(path, tenant, kinds, payload, **options):
            if path == self.path and not repaired:
                self.assertEqual(self.healthy + b"{", payload)
                os.replace(replacement, self.path)
                repaired.append(True)
            return native_decode(path, tenant, kinds, payload, **options)

        with mock.patch.object(epoch_carry, "_decode_path_records", side_effect=repair_then_decode):
            with self.assertRaises(IntegrityFailure) as caught:
                stop_maintenance(self.root, actor="recipient", check_authority=lambda: None)
        self.assertEqual([True], repaired)
        self.assert_failure_witness(caught.exception)
        self.assertNotEqual(self.original, measured(self.path))
        self.assertEqual(self.healthy, self.path.read_bytes())

    def test_unbound_payload_or_coordinate_cannot_borrow_selected_read_metadata(self):
        for case in ("copied-payload", "foreign-coordinate", "noncanonical-coordinate"):
            with self.subTest(case=case):
                def planner(root, payloads):
                    selected = next(raw for relative, _plane, raw in payloads if relative == "events.jsonl")
                    payload, path = selected, self.path
                    if case == "copied-payload":
                        payload = bytes(bytearray(selected))
                        self.assertIsNot(selected, payload)
                    elif case == "foreign-coordinate":
                        path = self.base / "foreign.jsonl"
                    else:
                        path = self.root.path / "receipts" / ".." / "events.jsonl"
                    jsonl._decode_path_records(path, root.tenant_id, frozenset(EVENT_KINDS), payload)

                with self.assertRaises(IntegrityFailure) as caught:
                    bus_epoch.roll_automatic_epoch(self.root, actor="sender", planner=planner)
                self.assertEqual("incomplete_jsonl_line", caught.exception.code)
                self.assertIsNone(getattr(caught.exception, "waiter_input_witness", None))
                self.assertEqual(self.healthy + b"{", self.path.read_bytes())
                self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(self.root)["classification"])

    def test_pure_decoder_retains_native_refusal_without_claiming_a_read(self):
        with self.assertRaises(IntegrityFailure) as caught:
            jsonl._decode_path_records(self.path, self.root.tenant_id, frozenset(EVENT_KINDS),
                                       self.healthy + b"{")
        failure = caught.exception
        self.assertEqual("incomplete_jsonl_line", failure.code)
        self.assertEqual("events.jsonl: framed input has an incomplete final record", failure.detail)
        self.assertEqual("incomplete_frame", failure.__cause__.code)
        self.assertEqual(0, failure.__cause__.line_number)
        self.assertIsNone(getattr(failure, "waiter_input_witness", None))
        self.assertIsNone(getattr(failure, "waiter_input_identity", None))

    def test_repaired_repeat_stop_exits_original_cooldown_before_full_budget(self):
        workspace = self.base / "workspace"
        workspace.mkdir()
        write_workspace_map(self.root.path, workspace, "recipient")
        participant = resolve_participant(self.root.path, workspace)
        CodexWaitConsentLedger(self.root).arm(participant.binding, hook_timeout_seconds=10,
            wait_deadline_seconds=6, idempotency_key="tail-consent")
        clock = [0.0]
        epoch = float(int(time.time()))

        def advance(seconds):
            clock[0] += seconds

        def invoke(sleep):
            result = {}
            code = codex_wait.run_stop_waiter(bus_home=self.root.path,
                hook_payload={"cwd": str(workspace), "session_id": "tail-session"},
                stdout=io.StringIO(), stderr=io.StringIO(), monotonic=lambda: clock[0],
                wall_time=lambda: epoch + clock[0], wait_readable=scripted_readiness((sleep), interval=(1.0)), sleep=sleep, _waiter_result=result)
            self.assertEqual(0, code)
            return result

        first = invoke(advance)
        self.assertEqual("integrity_failure", first["kind"])
        self.assertEqual("consumption_state_unavailable", first["code"])
        repaired = []

        def repair(seconds):
            advance(seconds)
            if not repaired:
                self.path.write_bytes(self.healthy)
                repaired.append(True)

        with waiter_lock_window(6, lambda: clock[0], repair):
            clock[0] = 2.0
            second = invoke(repair)
        self.assertEqual([True], repaired)
        self.assertEqual("integrity_failure", second["kind"])
        self.assertEqual("consumption_state_unavailable", second["code"])
        exits = jsonl.read_records_snapshot(self.root, "receipts/wake-waiter-exit/recipient.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"})
        diagnostic = exits[-1]["integrity_failure"]
        self.assertEqual("incomplete_jsonl_line", diagnostic["check_id"])
        self.assertEqual("events.jsonl", diagnostic["input_identity"]["path"])
        self.assertEqual(len(self.healthy), diagnostic["input_identity"]["byte_offset"])
        self.assertIn("input changed since the failed read", second["decision"]["reason"])
        self.assertLess(clock[0], 6.0, "repair must invalidate the old failure before its full cooldown")
        self.assertEqual(self.healthy, self.path.read_bytes())
