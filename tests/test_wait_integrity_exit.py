from __future__ import annotations

import io
import json
import time
import unittest
from unittest import mock

from floati.codex_wait import run_stop_waiter
from floati.jsonl import read_records_snapshot
from floati.records import WAKE_HOLD_KINDS
from tests import test_wake_waiter_continuity as fixtures
from tests.waiter_timing import scripted_readiness


class WaitIntegrityExitTests(unittest.TestCase):
    def setUp(self):
        fixtures.WakeWaiterContinuityTests.setUp(self)
        self.epoch = float(int(time.time()))
        default = mock.patch("floati.codex_wait.DEFAULT_WAIT_DEADLINE_SECONDS", 2)
        default.start()
        self.addCleanup(default.stop)
    fixture = fixtures.WakeWaiterContinuityTests.fixture
    rows = staticmethod(fixtures.WakeWaiterContinuityTests.rows)

    def test_repeated_tail_after_verified_prefix_keeps_the_same_offset_and_counts(self):
        from floati.events import EventLog

        root, workspace, _participant = self.fixture("verified-prefix-repeat")
        EventLog(root).send("architect", "architect", "floati", "a" * 40,
                            "docs/evidence/control.md", "control", idempotency_key="control")
        path = root.path / "events.jsonl"
        offset = path.stat().st_size
        clock = [0.0]

        def plant(seconds):
            clock[0] += seconds
            with path.open("ab") as handle:
                handle.write(b'{')

        outputs = []
        for first in (True, False):
            output = io.StringIO()
            run_stop_waiter(
                bus_home=root.path,
                hook_payload={"cwd": str(workspace), "session_id": "seat-prefix-repeat"},
                stdout=output, stderr=io.StringIO(),
                monotonic=lambda: clock[0], wall_time=lambda: self.epoch + clock[0],
                wait_readable=scripted_readiness((plant if first else lambda seconds: clock.__setitem__(0, clock[0] + seconds)), interval=(1.0)), sleep=plant if first else lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            )
            outputs.append(output.getvalue())
        diagnostics = [row["integrity_failure"] for row in self.rows(root)]
        self.assertEqual([offset, offset], [row["input_identity"]["byte_offset"] for row in diagnostics])
        self.assertEqual([1, 2], [row["occurrence_count"] for row in diagnostics])
        self.assertEqual(outputs[0], outputs[1])

    def test_malformed_consent_is_not_silently_classified_as_withdrawal(self):
        import os
        from floati.codex_wait_contract import consent_ledger_relative

        for in_flight in (False, True):
            with self.subTest(in_flight=in_flight):
                root, workspace, participant = self.fixture("consent-" + str(in_flight))
                coordinate = consent_ledger_relative(participant.binding.node_id)
                path = root.resolve_relative(coordinate)
                clock = [0.0]
                output = io.StringIO()

                def corrupt(seconds=0):
                    clock[0] += seconds
                    replacement = path.with_suffix(".replacement")
                    replacement.write_bytes(b'{"private_message_body":"MUST_NOT_APPEAR"')
                    os.replace(replacement, path)

                if not in_flight:
                    corrupt()
                run_stop_waiter(
                    bus_home=root.path,
                    hook_payload={"cwd": str(workspace), "session_id": "seat-consent-fault"},
                    stdout=output, stderr=io.StringIO(),
                    monotonic=lambda: clock[0], wall_time=lambda: self.epoch + clock[0], wait_readable=scripted_readiness((corrupt), interval=(1.0)), sleep=corrupt,
                )
                self.assertTrue(output.getvalue(), "malformed consent must surface an integrity block")
                block = json.loads(output.getvalue())
                self.assertEqual("block", block["decision"])
                self.assertNotIn("MUST_NOT_APPEAR", output.getvalue())
                receipt = self.rows(root)[0]
                self.assertEqual("integrity_failure", receipt["reason_code"])
                self.assertEqual(coordinate.as_posix(), receipt["integrity_failure"]["input_identity"]["path"])
                if in_flight:
                    repeated = io.StringIO()
                    run_stop_waiter(
                        bus_home=root.path,
                        hook_payload={"cwd": str(workspace), "session_id": "seat-consent-fault"},
                        stdout=repeated, stderr=io.StringIO(),
                        monotonic=lambda: clock[0], wall_time=lambda: self.epoch + clock[0],
                        wait_readable=scripted_readiness((lambda seconds: clock.__setitem__(0, clock[0] + seconds)), interval=(1.0)), sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                    )
                    self.assertEqual(output.getvalue(), repeated.getvalue())
                    self.assertEqual(2, self.rows(root)[1]["integrity_failure"]["occurrence_count"])

    def test_incomplete_tail_wakes_with_named_check_without_publishing_fault(self):
        root, workspace, _participant = self.fixture("incomplete-tail")
        output, error = io.StringIO(), io.StringIO()
        refused_id = "msg-00000000000070008000000000000000"
        planted = ('{"id":"' + refused_id + '","private_message_body":"MUST_NOT_APPEAR"').encode()
        clock = [0.0]

        def plant(seconds):
            clock[0] += seconds
            (root.path / "events.jsonl").write_bytes(planted)

        result = run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-integrity-tail"},
            stdout=output, stderr=error,
            monotonic=lambda: clock[0], wall_time=lambda: self.epoch + clock[0], wait_readable=scripted_readiness((plant), interval=(1.0)), sleep=plant,
        )
        self.assertEqual(0, result)
        receipt = self.rows(root)[0]
        self.assertEqual("integrity_failure", receipt["reason_code"])
        self.assertIn("integrity_failure", receipt, "the exit must retain its failed check")
        failure = receipt["integrity_failure"]
        self.assertEqual("incomplete_jsonl_line", failure["check_id"])
        self.assertEqual("IntegrityFailure", failure["exception_class"])
        self.assertEqual("events.jsonl", failure["input_identity"]["path"])
        self.assertEqual(0, failure["input_identity"]["byte_offset"])
        self.assertEqual(1, failure["occurrence_count"])
        block = json.loads(output.getvalue())
        self.assertEqual("block", block["decision"])
        self.assertIn("consumption_state_unavailable", block["reason"])
        self.assertIn("incomplete_jsonl_line", block["reason"])
        self.assertNotIn("MUST_NOT_APPEAR", output.getvalue())
        self.assertNotIn(refused_id, output.getvalue())
        self.assertNotIn("MUST_NOT_APPEAR", json.dumps(receipt))
        self.assertEqual([], read_records_snapshot(
            root,
            fixtures.public_ids.compose("receipts/deliveries/", fixtures.public_ids.ledger(fixtures.public_ids.builder("floati"))),
            allowed_kinds=set(WAKE_HOLD_KINDS),
        ))

        repeated = io.StringIO()
        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-integrity-tail"},
            stdout=repeated, stderr=io.StringIO(),
            monotonic=lambda: clock[0], wall_time=lambda: self.epoch + clock[0],
            wait_readable=scripted_readiness((lambda seconds: clock.__setitem__(0, clock[0] + seconds)), interval=(1.0)), sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
        self.assertEqual(block, json.loads(repeated.getvalue()))
        self.assertEqual(2, self.rows(root)[1]["integrity_failure"]["occurrence_count"])

    def test_deadline_and_pause_keep_the_original_hook_output(self):
        from floati import fixture_ids as public_ids
        from floati.wake_control import WakeController

        root, workspace, _participant = self.fixture("deadline-control")
        clock = [0.0]
        output = io.StringIO()
        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-deadline-control"},
            stdout=output, stderr=io.StringIO(),
            monotonic=lambda: clock[0],
            wait_readable=scripted_readiness((lambda seconds: clock.__setitem__(0, clock[0] + seconds)), interval=(1.0)), sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
        self.assertEqual(
            '{"decision": "block", "reason": "(floati: wait deadline exhausted; end this turn to re-arm)"}\n',
            output.getvalue(),
        )
        self.assertNotIn("integrity_failure", self.rows(root)[0])
        root, workspace, _participant = self.fixture("pause-control")
        WakeController(root).pause(public_ids.builder("floati"), "seat-pause-control", idempotency_key="pause")
        output = io.StringIO()
        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-pause-control"},
            stdout=output, stderr=io.StringIO(),
        )
        self.assertEqual("", output.getvalue())
        self.assertNotIn("integrity_failure", self.rows(root)[0])
