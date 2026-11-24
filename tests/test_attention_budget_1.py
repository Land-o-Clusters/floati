"""ATTENTION-BUDGET-1: every dispatch shows the person's unread review queue against their budget.

The row (docs/status/FLOATI-FIELD-ROWS-2026-11-22.md on the board branch, as
amended readout-first by the field-research synthesis): the inbox always shows
its unread review count and oldest item against the declared budget, and that
readout appears on every new dispatch. Holding a dispatch is a separate step
the person switches on, off by default; with it off nothing is delayed. With
it on, an over-budget dispatch is held with its reason (budget, count, oldest
item), never silently refused or silently sent, and is released by the
person's act or by the queue falling within budget; every hold and release is
a receipt.

It extends the inbox fixture in tests/test_needs_you_inbox_1.py, the module
that owns the items this budget counts; a separate module keeps the row's RED
and its receipts readable on their own.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

import tests  # noqa: F401  Pin HOME before any product import resolves a path.

from floati.jsonl import read_records_snapshot
from floati.mcp import McpServer
from tests.banked_sha_fixture import banked_send_repository
from tests.test_needs_you_inbox_1 import (FILER, OTHER, FleetCase, OperatorTerminal, _filed,
                                          floati_argv)


class AttentionBudgetTests(OperatorTerminal, FleetCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository, self.sha = banked_send_repository(self.base)
        self.reviews = [_filed(self, self.fleet.file("review-" + str(index), title="review " + str(index)))
                        for index in range(2)]

    def send(self, note: str, key: str, *extra: str):
        return self.fleet.cli("send", "--root", str(self.fleet.root.path), "--from", FILER, "--to", OTHER,
                              "--repo", "floati", "--sha", self.sha, "--repository", str(self.repository),
                              "--doc", "note.md",
                              "--note", note, "--idempotency-key", key, *extra)

    def delivered(self, note: str) -> bool:
        _code, artifact = self.fleet.cli("inbox", "--root", str(self.fleet.root.path), "--as", OTHER, "--peek")
        return note in json.dumps(artifact.get("evidence", {}).get("messages", []), ensure_ascii=False)

    def budget(self, *arguments: str):
        code, artifact, screen = self.at_terminal(
            floati_argv("needs", "budget", "--root", str(self.fleet.root.path), *arguments),
            cwd=self.neutral, answer=lambda shown: shown)
        self.assertEqual(0, code, screen)
        return artifact["evidence"]

    def holds(self) -> list:
        rows = read_records_snapshot(self.fleet.root, "receipts/needs/ledger.jsonl",
                                     allowed_kinds={"needs_item", "needs_read", "needs_close",
                                                    "needs_budget", "needs_dispatch_hold"})
        return [row for row in rows if row["kind"] == "needs_dispatch_hold"]

    def test_every_dispatch_carries_the_readout_and_holding_is_off_by_default(self) -> None:
        oldest = self.reviews[0]["id"]
        sent = _filed(self, self.send("before any budget", "send-1"))
        self.assertEqual((2, oldest, None), (sent["attention"]["unread_reviews"],
                                             sent["attention"]["oldest"]["item_id"],
                                             sent["attention"]["budget"]))
        self.budget("--limit", "1", "--idempotency-key", "budget-1")
        over = _filed(self, self.send("over budget, holding off", "send-2"))
        self.assertEqual((1, 2, True, False), (over["attention"]["budget"], over["attention"]["unread_reviews"],
                                               over["attention"]["over"], over["attention"]["hold_dispatches"]))
        self.assertTrue(self.delivered("over budget, holding off"))
        readout = self.fleet.show()["budget"]
        self.assertEqual((1, 2, oldest, True), (readout["limit"], readout["unread_reviews"],
                                                readout["oldest"]["item_id"], readout["over"]))
        self.assertEqual([], self.holds())

    def test_with_holding_on_an_over_budget_dispatch_is_held_with_its_reason_until_within_budget(self) -> None:
        self.budget("--limit", "1", "--hold-dispatches", "--idempotency-key", "budget-1")
        code, held = self.send("first held dispatch", "send-1")
        evidence = held["evidence"]
        self.assertEqual((20, "refused", "dispatch_held_for_attention"), (code, held["status"], evidence["code"]))
        self.assertEqual((1, 2, self.reviews[0]["id"]), (evidence["attention"]["budget"],
                                                         evidence["attention"]["unread_reviews"],
                                                         evidence["attention"]["oldest"]["item_id"]))
        self.assertFalse(self.delivered("first held dispatch"))
        preview = self.fleet.cli("needs", "admission", "--root", str(self.fleet.root.path))[1]["evidence"]
        self.assertEqual("hold", preview["admission"])
        # MCP binds the send repository to the server's working directory.
        self.addCleanup(os.chdir, Path.cwd())
        os.chdir(self.repository)
        mcp = McpServer(str(self.fleet.root.path), FILER, "session-a").call_tool("send", {
            "recipient": OTHER, "repo": "floati", "sha": self.sha, "doc": "note.md",
            "note": "first held dispatch", "idempotency_key": "send-1"})
        self.assertTrue(mcp.get("isError"))
        self.assertEqual(evidence["hold"]["id"], mcp["structuredContent"]["evidence"]["hold"]["id"])
        # Any send that is not a reply is a new dispatch, an fyi one included.
        code, fyi = self.send("fyi while over budget", "send-fyi", "--message-class", "fyi")
        self.assertEqual((20, "dispatch_held_for_attention"), (code, fyi["evidence"]["code"]))
        self.assertEqual(["held", "held"], [row["phase"] for row in self.holds()])
        # The queue falls within budget: the filer closes one review.
        _filed(self, self.fleet.cli("needs", "close", "--root", str(self.fleet.root.path), "--as", FILER,
                                    "--item", self.reviews[1]["id"], "--reason", "done",
                                    "--idempotency-key", "close-1"))
        _filed(self, self.send("first held dispatch", "send-1"))
        self.assertTrue(self.delivered("first held dispatch"))
        self.assertEqual([("held", None), ("held", None), ("released", "under_budget")],
                         [(row["phase"], row["release_cause"]) for row in self.holds()])

    def test_the_persons_release_lets_exactly_the_held_dispatch_go(self) -> None:
        self.budget("--limit", "1", "--hold-dispatches", "--idempotency-key", "budget-1")
        code, second = self.send("second held dispatch", "send-2")
        self.assertEqual((20, "dispatch_held_for_attention"), (code, second["evidence"]["code"]))
        release = floati_argv("needs", "release", "--root", str(self.fleet.root.path),
                              "--hold", second["evidence"]["hold"]["id"], "--idempotency-key", "release-1")
        code, refused = self.without_terminal(release, cwd=self.neutral)
        self.assertEqual((20, "needs_human_act_unavailable"), (code, refused.get("evidence", {}).get("code")))
        code, released, screen = self.at_terminal(release, cwd=self.neutral, answer=lambda shown: shown)
        self.assertEqual(0, code, screen)
        self.assertEqual(("released", "owner", "controlling_terminal"),
                         (released["evidence"]["phase"], released["evidence"]["release_cause"],
                          released["evidence"]["human_act"]["channel"]))
        _filed(self, self.send("second held dispatch", "send-2"))
        self.assertTrue(self.delivered("second held dispatch"))
        self.assertEqual([], self.fleet.show()["held_dispatches"])
        # The release named one request: a different dispatch is still held.
        code, other = self.send("a different dispatch", "send-3")
        self.assertEqual((20, "dispatch_held_for_attention"), (code, other["evidence"]["code"]))
        self.assertNotEqual(second["evidence"]["hold"]["id"], other["evidence"]["hold"]["id"])
        self.assertEqual(["held", "released", "dispatched", "held"], [row["phase"] for row in self.holds()])


if __name__ == "__main__":
    unittest.main()
