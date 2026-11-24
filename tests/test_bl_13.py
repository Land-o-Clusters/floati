"""BL-13: a dead claimant does not need a takeover.

The ruling pins the wake adapter's own coordinate as the one admissible
liveness proof: a claimant whose binding measures ``runtime_missing`` is
superseded by ``board`` without ``--take-over`` and the arm receipt names it
(``claim_superseded_dead``); a claimant whose runtime resolves — or whose
liveness cannot be measured at all — keeps today's contested refusal.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from floati import fixture_ids as ids
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests import test_seat_board


class DeadClaimantSupersessionTests(test_seat_board._SeatBoardFixture, unittest.TestCase):
    def bind_claimant_daemon(self, session: str, *, with_runtime: bool) -> str:
        node = ids.builder("floati")
        coordinate = DaemonCoordinate(self.root, node, "codex")
        executable = self.base / "codex-target"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        AdapterBindingStore(self.root).write(
            coordinate,
            session_id=session,
            workspace=self.workspace,
            executable=executable,
            adapter_version="1",
            adapter_digest=hashlib.sha256(b"adapter-contract").hexdigest(),
            binding_epoch=1,
        )
        runtime = (
            self.root.path
            / "state"
            / "wake-daemon"
            / "runtime"
            / f"{coordinate.digest}.json"
        )
        if with_runtime:
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.write_text(
                json.dumps(
                    {
                        "schema_version": 0,
                        "node_id": node,
                        "harness": "codex",
                        "session_digest": hashlib.sha256(
                            session.encode("utf-8")
                        ).hexdigest(),
                        "circuit_state": "closed",
                    }
                ),
                encoding="utf-8",
            )
        return str(runtime)

    def test_board_supersedes_a_claimant_whose_runtime_is_gone_and_receipts_it(self):
        prior = self.arm_session("prior-session")
        self.bind_claimant_daemon("prior-session", with_runtime=False)

        # RED at the frozen tree: board refuses seat_board_claim_contested
        # even though the claimant's own wake coordinate measures
        # runtime_missing — the dead claimant blocks every turnover.
        result = self.board()

        self.assertEqual(prior["id"], result["predecessor_receipt_id"])
        from floati.jsonl import read_records_snapshot
        rows = read_records_snapshot(
            self.root,
            ids.compose(
                "receipts/codex-wait-session/",
                ids.ledger(ids.builder("floati")),
            ),
            allowed_kinds={"codex_wait_session_receipt"},
        )
        arm_receipt = next(r for r in rows if r["id"] == result["arm_receipt_id"])
        self.assertEqual(
            "prior-session", arm_receipt["claim_superseded_dead"]["claimant"]
        )
        self.assertEqual(
            "runtime_missing", arm_receipt["claim_superseded_dead"]["evidence"]
        )

    def test_a_claimant_with_a_resolvable_runtime_stays_contested(self):
        self.arm_session("prior-session")
        self.bind_claimant_daemon("prior-session", with_runtime=True)

        from floati.errors import ProtocolRefusal

        with self.assertRaises(ProtocolRefusal) as caught:
            self.board()
        self.assertEqual("seat_board_claim_contested", caught.exception.code)

    def test_a_claimant_without_any_daemon_binding_stays_contested(self):
        # Green control carried from base: no wake coordinate at all is not
        # proof of death, so the contested refusal stands unchanged.
        self.arm_session("prior-session")

        from floati.errors import ProtocolRefusal

        with self.assertRaises(ProtocolRefusal) as caught:
            self.board()
        self.assertEqual("seat_board_claim_contested", caught.exception.code)
