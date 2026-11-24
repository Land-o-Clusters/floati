"""CURSOR-HOOK-REARM-DEATH-1 RED: the chain budget and the quiet pipe.

The architect's 2026-10-08 gate ruling, §14, on the owner's report ("grok
wake keeps dying, it does a couple 25 min
rearms then the wake dies"): the Cursor hook timeout runs from the turn-end
invocation's start, while the waiter's own deadline re-arms a second wait
inside the same chain — so the second wait outlives the harness and the
follow-up write meets a closed pipe (`followup_rearm` then BrokenPipeError,
journal shape of both grok deaths). The property: every wait, re-arm
included, ends inside the invocation's remaining budget, and a closed pipe
is journaled AND leaves the seat re-armable on its next stop instead of
deaf.

Both REDs run on the scratch native readiness fixture — a scratch root, an
injected clock, a scratch hook journal — never a live seat's config. At
main the chain tests show the second wait re-spending the full deadline
(the property violated: the wait outlives the budget), and the pipe test
shows the BrokenPipeError escaping the waiter (the traceback that deafened
the seat).
"""

from __future__ import annotations

import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import tests  # Pin HOME before importing any product default.

from floati import cursor_wait
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.registry import Registry
from floati.waiter_consent import arm_waiter_consent
from tests import test_f2_readiness_hold as readiness
from tests.waiter_timing import scripted_readiness


HOOK_TIMEOUT = 30
CONSENT_DEADLINE = 20
DEADLINE = 20
POLL = 1


class DeadPipeOutput(io.StringIO):
    """A stdout whose pipe the harness has already closed (real EPIPE)."""

    def __init__(self):
        super().__init__()
        self.writes = 0

    def write(self, value):
        self.writes += 1
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        return self


class CursorHookRearmDeathTests(unittest.TestCase):
    def setUp(self):
        self.f = readiness.NativeStopHoldReadinessTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.fixture("Cursor")
        self.root, self.node = self.f.root, self.f.node
        arm_waiter_consent(self.root.path, self.f.workspace, self.node,
            "Cursor", HOOK_TIMEOUT, CONSENT_DEADLINE)
        self.consent = CodexWaitConsentLedger(self.root).require_armed(self.f.binding)
        self.assertEqual("cursor", Registry(self.root).require_active(self.node)["role"].casefold())
        CodexWaitSessionLedger(self.root).arm(self.f.binding, self.consent,
            self.f.session, idempotency_key="rearm-death-claim")
        self.elapsed = 0.0
        self.sleeps = []
        self.epoch = datetime.now(timezone.utc)
        self.errors = io.StringIO()
        self.journal = cursor_wait._journal_path(self.root.path, self.node)

    def stamp(self, offset_seconds):
        moment = self.epoch + timedelta(seconds=offset_seconds)
        return moment.isoformat().replace("+00:00", "Z")

    def seed_chain_row(self, *, waited, seconds_ago):
        """The grok journal shape: a prior followup_rearm for this conversation."""

        self.journal.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": self.stamp(-seconds_ago),
            "event": "followup_rearm",
            "node": self.node,
            "status": "completed",
            "loop_count": 0,
            "conversation_id": self.f.session,
            "waited_seconds": waited,
            "chain_wait_seconds": waited,
        }
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def journal_rows(self):
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines()]

    def run_waiter(self, output, *, deadline=DEADLINE):
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.elapsed += seconds

        with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.f.workspace):
            return cursor_wait.run_cursor_stop_wait(root=self.root.path, node=self.node,
                runtime=self.f.runtime, deadline_seconds=deadline, poll_seconds=POLL,
                hook_timeout_seconds=HOOK_TIMEOUT,
                payload={"status": "completed", "conversation_id": self.f.session,
                         "loop_count": 1, "cwd": str(self.f.workspace)},
                stdout=output, stderr=self.errors, clock=lambda: self.elapsed,
                sleep=sleep, wait_readable=scripted_readiness(sleep, interval=POLL),
                now_utc=lambda: self.stamp(self.elapsed),
                last_seen_root=self.f.workspace)

    def test_rearm_continuation_waits_only_the_chain_budget_remaining(self):
        """A second wait re-spending the full deadline outlives the harness."""

        self.seed_chain_row(waited=10.0, seconds_ago=2)
        # Chain budget: 30 - 5 margin - 1 poll - (10 waited + 2 gap) = 12 s.
        output = io.StringIO()
        self.run_waiter(output)
        self.assertLessEqual(
            sum(self.sleeps), 12.0,
            "the wait after a re-arm spent %s s of a 12 s chain budget" % sum(self.sleeps))
        row = self.journal_rows()[-1]
        self.assertEqual("followup_rearm", row["event"])
        self.assertLessEqual(row["waited_seconds"], 12.0)

    def test_exhausted_chain_budget_ends_quietly_without_another_wait(self):
        """No budget, no wait: an empty exit the seat's next stop can re-arm."""

        self.seed_chain_row(waited=22.0, seconds_ago=2)
        # Chain budget: 30 - 5 margin - 1 poll - (22 waited + 2 gap) = 0 s.
        output = io.StringIO()
        self.run_waiter(output)
        self.assertEqual(
            [], self.sleeps,
            "the waiter held again with the chain budget already spent")
        row = self.journal_rows()[-1]
        self.assertEqual("exit_empty_chain_budget", row["event"])

    def test_closed_pipe_returns_quietly_and_journals_the_publication(self):
        """A closed pipe is typed evidence, never an escaping traceback."""

        self.f.send("rearm-death-mail")
        output = DeadPipeOutput()
        result = self.run_waiter(output)
        self.assertEqual(0, result)
        self.assertEqual(1, output.writes)
        rows = self.journal_rows()
        self.assertTrue(rows, "the closed pipe left no journal row at all")
        last = rows[-1]
        self.assertEqual("failed", last.get("publication"))
        self.assertEqual("BrokenPipeError", last.get("exception_type"))
        self.assertEqual("cursor_followup_publication_failed", last.get("reason_code"))


if __name__ == "__main__":
    unittest.main()
