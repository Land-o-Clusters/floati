"""Native controls for affirmative closed-claimant visibility, never recovery."""
from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records_snapshot
from floati.mail_health import RecipientReadiness
from floati.registry import Registry
from floati.seat_board import SeatBoard
from floati.stop_hold import StopHoldLedger
from tests import test_f2_readiness_hold as native
from tests.waiter_timing import scripted_readiness


def stamp(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ClosedClaimantReadinessControls(unittest.TestCase):
    def setUp(self):
        self.f = native.NativeStopHoldReadinessTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.fixture("Codex")

    def run_native(self, action=None):
        clock = native._Clock(action or (lambda: None))
        rows = self.f.hold_rows()
        if rows:
            clock.epoch = (datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00"))
                + timedelta(seconds=1)).timestamp()
        output, error = io.StringIO(), io.StringIO()
        code = run_stop_waiter(bus_home=self.f.root.path,
            hook_payload={"cwd": str(self.f.workspace), "session_id": self.f.session},
            stdout=output, stderr=error, monotonic=clock.monotonic,
            wait_readable=scripted_readiness((clock.sleep), interval=(1.0)), sleep=clock.sleep, wall_time=clock.wall_time)
        self.assertEqual((0, ""), (code, error.getvalue()))
        self.assertTrue(clock.called)
        self.assertTrue(output.getvalue())
        return clock

    def closed(self):
        self.run_native()
        rows = self.f.hold_rows()
        self.assertEqual(["start", "exit"], [row["phase"] for row in rows])
        claim = read_records_snapshot(self.f.root,
            CodexWaitSessionLedger._relative(self.f.node),
            allowed_kinds={"codex_wait_session_receipt"})[-1]
        observed = datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00")) + timedelta(seconds=1)
        return rows, claim, observed

    def observe(self, observed):
        return RecipientReadiness.observe(self.f.root, self.f.node,
            observed_at=stamp(observed)).artifact()

    def assert_no_closed_fact(self, observed):
        readiness = self.observe(observed)
        self.assertFalse(any(fact["reason"] == "closed_claimant_not_holding"
            for fact in readiness["mechanism_evidence"]), readiness)
        return readiness

    def test_closed_current_claim_is_cited_without_mutating_native_planes(self):
        rows, claim, observed = self.closed()
        paths = [self.f.root.tenant_home / relative for relative in (
            StopHoldLedger.relative(self.f.node), CodexWaitSessionLedger._relative(self.f.node),
            CodexWaitConsentLedger._relative(self.f.node), "codex-wait/workspaces.v0.json")]
        before = {path: path.read_bytes() for path in paths}
        readiness = self.observe(observed)
        native.assert_closed_claim_visibility(self, readiness, claim, rows, self.f.root)
        self.assertEqual("closed_claimant_not_holding", readiness["mechanism_evidence"][1]["reason"])
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_newer_real_active_hold_outranks_prior_closure(self):
        self.closed()
        observed = []
        def holding():
            start = self.f.hold_rows()[-1]
            value = self.observe(datetime.fromisoformat(start["started_at"].replace("Z", "+00:00")))
            self.assertEqual("active_stop_hold", value["mechanism_evidence"][1]["reason"])
            self.assertEqual("recipient_wake_arranged", value["state"])
            observed.append(value)
        self.run_native(holding)
        self.assertEqual(1, len(observed))

    def test_unmatched_expired_invocation_does_not_revive_old_closure(self):
        rows, claim, observed = self.closed()
        consent = CodexWaitConsentLedger(self.f.root).require_armed(self.f.binding)
        start = StopHoldLedger(self.f.root).start(self.f.binding, consent, self.f.session,
            started_at=stamp(observed), deadline_seconds=2)
        self.assertEqual("start", start["phase"])
        self.assert_no_closed_fact(observed + timedelta(seconds=10))

    def test_observation_before_native_exit_does_not_cite_closure(self):
        rows, claim, observed = self.closed()
        exit_time = datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00"))
        self.assert_no_closed_fact(exit_time - timedelta(milliseconds=1))

    def test_decodable_pair_exceeding_consent_budget_does_not_attest_closure(self):
        from floati.framing import encode_frame
        rows, claim, observed = self.closed()
        consent = CodexWaitConsentLedger(self.f.root).require_armed(self.f.binding)
        authority_paths = [self.f.root.tenant_home / ledger._relative(self.f.node)
            for ledger in (CodexWaitSessionLedger, CodexWaitConsentLedger)]
        authority_before = {path: path.read_bytes() for path in authority_paths}
        changed = [dict(row, deadline_seconds=consent["wait_deadline_seconds"] + 1)
            for row in rows]
        path = self.f.root.tenant_home / StopHoldLedger.relative(self.f.node)
        path.write_bytes(b"".join(encode_frame(row) for row in changed))
        self.assertEqual(changed, StopHoldLedger(self.f.root).read(self.f.node))
        self.assert_no_closed_fact(observed)
        self.assertEqual(authority_before, {path: path.read_bytes() for path in authority_paths})

    def test_same_session_rearm_after_exit_cannot_inherit_closure(self):
        rows, claim, observed = self.closed()
        consent = CodexWaitConsentLedger(self.f.root).require_armed(self.f.binding)
        with mock.patch("floati.codex_wait_contract.utc_now", return_value=stamp(observed)):
            new = CodexWaitSessionLedger(self.f.root).arm(self.f.binding, consent,
                self.f.session, idempotency_key="post-exit-rearm")
        self.assertNotEqual(claim["id"], new["id"])
        self.assert_no_closed_fact(observed + timedelta(seconds=1))

    def test_superseding_claim_cannot_inherit_predecessor_closure(self):
        rows, claim, observed = self.closed()
        consent = CodexWaitConsentLedger(self.f.root).require_armed(self.f.binding)
        CodexWaitSessionLedger(self.f.root).arm(self.f.binding, consent,
            "successor", idempotency_key="supersede-closed", take_over=True)
        self.assert_no_closed_fact(observed)

    def test_replaced_consent_does_not_attest_old_closure(self):
        rows, claim, observed = self.closed()
        CodexWaitConsentLedger(self.f.root).arm(self.f.binding, hook_timeout_seconds=10,
            wait_deadline_seconds=3, idempotency_key="replace-consent")
        self.assert_no_closed_fact(observed)

    def test_future_claim_does_not_attest_closure(self):
        rows, claim, observed = self.closed()
        consent = CodexWaitConsentLedger(self.f.root).require_armed(self.f.binding)
        with mock.patch("floati.codex_wait_contract.utc_now", return_value=stamp(observed + timedelta(days=1))):
            CodexWaitSessionLedger(self.f.root).arm(self.f.binding, consent,
                self.f.session, idempotency_key="future-rearm")
        self.assert_no_closed_fact(observed)

    def test_consent_issued_after_exit_does_not_attest_earlier_closure(self):
        issued = datetime.now(timezone.utc) + timedelta(minutes=1)
        with mock.patch("floati.codex_wait_contract.utc_now", return_value=stamp(issued)):
            CodexWaitConsentLedger(self.f.root).arm(self.f.binding,
                hook_timeout_seconds=10, wait_deadline_seconds=2,
                idempotency_key="future-consent-clock-rollback")
        rows, claim, observed = self.closed()
        self.assertLess(datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00")), issued)
        self.assert_no_closed_fact(issued + timedelta(seconds=1))

    def test_native_plane_changed_during_later_reader_invalidates_closed_fact(self):
        from floati import recipient_mechanisms
        rows, claim, observed = self.closed()
        path = self.f.root.tenant_home / StopHoldLedger.relative(self.f.node)
        foreign = self.f.base / "replaced-hold.jsonl"
        foreign.write_bytes(path.read_bytes())
        original = recipient_mechanisms._board
        def change_at_board(*args, **kwargs):
            path.unlink()
            path.symlink_to(foreign)
            return original(*args, **kwargs)
        with mock.patch.object(recipient_mechanisms, "_board", side_effect=change_at_board):
            self.assert_no_closed_fact(observed)

    def test_ambiguous_workspace_map_does_not_attest_closure(self):
        rows, claim, observed = self.closed()
        other = self.f.workspace.parent / "other-workspace"
        other.mkdir()
        path = self.f.root.path / "codex-wait/workspaces.v0.json"
        value = json.loads(path.read_text())
        value["mappings"].append({"workspace": str(other), "node_id": self.f.node})
        value["mappings"].sort(key=lambda row: (row["workspace"], row["node_id"]))
        path.write_text(json.dumps(value))
        self.assert_no_closed_fact(observed)

    def test_unreadable_or_aliased_hold_never_attests_closure(self):
        rows, claim, observed = self.closed()
        path = self.f.root.tenant_home / StopHoldLedger.relative(self.f.node)
        original = path.read_bytes()
        path.write_bytes(b"malformed native hold\n")
        self.assert_no_closed_fact(observed)
        path.unlink()
        foreign = self.f.base / "borrowed-hold.jsonl"
        foreign.write_bytes(original)
        path.symlink_to(foreign)
        self.assert_no_closed_fact(observed)
        path.unlink()
        path.mkdir()
        self.assert_no_closed_fact(observed)

    def test_active_administrative_lease_preserves_independent_readiness(self):
        rows, claim, observed = self.closed()
        lease = {"schema_version": 0, "id": "lease-" + uuid7_hex(),
            "tenant_id": self.f.root.tenant_id, "timestamp": stamp(observed),
            "kind": "node_lease", "node_id": self.f.node, "workspace": str(self.f.workspace),
            "state": "active", "expires_at": stamp(observed + timedelta(hours=1))}
        append_record(self.f.root, Registry(self.f.root).relative_path, lease,
            allowed_kinds={"registry_entry", "node_lease"})
        value = self.observe(observed)
        self.assertEqual("active_lease", value["reason"])
        native.assert_closed_claim_visibility(self, value, claim, rows, self.f.root)

    def test_recent_board_cannot_mask_early_native_exit_but_later_board_can(self):
        from tests.test_workspace_seat_declaration import WorkspaceSeatDeclarationTests
        WorkspaceSeatDeclarationTests.write_seat_fixture(self, self.f.workspace,
            self.f.root.path, self.f.node)
        board = SeatBoard(self.f.root).board(self.f.node, self.f.workspace,
            self.f.session, idempotency_key="pre-hold-board")
        self.assertEqual([], board["item_ids"])
        self.run_native(lambda: self.f.send("early-close"))
        rows = self.f.hold_rows()
        claims = read_records_snapshot(self.f.root, CodexWaitSessionLedger._relative(self.f.node),
            allowed_kinds={"codex_wait_session_receipt"})
        observed = datetime.fromisoformat(rows[-1]["timestamp"].replace("Z", "+00:00")) + timedelta(milliseconds=1)
        value = self.observe(observed)
        native.assert_closed_claim_visibility(self, value, claims[-1], rows, self.f.root)
        self.assertNotEqual("arranged", value["mechanism_evidence"][2]["state"], value)
        self.assertEqual("unknown", value["state"])
        with mock.patch("floati.codex_wait_contract.utc_now", return_value=stamp(observed)),\
             mock.patch("floati.seat_board.utc_now", return_value=stamp(observed)):
            later = SeatBoard(self.f.root).board(self.f.node, self.f.workspace,
                self.f.session, idempotency_key="after-hold-board")
        self.assertTrue(later["arm_receipt_id"])
        value = self.observe(observed + timedelta(milliseconds=1))
        self.assertEqual("seat_board", value["reason"], value)


if __name__ == "__main__":
    unittest.main()
