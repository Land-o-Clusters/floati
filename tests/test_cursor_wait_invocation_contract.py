"""Authority and native absence remain required at invocation boundaries."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal
import unittest
from unittest import mock

from floati.codex_wait_liveness import process_start_epoch
from floati.cursor_wait import run_cursor_stop_wait
from floati.errors import ProtocolRefusal
from floati import waiter_invocation
from tests.cursor_invocation_fixture import CursorInvocationFixture


class CursorWaitInvocationContractTests(unittest.TestCase):
    def setUp(self):
        self.f = CursorInvocationFixture(self)

    def test_held_cursor_reuses_projection_only_while_its_selected_proof_is_current(self):
        from floati import cut_prefix, inbox_prefix
        from floati.bus_epoch import epoch_guard
        from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
        from floati.cursor_presentations import CursorPresentationLedger
        from floati.wake_hold import wake_coordination_guard
        from tests.bus_scale_io import IOProbe

        f = self.f
        message = f.send("held-memo")
        self.assertIsNotNone(inbox_prefix.warm(f.root))
        self.assertIsNotNone(cut_prefix.warm(f.root))
        plane = CursorPresentationLedger(f.root, sealed="bytes")
        binding = resolve_participant(f.root.tenant_home, f.workspace).binding
        with epoch_guard(f.root, exclusive=False), wake_coordination_guard(f.root, f.node):
            consent = CodexWaitConsentLedger(f.root).require_armed(binding)
            classification = CodexWaitSessionLedger(f.root)._participate_already_guarded(
                binding, consent, f.session, classified=True)

            def select():
                return plane._select_waiter(binding, consent, classification, harness="cursor")

            fresh = select()
            prepared = plane.prepare(fresh, {"followup_message": message["id"]})
            plane.flushed(prepared, plane.deliver(prepared, fresh))
            held = select()
            expected = plane.held_evidence(held)
            self.assertEqual([message["id"]], [item["item_id"] for item in expected["held_items"]])
            held["held_items"].clear()
            with IOProbe(f.root.tenant_home) as unchanged:
                self.assertEqual(expected, plane.held_evidence(select()))
            unchanged.assert_complete()
            self.assertEqual(0, unchanged.read_bytes_by_path.get(plane.relative(f.node).as_posix(), 0),
                "unchanged held poll reread its complete presentation ledger")
            (f.root.tenant_home / cut_prefix.cache_name() / "checkpoint.json").write_bytes(b"broken")
            with IOProbe(f.root.tenant_home) as fallback:
                self.assertEqual(expected, plane.held_evidence(select()))
            fallback.assert_complete()
            self.assertGreaterEqual(fallback.read_bytes_by_path.get("events.jsonl", 0),
                (f.root.tenant_home / "events.jsonl").stat().st_size)
            self.assertEqual(expected, plane.held_evidence(plane.select(binding, consent, classification, harness="cursor")))

    def test_cold_waiter_flushes_selected_mail_without_reading_foreign_history(self):
        from floati import cut_prefix, inbox_prefix
        from floati.events import EventLog
        from tests.bus_scale_io import IOProbe

        f = self.f
        log = EventLog(f.root)
        foreign = log.send("architect", "architect", "floati", "a" * 40,
            "note.md", "unrelated retained mail", idempotency_key="foreign-mail")
        previous = foreign["id"]
        retracted = None
        for key in ("first-arrival", "later-arrival"):
            with self.subTest(arrival=key):
                if key == "later-arrival":
                    worker = log.send("architect", f.node, "floati", "a" * 40,
                        "note.md", "worker mail", idempotency_key="worker-arrival",
                        worker_session_id="selected-worker")
                    retracted = log.retract(worker["id"], worker_session_id="selected-worker",
                        reason="sent_in_error", author="architect")
                message = f.send(key)
                self.assertIsNotNone(inbox_prefix.warm(f.root))
                self.assertIsNotNone(cut_prefix.warm(f.root))
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch("floati.cursor_wait.Path.cwd", return_value=f.workspace),\
                        IOProbe(f.root.tenant_home) as probe:
                    result = run_cursor_stop_wait(root=f.root.path, node=f.node,
                        runtime=f.runtime, deadline_seconds=30, poll_seconds=.05,
                        hook_timeout_seconds=60,
                        payload={"conversation_id": f.session, "status": "completed"},
                        stdout=output, stderr=errors)
                probe.assert_complete()
                self.assertEqual(0, result, errors.getvalue())
                followup = json.loads(output.getvalue())["followup_message"]
                self.assertIn(message["id"], followup)
                self.assertNotIn(previous, followup)
                if retracted is not None:
                    self.assertNotIn(retracted["retracted_message_id"], followup)
                self.assertLess(probe.read_bytes_by_path.get("events.jsonl", 0),
                    log.path.stat().st_size, "a cold selection replayed the whole event ledger")
                previous = message["id"]

    def test_cursor_adapter_delivers_for_an_authorized_codex_role_binding(self):
        f = CursorInvocationFixture(self, harness="Codex", node="shared-seat")
        message = f.send("cursor-adapter-shared-binding")
        output = io.StringIO()
        with mock.patch("floati.cursor_wait.Path.cwd", return_value=f.workspace):
            result = run_cursor_stop_wait(root=f.root.path, node=f.node,
                runtime=f.runtime, deadline_seconds=30, poll_seconds=.05,
                hook_timeout_seconds=60,
                payload={"conversation_id": f.session, "status": "completed"},
                stdout=output, stderr=io.StringIO())
        self.assertEqual(0, result)
        body = json.loads(output.getvalue())
        self.assertIn("followup_message", body,
            "a single authorized Cursor invocation must preserve native delivery across registry roles")
        self.assertIn(message["id"], body["followup_message"])

    def test_authority_appearing_after_unbound_preflight_cannot_hold_or_deliver_without_lease(self):
        self.f.send("preflight-authority-race")
        output = io.StringIO()
        with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.f.workspace),\
                mock.patch("floati.waiter_invocation.verified_invocation", return_value=None):
            result = run_cursor_stop_wait(root=self.f.root.path, node=self.f.node,
                runtime=self.f.runtime, deadline_seconds=30, poll_seconds=.05, hook_timeout_seconds=60,
                payload={"conversation_id": self.f.session, "status": "completed"},
                stdout=output, stderr=io.StringIO())
        self.assertEqual(0, result)
        self.assertEqual({}, json.loads(output.getvalue()),
            "late authority cannot authorize an invocation that never acquired its kernel lease")
        self.assertEqual([], self.f.rows())
        self.assertFalse((self.f.root.path / "receipts/deliveries" / (self.f.node + ".jsonl")).exists())

    def test_unreadable_birth_of_live_process_is_not_actual_exit_proof(self):
        birth = process_start_epoch(os.getpid())
        self.assertIsNotNone(birth)
        with mock.patch("floati.waiter_invocation.process_start_epoch", return_value=None):
            with self.assertRaises(ProtocolRefusal) as caught:
                waiter_invocation._same_process(os.getpid(), birth)
        self.assertEqual("waiter_invocation_unproven", caught.exception.code)

    def test_symlinked_invocation_parent_cannot_redirect_lease_or_holder_writes(self):
        target = self.f.root.path / "foreign-state"
        target.mkdir()
        parent = self.f.root.path / "state"
        parent.mkdir(exist_ok=True)
        (parent / "waiter-invocations").symlink_to(target, target_is_directory=True)
        with self.assertRaises(ProtocolRefusal):
            lease = waiter_invocation.verified_invocation(self.f.root.path, self.f.node,
                self.f.workspace, self.f.session)
            if lease is not None:
                lease.close()
        self.assertEqual([], list(target.iterdir()))

    def test_host_exit_retires_hook_even_when_intermediate_parent_survives(self):
        host = self.f.start(host="chain")
        self.f.wait_started()
        pid = int(Path(str(host["control"]) + ".pid").read_text())
        bridge_pid = int(Path(str(host["control"]) + ".bridge.pid").read_text())
        births = {value: process_start_epoch(value) for value in (pid, bridge_pid)}
        def cleanup():
            for child_pid, birth in births.items():
                if birth is not None and process_start_epoch(child_pid) == birth:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        self.addCleanup(cleanup)
        host["control"].touch()
        host["process"].wait(timeout=3)
        self.f.wait_until(lambda: len(self.f.rows()) == 2,
            "a surviving intermediate parent hid the host's exit", seconds=3)
        self.f.wait_until(lambda: process_start_epoch(pid) != births[pid],
            "host-exit native receipt did not accompany actual hook process exit", seconds=3)


if __name__ == "__main__":
    unittest.main()
