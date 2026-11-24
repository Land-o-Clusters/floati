"""Am12/14 readiness from recipient-bound native mechanisms, scratch only.

Authored before implementation. The parent owns execution and RED banking.
Configuration establishes arrangement, never a running hook or daemon. These
cases consume the existing send API rather than importing a proposed reader.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import tests  # Pin HOME before importing product code or contained fixtures.

from floati.codex_wait_contract import (
    CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant,
    write_workspace_map,
)
from floati.cursor_wait import install_cursor_hook
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_board import SeatBoard
from floati.seat_waiter_lifecycle import retire_waiter
from floati.wake_control import WakeController
from tests.temp_roots import REAL_TEMP_ROOT


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CHECKED = ["lease", "stop_hook", "seat_board"]


def stamp(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RecipientHarnessHookCapabilityTests(unittest.TestCase):
    """F2 gate: unreadable hook capability differs from structural absence."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(temporary.name).resolve() / "fleet", create=True)
        self.registry = Registry(self.root)
        self.registry.register("sender", "worker")

    def observe_native_send(self, harness, expected_state, expected_hook):
        """Exercise the frozen send observation without any hook provenance."""
        self.registry.register("recipient", harness)
        before = self.registry.path.read_bytes()
        sent = EventLog(self.root).send(
            "sender", "recipient", "fixture", "a" * 40,
            "docs/evidence/harness-capability.md", "native capability witness",
            idempotency_key="harness-capability", now=NOW)
        readiness = sent["recipient_readiness"]
        self.assertEqual(expected_state, readiness["state"], readiness)
        self.assertEqual(CHECKED, readiness["checked_mechanisms"])
        self.assertEqual(["absent", expected_hook, "absent"],
            [fact["state"] for fact in readiness["mechanism_evidence"]], readiness)
        self.assertEqual("all_mechanisms_absent" if expected_hook == "absent"
            else "mechanism_evidence_incomplete", readiness["reason"])
        self.assertEqual(stamp(NOW), readiness["observed_at"])
        self.assertEqual(before, self.registry.path.read_bytes())
        physical = EventLog(self.root).event_records()
        self.assertEqual(["message_envelope", "send_readiness_observation"],
            [row["kind"] for row in physical])
        self.assertEqual(sent["message"]["id"], physical[1]["envelope_id"])
        self.assertEqual(readiness, physical[1]["readiness"])

    def test_claude_can_carry_an_unreadable_hook_so_readiness_is_unknown(self):
        cases = (
            ("Claude", "unknown", "unknown"),
            ("zcode", "unknown", "unknown"),
            ("worker", "recipient_not_listening", "absent"),
            ("smoke", "recipient_not_listening", "absent"),
            ("OpenCode", "unknown", "unknown"),
        )
        for index, case in enumerate(cases):
            harness, expected_state, expected_hook = case
            with self.subTest(harness=harness, expected_state=expected_state, expected_hook=expected_hook):
                if index:
                    self.doCleanups()
                    self.setUp()
                self.observe_native_send(harness, expected_state, expected_hook)


class RecipientHookMechanismTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.registry = Registry(self.root)
        self.node = "recipient"
        with mock.patch("floati.registry.utc_now", return_value=stamp(NOW)):
            self.registry.register("sender", "Codex")
            self.registry.register(self.node, "Cursor")
        self.workspace = self.base / "recipient-workspace"
        self.workspace.mkdir()
        self.runtime = self.base / "runtime"
        (self.runtime / "scripts").mkdir(parents=True)
        launcher = self.runtime / "scripts/floati"
        launcher.write_text("#!/bin/sh\nexit 31\n", encoding="utf-8")
        launcher.chmod(0o700)
        installed = install_cursor_hook(workspace=self.workspace,
            root=self.root.path, node=self.node, runtime=self.runtime)
        self.hooks = Path(installed["hooks_path"])
        self.install_record = (self.root.path / "state/cursor-wait"
                               / self.node / "installed-hook.json")
        saved = json.loads(self.install_record.read_text(encoding="utf-8"))
        self.assertEqual(str(self.root.path), saved["root"])
        self.assertEqual(self.node, saved["node"])
        self.assertEqual(str(self.workspace), saved["workspace"])
        self.assertEqual(str(self.hooks), saved["hooks_path"])
        self.assertEqual("not_leased", self.registry.node_lease_state(
            self.node, now=NOW)["state"])

    def readiness(self, key, *, observed=NOW):
        with mock.patch("floati.events.utc_now", return_value=stamp(observed)):
            sent = EventLog(self.root).send("sender", self.node, "fixture",
                "a" * 40, "docs/evidence/ping.md", "readiness fixture",
                idempotency_key=key, now=observed)
        return sent["recipient_readiness"]

    def empty_hooks(self):
        self.hooks.write_text(json.dumps({"version": 1,
            "hooks": {"stop": []}}) + "\n", encoding="utf-8")

    def board(self, *, deadline=2, key="readiness-board", session="board-session"):
        from tests.test_workspace_seat_declaration import WorkspaceSeatDeclarationTests

        WorkspaceSeatDeclarationTests.write_seat_fixture(
            self, self.workspace, self.root.path, self.node)
        write_workspace_map(self.root.path, self.workspace, self.node)
        participant = resolve_participant(self.root.path, self.workspace)
        self.assertIsNotNone(participant)
        self.binding = participant.binding
        with mock.patch("floati.codex_wait_contract.utc_now", return_value=stamp(NOW)),\
             mock.patch("floati.seat_board.utc_now", return_value=stamp(NOW)):
            self.consent = CodexWaitConsentLedger(self.root).arm(self.binding,
                hook_timeout_seconds=30, wait_deadline_seconds=deadline,
                idempotency_key=key + "-consent")
            result = SeatBoard(self.root).board(self.node, self.workspace,
                session, idempotency_key=key, take_over=True)
        self.assertEqual([], result["item_ids"], "fixture boards an empty inbox")
        self.assertEqual("completed", result["step"])
        return result

    def test_configured_legacy_hook_without_hold_testimony_is_unknown(self):
        """Catches treating a real configured hook's missing new receipt as deafness."""
        readiness = self.readiness("configured-legacy")
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_readable_exact_empty_config_checks_all_mechanisms_before_negative(self):
        """Catches declaring absence from an inactive lease without reading the hook."""
        self.empty_hooks()
        readiness = self.readiness("affirmative-absent")
        self.assertEqual("recipient_not_listening", readiness.get("state"), readiness)
        self.assertEqual(CHECKED, readiness.get("checked_mechanisms"), readiness)

    def test_sender_home_cannot_supply_recipient_configuration(self):
        """Catches substituting ambient sender HOME for the saved recipient path."""
        self.empty_hooks()
        sender_home = self.base / "sender-home"
        (sender_home / ".cursor").mkdir(parents=True)
        (sender_home / ".cursor/hooks.json").write_text(json.dumps({"version": 1,
            "hooks": {"stop": [{"command": "foreign-sender-hook"}]}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(sender_home)}):
            readiness = self.readiness("recipient-not-sender")
        self.assertEqual("recipient_not_listening", readiness.get("state"), readiness)
        self.assertEqual(CHECKED, readiness.get("checked_mechanisms"), readiness)

    def test_absent_provenance_is_unknown_even_when_sender_home_has_empty_config(self):
        """Catches converting an unresolvable recipient coordinate into no hook."""
        self.install_record.unlink()
        sender_home = self.base / "sender-home"
        (sender_home / ".cursor").mkdir(parents=True)
        (sender_home / ".cursor/hooks.json").write_text(
            '{"version":1,"hooks":{"stop":[]}}\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"HOME": str(sender_home)}):
            readiness = self.readiness("unbound-config")
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_unreadable_hook_coordinate_is_unknown(self):
        """Catches interpreting a named config that cannot be read as absent."""
        self.hooks.unlink()
        self.hooks.mkdir()  # A directory reliably fails an ordinary-file read.
        readiness = self.readiness("unreadable-config")
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_malformed_hook_config_is_unknown(self):
        """Catches turning malformed JSON or hook shape into an all-clear negative."""
        for index, encoded in enumerate((b'{"version":', b'{"version":1,"hooks":{"stop":false}}')):
            with self.subTest(encoded=encoded):
                self.hooks.write_bytes(encoded)
                readiness = self.readiness("malformed-config-" + str(index))
                self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_deeply_nested_hook_json_is_unknown_without_repair(self):
        encoded = b"[" * 2000 + b"0" + b"]" * 2000
        self.hooks.write_bytes(encoded)
        try:
            readiness = self.readiness("deeply-nested-config")
        except RecursionError:
            self.fail("bounded malformed hook JSON escaped the readiness boundary")
        self.assertEqual("unknown", readiness["state"], readiness)
        self.assertEqual(encoded, self.hooks.read_bytes())

    def test_foreign_install_coordinate_cannot_prove_absence_or_be_repaired(self):
        """Catches trusting a saved hooks_path that is outside its recipient workspace."""
        foreign = self.base / "foreign-workspace/.cursor/hooks.json"
        foreign.parent.mkdir(parents=True)
        foreign.write_text('{"version":1,"hooks":{"stop":[]}}\n', encoding="utf-8")
        saved = json.loads(self.install_record.read_text(encoding="utf-8"))
        saved["hooks_path"] = str(foreign)
        self.install_record.write_text(json.dumps(saved) + "\n", encoding="utf-8")
        before = (self.install_record.read_bytes(), foreign.read_bytes())
        readiness = self.readiness("foreign-coordinate")
        self.assertEqual("unknown", readiness.get("state"), readiness)
        self.assertEqual(before, (self.install_record.read_bytes(), foreign.read_bytes()))

    def test_foreign_recipient_in_install_record_is_unknown(self):
        """Catches borrowing a different node's readable empty configuration."""
        self.empty_hooks()
        saved = json.loads(self.install_record.read_text(encoding="utf-8"))
        saved["node"] = "another-recipient"
        self.install_record.write_text(json.dumps(saved) + "\n", encoding="utf-8")
        readiness = self.readiness("foreign-node")
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_multiple_recipient_workspaces_cannot_prove_global_hook_absence(self):
        self.empty_hooks()
        second = self.base / "other-recipient-workspace"
        second.mkdir()
        write_workspace_map(self.root.path, self.workspace, self.node)
        write_workspace_map(self.root.path, second, self.node)
        readiness = self.readiness("ambiguous-workspaces")
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_symlink_hook_config_cannot_supply_affirmative_absence(self):
        foreign = self.base / "foreign-hooks.json"
        foreign.write_text('{"version":1,"hooks":{"stop":[]}}\n', encoding="utf-8")
        self.hooks.unlink()
        self.hooks.symlink_to(foreign)
        readiness = self.readiness("symlink-config")
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_malformed_board_ledger_prevents_all_mechanisms_absent(self):
        self.empty_hooks()
        ledger = self.root.path / "receipts/seat-board" / (self.node + ".jsonl")
        ledger.parent.mkdir(parents=True)
        ledger.write_bytes(b"not-a-valid-event-frame")
        readiness = self.readiness("malformed-board-ledger")
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_administrative_active_lease_remains_positive_without_daemon_claim(self):
        """Catches losing the existing lease witness or relabeling it live-daemon proof."""
        lease = {"schema_version": 0, "id": "lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": stamp(NOW),
            "kind": "node_lease", "node_id": self.node,
            "workspace": str(self.workspace), "state": "active",
            "expires_at": stamp(NOW + timedelta(hours=1))}
        append_record(self.root, self.registry.relative_path, lease,
                      allowed_kinds={"registry_entry", "node_lease"})
        readiness = self.readiness("administrative-lease")
        self.assertEqual("recipient_wake_arranged", readiness.get("state"), readiness)
        self.assertEqual("active_lease", readiness.get("reason"), readiness)
        text = json.dumps(readiness).lower()
        self.assertNotIn("daemon_alive", text)
        self.assertNotIn("daemon_running", text)
        self.assertFalse((self.root.path / "state/wake-daemon").exists())

    def test_completed_recent_empty_board_is_a_positive_witness(self):
        """Catches requiring delivered mail before a current board can witness readiness."""
        self.board()
        readiness = self.readiness("empty-board", observed=NOW + timedelta(seconds=1))
        self.assertEqual("recipient_wake_arranged", readiness.get("state"), readiness)
        self.assertEqual("seat_board", readiness.get("reason"), readiness)

    def test_board_event_plane_symlink_cannot_qualify_native_references(self):
        self.board()
        events = self.root.path / "events.jsonl"
        foreign = self.base / "foreign-events.jsonl"
        foreign.write_bytes(events.read_bytes() if events.exists() else b"")
        if events.exists():
            events.unlink()
        events.symlink_to(foreign)
        # Observe directly: a send must not append through this forged plane.
        from floati.mail_health import RecipientReadiness
        readiness = RecipientReadiness.observe(self.root, self.node,
            observed_at=stamp(NOW + timedelta(seconds=1))).artifact()
        self.assertEqual("unknown", readiness["state"], readiness)

    def test_declaration_removed_between_snapshot_and_native_validation_is_unknown(self):
        from floati import seat_declaration
        from floati.mail_health import RecipientReadiness
        self.board()
        original = seat_declaration.require_declared_coordinate
        def remove_then_validate(workspace, node, root):
            (workspace / "SEAT.json").unlink()
            return original(workspace, node, root)
        with mock.patch("floati.seat_declaration.require_declared_coordinate", side_effect=remove_then_validate):
            readiness = RecipientReadiness.observe(self.root, self.node,
                observed_at=stamp(NOW + timedelta(seconds=1))).artifact()
        self.assertEqual("unknown", readiness["state"], readiness)

    def test_organic_whole_map_growth_does_not_invalidate_current_board(self):
        self.board()
        another = self.base / "different-seat"
        another.mkdir()
        write_workspace_map(self.root.path, another, "sender")
        readiness = self.readiness("organic-map-growth", observed=NOW + timedelta(seconds=1))
        self.assertEqual("recipient_wake_arranged", readiness["state"], readiness)
        self.assertEqual("seat_board", readiness["reason"], readiness)

    def test_current_board_in_one_of_multiple_recipient_workspaces_is_positive(self):
        self.board()
        another = self.base / "second-recipient-workspace"
        another.mkdir()
        write_workspace_map(self.root.path, another, self.node)
        readiness = self.readiness("multi-workspace-board", observed=NOW + timedelta(seconds=1))
        self.assertEqual("recipient_wake_arranged", readiness["state"], readiness)
        self.assertEqual("seat_board", readiness["reason"], readiness)

    def test_exact_live_hold_in_one_of_multiple_recipient_workspaces_is_positive(self):
        from floati.stop_hold import StopHoldLedger
        self.board()
        StopHoldLedger(self.root).start(self.binding, self.consent, "board-session",
            started_at=stamp(NOW), deadline_seconds=2)
        another = self.base / "second-recipient-workspace"
        another.mkdir()
        write_workspace_map(self.root.path, another, self.node)
        readiness = self.readiness("multi-workspace-hold", observed=NOW + timedelta(seconds=1))
        self.assertEqual("recipient_wake_arranged", readiness["state"], readiness)
        self.assertEqual("stop_hook", readiness["reason"], readiness)

    def test_registry_replacement_cannot_supply_a_borrowed_hook_registration(self):
        from floati import recipient_mechanisms
        from floati.mail_health import RecipientReadiness
        from floati.stop_hold import StopHoldLedger
        self.board()
        StopHoldLedger(self.root).start(self.binding, self.consent, "board-session",
            started_at=stamp(NOW), deadline_seconds=2)
        foreign = self.root.tenant_home / "borrowed-registry.jsonl"
        foreign.write_bytes(self.registry.path.read_bytes())
        original = recipient_mechanisms._rows
        changed = []
        def replace_after_read(root, relative, kinds, **kwargs):
            rows = original(root, relative, kinds, **kwargs)
            if relative == self.registry.relative_path and not changed:
                changed.append(True)
                self.registry.path.unlink()
                self.registry.path.symlink_to(foreign)
            return rows
        with mock.patch("floati.recipient_mechanisms._rows", side_effect=replace_after_read):
            readiness = RecipientReadiness.observe(self.root, self.node,
                observed_at=stamp(NOW + timedelta(seconds=1))).artifact()
        self.assertEqual("unknown", readiness["state"], readiness)

    def test_canonical_claude_hold_with_missing_native_claim_is_unknown(self):
        """A retained real start cannot replace the non-Cursor claim it requires."""
        from floati.mail_health import RecipientReadiness
        from floati.stop_hold import StopHoldLedger
        node, session = "claude-recipient", "claude-holding-session"
        self.registry.register(node, "Claude")
        workspace = self.base / "claude-workspace"
        workspace.mkdir()
        write_workspace_map(self.root.tenant_home, workspace, node)
        participant = resolve_participant(self.root.tenant_home, workspace)
        self.assertIsNotNone(participant, "the native Claude workspace must resolve")
        binding = participant.binding
        with mock.patch("floati.codex_wait_contract.utc_now", return_value=stamp(NOW)):
            consent = CodexWaitConsentLedger(self.root).arm(binding,
                hook_timeout_seconds=30, wait_deadline_seconds=2,
                idempotency_key="claude-hold-consent")
            claim = CodexWaitSessionLedger(self.root).arm(binding, consent, session,
                idempotency_key="claude-hold-claim")
        start = StopHoldLedger(self.root).start(binding, consent, session,
            started_at=stamp(NOW), deadline_seconds=2)
        self.assertEqual("claude", start["harness"])
        live = RecipientReadiness.observe(self.root, node,
            observed_at=stamp(NOW + timedelta(seconds=1))).artifact()
        self.assertEqual("arranged", live["mechanism_evidence"][1]["state"], live)
        claim_path = self.root.tenant_home / CodexWaitSessionLedger._relative(node)
        # Native receipt read proves this is the exact claim being removed.
        self.assertEqual([claim], read_records_snapshot(self.root,
            CodexWaitSessionLedger._relative(node),
            allowed_kinds={"codex_wait_session_receipt"}))
        retained = [self.registry.path,
            self.root.tenant_home / "codex-wait/workspaces.v0.json",
            self.root.tenant_home / CodexWaitConsentLedger._relative(node),
            self.root.tenant_home / StopHoldLedger.relative(node)]
        before = {path: path.read_bytes() for path in retained}
        claim_path.unlink()  # Deliberate corruption of this scratch plane only.
        readiness = RecipientReadiness.observe(self.root, node,
            observed_at=stamp(NOW + timedelta(seconds=1))).artifact()
        self.assertEqual("unknown", readiness["mechanism_evidence"][1]["state"], readiness)
        self.assertEqual("unknown", readiness["state"], readiness)
        self.assertFalse(claim_path.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in retained})

    def test_raw_pause_marker_dangling_alias_makes_hold_and_board_unknown(self):
        """A missing alias target cannot establish absence of the native marker."""
        import hashlib
        from floati.mail_health import RecipientReadiness
        from floati.stop_hold import StopHoldLedger
        self.board()
        StopHoldLedger(self.root).start(self.binding, self.consent, "board-session",
            started_at=stamp(NOW), deadline_seconds=2)
        live = RecipientReadiness.observe(self.root, self.node,
            observed_at=stamp(NOW + timedelta(seconds=1))).artifact()
        self.assertEqual("arranged", live["mechanism_evidence"][1]["state"], live)
        self.assertEqual("arranged", live["mechanism_evidence"][2]["state"], live)
        # Use the literal native coordinate, before resolve_relative can erase
        # its symlink identity by resolving it to a missing in-tenant target.
        digest = hashlib.sha256(b"board-session").hexdigest()
        marker = self.root.tenant_home / "state/wake-control" / self.node / (digest + ".json")
        marker.parent.mkdir(parents=True, exist_ok=True)
        missing = self.root.tenant_home / "missing-pause-target.json"
        self.assertFalse(missing.exists())
        marker.symlink_to(missing)
        retained = [self.registry.path,
            self.root.tenant_home / "codex-wait/workspaces.v0.json",
            self.root.tenant_home / CodexWaitSessionLedger._relative(self.node),
            self.root.tenant_home / CodexWaitConsentLedger._relative(self.node),
            self.root.tenant_home / StopHoldLedger.relative(self.node),
            self.root.tenant_home / "receipts/seat-board" / (self.node + ".jsonl")]
        before = {path: path.read_bytes() for path in retained}
        readiness = RecipientReadiness.observe(self.root, self.node,
            observed_at=stamp(NOW + timedelta(seconds=1))).artifact()
        self.assertEqual("unknown", readiness["mechanism_evidence"][1]["state"], readiness)
        self.assertEqual("unknown", readiness["mechanism_evidence"][2]["state"], readiness)
        self.assertEqual("unknown", readiness["state"], readiness)
        self.assertTrue(marker.is_symlink())
        self.assertEqual(str(missing), os.readlink(marker))
        self.assertFalse(missing.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in retained})

    def test_board_expiry_uses_its_referenced_consent_deadline(self):
        """Catches replacing a seat's consent deadline with a fixed freshness window."""
        self.board(deadline=7)
        recent = self.readiness("long-consent-recent", observed=NOW + timedelta(seconds=3))
        self.assertEqual("recipient_wake_arranged", recent.get("state"), recent)
        self.assertEqual("seat_board", recent.get("reason"), recent)
        expired = self.readiness("long-consent-expired", observed=NOW + timedelta(seconds=8))
        self.assertEqual("unknown", expired.get("state"), expired)

    def test_board_deadline_boundary_is_expired_not_an_extra_ready_second(self):
        self.board(deadline=2)
        before = self.readiness("before-deadline", observed=NOW + timedelta(seconds=1))
        self.assertEqual("recipient_wake_arranged", before.get("state"), before)
        boundary = self.readiness("at-deadline", observed=NOW + timedelta(seconds=2))
        self.assertEqual("unknown", boundary.get("state"), boundary)

    def test_later_long_consent_cannot_refresh_an_older_board_claim(self):
        """Catches substituting the newest consent for the claim's cited receipt."""
        self.board(deadline=2)
        with mock.patch("floati.codex_wait_contract.utc_now", return_value=stamp(NOW + timedelta(seconds=1))):
            CodexWaitConsentLedger(self.root).arm(self.binding,
                hook_timeout_seconds=300, wait_deadline_seconds=120,
                idempotency_key="later-long-consent")
        readiness = self.readiness("old-claim-new-consent", observed=NOW + timedelta(seconds=3))
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_superseding_unboarded_session_invalidates_previous_board_witness(self):
        """Catches continuing to advertise the displaced session's completed board."""
        self.board()
        with mock.patch("floati.codex_wait_contract.utc_now", return_value=stamp(NOW)):
            CodexWaitSessionLedger(self.root).arm(self.binding, self.consent,
                "superseding-session", idempotency_key="superseding-claim", take_over=True)
        readiness = self.readiness("superseded-board", observed=NOW + timedelta(seconds=1))
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_pause_invalidates_current_board_witness(self):
        """Catches treating a completed board as ready after its session pauses."""
        self.board()
        with mock.patch("floati.wake_control.utc_now", return_value=stamp(NOW)):
            WakeController(self.root).pause(self.node, "board-session",
                                           idempotency_key="pause-board-session")
        readiness = self.readiness("paused-board", observed=NOW + timedelta(seconds=1))
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_withdrawn_consent_invalidates_current_board_witness(self):
        """Catches a retained native claim advertising readiness after capability removal."""
        self.board()
        with mock.patch("floati.seat_waiter_lifecycle.utc_now", return_value=stamp(NOW)):
            removed = retire_waiter(self.root, self.node, self.workspace, "withdraw-consent")
        self.assertEqual("disarmed", removed["state"])
        readiness = self.readiness("withdrawn-board", observed=NOW + timedelta(seconds=1))
        self.assertEqual("unknown", readiness.get("state"), readiness)


class CodexRecipientJournalTests(unittest.TestCase):
    def setUp(self):
        from tests.test_seat_onboard import SeatOnboardingTests

        # Compose setup only, with cleanup registered before setup can fail.
        self.fixture = SeatOnboardingTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        code, output, errors = self.fixture.add()
        self.assertEqual(0, code, errors or output)
        self.prepared = json.loads(output)["evidence"]
        self.node = self.prepared["node_id"]
        from floati.seat_onboarding import _load, _record_path
        path = _record_path(self.fixture.root, self.fixture.home)
        saved = _load(path, self.fixture.root, self.fixture.home)
        self.assertEqual("ready", saved["state"])
        self.assertEqual(str(self.fixture.hooks), saved["request"]["codex_hooks"])
        self.assertEqual(str(self.fixture.config), saved["request"]["codex_config"])

    def readiness(self, key):
        sent = EventLog(self.fixture.root).send("architect", self.node, "fixture",
            "a" * 40, "docs/evidence/ping.md", "recipient journal fixture",
            idempotency_key=key, now=NOW)
        return sent["recipient_readiness"]

    def test_ready_codex_journal_with_configured_legacy_hook_is_unknown(self):
        """Catches missing shared hold testimony masquerading as absent configured Codex hook."""
        readiness = self.readiness("codex-configured-legacy")
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def test_ready_codex_journal_names_exact_empty_config_for_negative(self):
        """Catches ignoring the saved recipient Codex hook path when proving absence."""
        self.fixture.hooks.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")
        readiness = self.readiness("codex-affirmative-absent")
        self.assertEqual("recipient_not_listening", readiness.get("state"), readiness)
        self.assertEqual(CHECKED, readiness.get("checked_mechanisms"), readiness)

    def test_raw_ready_codex_journal_alias_cannot_prove_hook_absence(self):
        """A retained valid ready record cannot substitute for its ordinary path."""
        import hashlib
        from floati.mail_health import RecipientReadiness
        self.fixture.hooks.write_text('{"hooks":{"Stop":[]}}\n', encoding="utf-8")
        root = self.fixture.root
        before_alias = RecipientReadiness.observe(root, self.node,
            observed_at=stamp(NOW)).artifact()
        self.assertEqual("recipient_not_listening", before_alias["state"], before_alias)
        # Derive the literal native coordinate; do not use _record_path after
        # the link exists, because that would already follow the forged alias.
        token = hashlib.sha256(str(self.fixture.home).encode()).hexdigest()
        journal = root.tenant_home / "state/seat-onboarding" / (token + ".json")
        archived = journal.with_name("retained-ready-record.json")
        original = journal.read_bytes()
        self.assertEqual("ready", json.loads(original)["state"])
        journal.rename(archived)
        journal.symlink_to(archived)
        retained = [archived, self.fixture.hooks, self.fixture.config,
            Registry(root).path, root.tenant_home / "codex-wait/workspaces.v0.json",
            root.tenant_home / CodexWaitConsentLedger._relative(self.node)]
        before = {path: path.read_bytes() for path in retained}
        readiness = RecipientReadiness.observe(root, self.node,
            observed_at=stamp(NOW)).artifact()
        self.assertEqual("unknown", readiness["mechanism_evidence"][1]["state"], readiness)
        self.assertEqual("unknown", readiness["state"], readiness)
        self.assertTrue(journal.is_symlink())
        self.assertEqual(str(archived), os.readlink(journal))
        self.assertEqual(original, archived.read_bytes())
        self.assertEqual(before, {path: path.read_bytes() for path in retained})


class ClassicTenantReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open(self.base / "classic", "recipient-fleet")
        self.registry = Registry(self.root)
        self.registry.register("sender", "Codex")
        self.registry.register("recipient", "Cursor")
        self.workspace = self.base / "recipient-workspace"
        self.workspace.mkdir()
        runtime = self.base / "runtime"
        (runtime / "scripts").mkdir(parents=True)
        (runtime / "scripts/floati").write_text("#!/bin/sh\nexit 31\n", encoding="utf-8")
        (runtime / "scripts/floati").chmod(0o700)
        installed = install_cursor_hook(workspace=self.workspace,
            root=self.root.tenant_home, node="recipient", runtime=runtime)
        self.hooks = Path(installed["hooks_path"])

    def observe(self):
        from floati.mail_health import RecipientReadiness
        return RecipientReadiness.observe(self.root, "recipient", observed_at=stamp(NOW)).artifact()

    def add_lease(self):
        lease = {"schema_version": 0, "id": "lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": stamp(NOW),
            "kind": "node_lease", "node_id": "recipient",
            "workspace": str(self.workspace), "state": "active",
            "expires_at": stamp(NOW + timedelta(hours=1))}
        append_record(self.root, self.registry.relative_path, lease,
                      allowed_kinds={"registry_entry", "node_lease"})
        return lease

    def test_actual_provider_switch_does_not_obscure_native_active_lease(self):
        import io
        from floati.admin_registry import RegistryAdminBackend
        from floati.provider_switch import ProviderSwitchWizard
        self.add_lease()
        ProviderSwitchWizard(self.root, RegistryAdminBackend(self.root),
            id_factory=uuid7_hex, now=lambda: NOW).switch_from_keys(
                ["recipient", "Cursor", "fixture-model"], io.StringIO())
        readiness = self.observe()
        self.assertEqual("recipient_wake_arranged", readiness["state"], readiness)
        self.assertEqual("active_lease", readiness["reason"], readiness)

    def test_registry_symlink_after_guarded_snapshot_cannot_supply_lease(self):
        from floati import recipient_mechanisms
        self.add_lease()
        original = recipient_mechanisms._rows
        foreign = self.root.tenant_home / "borrowed-registry.jsonl"
        foreign.write_bytes(self.registry.path.read_bytes())
        changed = []
        def replace_after_read(root, relative, kinds, **kwargs):
            rows = original(root, relative, kinds, **kwargs)
            if relative == self.registry.relative_path and not changed:
                changed.append(True)
                self.registry.path.unlink()
                self.registry.path.symlink_to(foreign)
            return rows
        with mock.patch("floati.recipient_mechanisms._rows", side_effect=replace_after_read):
            readiness = self.observe()
        self.assertEqual("unknown", readiness["state"], readiness)

    def test_classic_native_lease_ignores_foreign_namespace_plane_and_cites_tenant(self):
        lease = {"schema_version": 0, "id": "lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": stamp(NOW),
            "kind": "node_lease", "node_id": "recipient",
            "workspace": str(self.workspace), "state": "active",
            "expires_at": stamp(NOW + timedelta(hours=1))}
        append_record(self.root, self.registry.relative_path, lease,
                      allowed_kinds={"registry_entry", "node_lease"})
        foreign = self.root.path / "registry/entries.jsonl"
        foreign.parent.mkdir()
        foreign.write_bytes(b"foreign malformed namespace plane")
        readiness = self.observe()
        self.assertEqual("recipient_wake_arranged", readiness["state"], readiness)
        self.assertEqual("active_lease", readiness["reason"], readiness)
        lease_fact = readiness["mechanism_evidence"][0]
        self.assertEqual([{"path": str(self.registry.path), "record_id": lease["id"]}],
                         lease_fact["sources"])
        self.assertEqual(b"foreign malformed namespace plane", foreign.read_bytes())

    def test_classic_exact_tenant_cursor_empty_hook_proves_absence(self):
        self.hooks.write_text('{"version":1,"hooks":{"stop":[]}}\n', encoding="utf-8")
        readiness = self.observe()
        self.assertEqual("recipient_not_listening", readiness["state"], readiness)
        self.assertEqual(CHECKED, readiness["checked_mechanisms"])
        self.assertEqual(str(self.root.tenant_home / "state/cursor-wait/recipient/installed-hook.json"),
            readiness["mechanism_evidence"][1]["sources"][0]["path"])

    def test_classic_namespace_root_is_not_an_alias_for_hook_coordinate(self):
        self.hooks.write_text('{"version":1,"hooks":{"stop":[]}}\n', encoding="utf-8")
        provenance = self.root.tenant_home / "state/cursor-wait/recipient/installed-hook.json"
        saved = json.loads(provenance.read_text(encoding="utf-8"))
        saved["root"] = str(self.root.path)
        provenance.write_text(json.dumps(saved), encoding="utf-8")
        readiness = self.observe()
        self.assertEqual("unknown", readiness["state"], readiness)


if __name__ == "__main__":
    unittest.main()
