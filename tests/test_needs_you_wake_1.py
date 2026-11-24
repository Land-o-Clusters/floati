"""NEEDS-YOU-OBSERVED-1, second series: a seat's Stop waiter wakes when an observed item arrives.

The ruling (architect, 2026-11-22): a ``needs-you`` condition on the Codex Stop
waiter's existing tick. A seat declares its orchestrator source in one fixed
per-node file in the root; with no file the condition does not exist and the
waiter is byte-identical to before. The pull runs at most once per interval
(60 s by default, validated), keeps the pull's deadline and output bound, and
a failed or degraded pull never wakes the seat and never stops the waiter. A
wake is the waiter's own receipted terminal exit, reason ``observed_item``,
and it names the observed item's event key and its coordinate. "New" means new
to this seat: a seat is never woken twice for one key, a second seat still
wakes for a key the first one saw, and a second completion is a new key.

A new module because the waiter's own modules own mail wakes and none of them
has an orchestrator fixture; the vocabulary case extends the precedent pinned
in tests/test_f2_superseded_waiter.py, which added the previous exit reason.
"""

from __future__ import annotations

import copy
import re
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  Pin HOME before any product import resolves a path.

from floati import fixture_ids as public_ids
from floati.jsonl import read_records_snapshot
from floati.mcp import run_cli_artifact
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_needs_you_observed_1 import PROJECT, Orchestrator, thread
from tests.waiter_timing import scripted_readiness


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXIT_SCHEMA = REPOSITORY_ROOT / "schemas/v1/wake-waiter-exit-receipt.schema.json"
HEALTH_SCHEMA = REPOSITORY_ROOT / "schemas/v1/wake-health-fact.schema.json"
EXIT_BEFORE = REPOSITORY_ROOT / "tests/fixtures/needs-you-wake-exit-v1-before.schema.json"
HEALTH_BEFORE = REPOSITORY_ROOT / "tests/fixtures/needs-you-wake-health-v1-before.schema.json"
EXHAUSTED = "(floati: wait deadline exhausted; end this turn to re-arm)"


class Fleet:
    """One root whose seats each have a mapped workspace and armed waiter consent."""

    def __init__(self, base: Path, nodes: tuple) -> None:
        self.base = base
        self.bus_home = base / "fleet"
        self.root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        registry = Registry(self.root)
        self.workspaces = {}
        for node in nodes:
            registry.register(node, "worker")
            self.workspaces[node] = base / ("workspace-" + node)
            self.workspaces[node].mkdir()
        path = self.bus_home / "codex-wait" / "workspaces.v0.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"schema_version": 0, "tenant_id": self.root.tenant_id, "mappings": [
            {"workspace": str(self.workspaces[node]), "node_id": node} for node in nodes]},
            sort_keys=True, separators=(",", ":")) + "\n")
        from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
        for node in nodes:
            participant = resolve_participant(self.bus_home, self.workspaces[node])
            CodexWaitConsentLedger(self.root).arm(participant.binding, hook_timeout_seconds=10,
                                                  wait_deadline_seconds=2, idempotency_key="consent-" + node)
        self.orchestrator = Orchestrator(base)
        self.wall = time.time()

    def declare(self, node: str, **change: object) -> Path:
        """The seat's source declaration, at the fixed per-node path in the root."""
        path = self.bus_home / "config" / "needs-observed" / (node + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "orchestrator": "t3-code",
                                    "cli": str(self.orchestrator.cli), "home": str(self.orchestrator.home),
                                    "project": PROJECT, **change}))
        return path

    def wait(self, node: str, *, after: float = 0.0, session: str = "thread-live", output=None):
        """One Stop of this seat, `after` seconds of wall clock past the fleet's start."""
        from floati.codex_wait import run_stop_waiter
        self.wall += after
        clock = [0.0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        stdout, stderr, result = (io.StringIO() if output is None else output), io.StringIO(), {}
        status = run_stop_waiter(
            bus_home=self.bus_home, hook_payload={"cwd": str(self.workspaces[node]), "session_id": session},
            stdout=stdout, stderr=stderr, monotonic=lambda: clock[0], sleep=sleep,
            wall_time=lambda: self.wall + clock[0], poll_interval_seconds=1.0,
            wait_readable=scripted_readiness(sleep, interval=1.0), _waiter_result=result)
        return status, stdout.getvalue(), stderr.getvalue(), result

    def exits(self, node: str) -> list:
        return read_records_snapshot(self.root, "receipts/wake-waiter-exit/" + node + ".jsonl",
                                     allowed_kinds={"wake_waiter_exit_receipt"})

    def wakes(self, node: str) -> list:
        from floati.needs_records import NEEDS_KINDS
        rows = read_records_snapshot(self.root, "receipts/needs/ledger.jsonl", allowed_kinds=set(NEEDS_KINDS))
        return [row for row in rows if row["kind"] == "needs_wake" and row["actor"] == node]

    def observed(self) -> dict:
        code, artifact = run_cli_artifact(["needs", "show", "--root", str(self.root.path), "--all"])
        assert code == 0, artifact
        return artifact["evidence"]


SEAT = public_ids.builder("floati")
OTHER = public_ids.builder("second")


class WaiterConditionCase(unittest.TestCase):
    nodes = (SEAT,)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.fleet = Fleet(self.base, self.nodes)

    def woke(self, outcome, node: str = SEAT) -> dict:
        status, stdout, stderr, result = outcome
        self.assertEqual((0, "observed_item"), (status, result.get("kind")), "stderr=%r stdout=%r" % (stderr, stdout))
        decision = json.loads(stdout)
        self.assertEqual("block", decision["decision"])
        return decision

    def slept(self, outcome) -> None:
        status, stdout, stderr, result = outcome
        self.assertEqual((0, "exhausted"), (status, result.get("kind")), "stderr=%r stdout=%r" % (stderr, stdout))
        self.assertEqual({"decision": "block", "reason": EXHAUSTED}, json.loads(stdout))


class ObservedWakeTests(WaiterConditionCase):
    def test_an_observed_item_wakes_the_seat_through_a_receipted_terminal_exit(self) -> None:
        self.fleet.declare(SEAT)
        self.fleet.orchestrator.threads([thread("t-1", "completed", "run-1")])
        decision = self.woke(self.fleet.wait(SEAT))
        (item,) = [entry["record"] for entry in self.fleet.observed()["records"]
                   if entry["source"]["type"] == "observed"]
        self.assertIn(item["event_key"], decision["reason"])
        self.assertIn(json.dumps(item["coordinates"][0], sort_keys=True), decision["reason"])
        (exit_row,) = self.fleet.exits(SEAT)
        self.assertEqual("observed_item", exit_row["reason_code"])
        validate_json_schema(exit_row, EXIT_SCHEMA)
        (wake,) = self.fleet.wakes(SEAT)
        self.assertEqual([{"item_id": item["id"], "event_key": item["event_key"],
                           "coordinate": item["coordinates"][0]}], wake["items"])
        from floati.wake_health import WakeHealthProjection
        from datetime import datetime, timezone
        fact = WakeHealthProjection(self.fleet.root).fact(SEAT, datetime.now(timezone.utc))
        self.assertEqual("observed_item", fact["waiter_exit_reason"])
        validate_json_schema(fact, HEALTH_SCHEMA)
        self.assertEqual("unread", next(entry["read"]["state"] for entry in self.fleet.observed()["records"]
                                        if entry["record"]["id"] == item["id"]),
                         "waking a seat is not a person reading the item")

    def test_failed_hook_flush_leaves_the_observed_item_eligible_for_retry(self) -> None:
        class ClosedPipe(io.StringIO):
            def flush(self):
                raise BrokenPipeError

        self.fleet.declare(SEAT)
        self.fleet.orchestrator.threads([thread("t-1", "completed")])
        try:
            self.fleet.wait(SEAT, output=ClosedPipe())
        except BrokenPipeError:
            pass
        self.woke(self.fleet.wait(SEAT, after=61))
        self.assertEqual(1, len(self.fleet.wakes(SEAT)))

    def test_a_read_close_or_supersession_during_selection_prevents_a_stale_wake(self) -> None:
        from floati.needs_inbox import close_item, mark_read
        from floati.needs_observed import observe
        from floati.needs_you_condition import NeedsYouCondition

        select = NeedsYouCondition.pending
        for change in ("read", "close", "supersede"):
            with self.subTest(change=change):
                base = self.base / change
                base.mkdir()
                fleet = Fleet(base, (SEAT,))
                root = fleet.root
                source = fleet.declare(SEAT)
                fleet.orchestrator.threads([thread(change, "completed")])

                def select_then_change(condition):
                    candidates = select(condition)
                    (item,) = candidates
                    if change == "read":
                        from floati.needs_inbox import _CODE_LENGTH
                        with mock.patch("floati.needs_inbox._terminal", return_value=os.open(os.devnull, os.O_RDWR)), (
                            mock.patch("floati.needs_inbox.secrets.choice", return_value="A")
                        ), mock.patch("floati.needs_inbox._answer", return_value="A" * _CODE_LENGTH):
                            mark_read(root, [item["item_id"]], "read-during-selection", cwd=base)
                    elif change == "close":
                        close_item(root, SEAT, item["item_id"], "Reviewed", "close-during-selection")
                    else:
                        fleet.orchestrator.threads([thread(change, "completed", "run-2")])
                        observe(root, SEAT, str(source))
                    return candidates

                with mock.patch.object(NeedsYouCondition, "pending", select_then_change):
                    output = fleet.wait(SEAT)
                self.slept(output)
                self.assertEqual([], fleet.wakes(SEAT))

    def test_withdrawn_workspace_authority_prevents_a_stop_waiter_pull(self) -> None:
        from floati.needs_you_condition import NeedsYouCondition

        self.fleet.declare(SEAT)
        self.fleet.orchestrator.threads([thread("t-1", "completed")])
        mapping = self.fleet.bus_home / "codex-wait" / "workspaces.v0.json"
        for_seat = NeedsYouCondition.for_seat

        def withdraw_before_tick(*args):
            condition = for_seat(*args)
            document = json.loads(mapping.read_text())
            document["mappings"] = []
            mapping.write_text(json.dumps(document))
            return condition

        with mock.patch.object(NeedsYouCondition, "for_seat", side_effect=withdraw_before_tick):
            self.fleet.wait(SEAT)
        self.assertEqual([], self.fleet.orchestrator.calls())
        self.assertEqual([], self.fleet.observed()["records"])

    def test_missing_claim_prevents_an_observer_launch_after_participation(self) -> None:
        from floati.codex_wait_contract import CodexWaitSessionLedger
        from floati.needs_you_condition import NeedsYouCondition

        self.fleet.declare(SEAT)
        self.fleet.orchestrator.threads([thread("t-1", "completed")])
        claim = self.fleet.root.path / CodexWaitSessionLedger._relative(SEAT)
        for_seat = NeedsYouCondition.for_seat
        unclaimed_pulls = []

        def lose_claim_before_tick(*args):
            condition = for_seat(*args)
            claim.unlink()
            return condition

        def track_claim(argv, *args, **kwargs):
            if argv[0] == str(self.fleet.orchestrator.cli) and not claim.exists():
                unclaimed_pulls.append(argv)
            return popen(argv, *args, **kwargs)

        import subprocess
        popen = subprocess.Popen
        with mock.patch.object(NeedsYouCondition, "for_seat", side_effect=lose_claim_before_tick), (
            mock.patch("floati.needs_observed.subprocess.Popen", side_effect=track_claim)
        ):
            self.fleet.wait(SEAT)
        self.assertEqual([], unclaimed_pulls)

    def test_workspace_authority_lost_during_a_pull_prevents_observation_records(self) -> None:
        from floati import needs_observed

        self.fleet.declare(SEAT)
        self.fleet.orchestrator.threads([thread("t-1", "completed")])
        mapping = self.fleet.bus_home / "codex-wait" / "workspaces.v0.json"
        run = needs_observed._run

        def withdraw_after_read(*args, **kwargs):
            output = run(*args, **kwargs)
            document = json.loads(mapping.read_text())
            document["mappings"] = []
            mapping.write_text(json.dumps(document))
            return output

        with mock.patch.object(needs_observed, "_run", side_effect=withdraw_after_read):
            self.fleet.wait(SEAT)
        self.assertEqual(1, len(self.fleet.orchestrator.calls()))
        self.assertEqual([], self.fleet.observed()["records"])
        self.assertEqual("none_recorded", self.fleet.observed()["observed_sources"]["state"])

    def test_a_seat_is_woken_once_per_key_and_again_for_a_second_completion(self) -> None:
        self.fleet.declare(SEAT)
        self.fleet.orchestrator.threads([thread("t-1", "completed", "run-1")])
        first = self.woke(self.fleet.wait(SEAT))
        self.slept(self.fleet.wait(SEAT, after=61))
        self.fleet.orchestrator.threads([thread("t-1", "completed", "run-2")])
        second = self.woke(self.fleet.wait(SEAT, after=61))
        keys = [wake["items"][0]["event_key"] for wake in self.fleet.wakes(SEAT)]
        self.assertEqual(2, len(set(keys)), keys)
        self.assertIn(keys[1], second["reason"])
        self.assertNotIn(keys[1], first["reason"])

    def test_the_pull_is_rate_limited_and_a_degraded_pull_never_wakes_or_stops_the_waiter(self) -> None:
        self.fleet.declare(SEAT)
        self.fleet.orchestrator.threads([thread("t-1", "completed", "run-1")])
        (self.fleet.orchestrator.home / "exit").write_text("3")
        self.slept(self.fleet.wait(SEAT))
        (source,) = self.fleet.observed()["observed_sources"]["sources"]
        self.assertEqual(("unknown", "source_exit_nonzero"), (source["outcome"], source["cause"]["code"]))
        (self.fleet.orchestrator.home / "exit").unlink()
        calls = len(self.fleet.orchestrator.calls())
        self.slept(self.fleet.wait(SEAT, after=10))
        self.assertEqual(calls, len(self.fleet.orchestrator.calls()), "no second pull inside the interval")
        self.woke(self.fleet.wait(SEAT, after=51))
        self.assertEqual(calls + 1, len(self.fleet.orchestrator.calls()))

    def test_rearmed_waiters_use_the_declared_pull_interval(self) -> None:
        for interval, after, due in ((30, 31, True), (3600, 61, False)):
            with self.subTest(interval=interval):
                base = self.base / str(interval)
                base.mkdir()
                fleet = Fleet(base, (SEAT,))
                fleet.declare(SEAT, interval_seconds=interval)
                self.slept(fleet.wait(SEAT))
                fleet.orchestrator.threads([thread("t-1", "completed")])
                outcome = fleet.wait(SEAT, after=after)
                (self.woke if due else self.slept)(outcome)
                self.assertEqual(1 + int(due), len(fleet.orchestrator.calls()))

    def test_an_invalid_interval_is_refused_and_never_wakes(self) -> None:
        for label, interval in (("too_short", 5), ("too_long", 86400), ("not_a_number", "60")):
            with self.subTest(interval=label):
                self.fleet.declare(SEAT, interval_seconds=interval)
                self.fleet.orchestrator.threads([thread("t-1", "completed", "run-1")])
                self.slept(self.fleet.wait(SEAT, after=3600))
                self.assertEqual([], self.fleet.orchestrator.calls())
                code, artifact = run_cli_artifact(["needs", "observe", "--root", str(self.fleet.root.path),
                                                   "--as", SEAT, "--source-config",
                                                   str(self.fleet.declare(SEAT, interval_seconds=interval))])
                self.assertEqual((20, "needs_source_config_invalid"), (code, artifact["evidence"]["code"]))
        self.fleet.declare(SEAT, interval_seconds=30)
        self.woke(self.fleet.wait(SEAT, after=3600))


class SecondSeatTests(WaiterConditionCase):
    nodes = (SEAT, OTHER)

    def test_a_second_seat_wakes_for_a_key_the_first_seat_already_saw(self) -> None:
        self.fleet.declare(SEAT)
        self.fleet.declare(OTHER)
        self.fleet.orchestrator.threads([thread("t-1", "completed", "run-1")])
        self.woke(self.fleet.wait(SEAT))
        self.woke(self.fleet.wait(OTHER, after=1), node=OTHER)
        self.assertEqual([wake["items"][0]["event_key"] for wake in self.fleet.wakes(SEAT)],
                         [wake["items"][0]["event_key"] for wake in self.fleet.wakes(OTHER)])
        self.slept(self.fleet.wait(OTHER, after=61))


class NoSourceTests(unittest.TestCase):
    def test_without_a_source_file_the_condition_does_not_exist_and_the_waiter_is_byte_identical(self) -> None:
        from floati import needs_observed
        outcomes = {}
        for label in ("today", "no_source_file"):
            temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
            self.addCleanup(temporary.cleanup)
            fleet = Fleet(Path(temporary.name).resolve(), (SEAT,))
            fleet.orchestrator.threads([thread("t-1", "completed", "run-1")])
            before = {path.relative_to(fleet.bus_home).as_posix() for path in fleet.bus_home.rglob("*")}
            with mock.patch.object(needs_observed, "observe", side_effect=AssertionError("pulled")):
                if label == "today":
                    with mock.patch("floati.needs_you_condition.NeedsYouCondition.for_seat", return_value=None):
                        status, stdout, stderr, result = fleet.wait(SEAT)
                else:
                    status, stdout, stderr, result = fleet.wait(SEAT)
            after = {path.relative_to(fleet.bus_home).as_posix() for path in fleet.bus_home.rglob("*")}
            written = sorted(after - before)
            self.assertFalse([path for path in written if "needs" in path], written)
            # Two fleets differ only in identifier-derived names (shards, digests).
            outcomes[label] = (status, stdout, stderr, result.get("kind"),
                               sorted(re.sub(r"[0-9a-f]{4,}", "#", path) for path in written))
            self.assertEqual([], fleet.orchestrator.calls())
        self.assertEqual(outcomes["today"], outcomes["no_source_file"])
        self.assertEqual((0, json.dumps({"decision": "block", "reason": EXHAUSTED}) + "\n"), outcomes["today"][:2])


class ExitReasonVocabularyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.fleet = Fleet(Path(temporary.name).resolve(), (SEAT,))

    def test_the_schemas_widen_only_by_the_observed_item_wake(self) -> None:
        for before, now in ((EXIT_BEFORE, EXIT_SCHEMA), (HEALTH_BEFORE, HEALTH_SCHEMA)):
            with self.subTest(schema=now.name):
                expected = copy.deepcopy(json.loads(before.read_bytes()))
                if now == EXIT_SCHEMA:
                    expected["properties"]["reason_code"]["enum"].append("observed_item")
                else:
                    expected["properties"]["waiter_exit_reason"]["enum"].append("observed_item")
                    # The Cursor waiter's journaled publication of the same wake.
                    expected["$defs"]["recent_cursor_record"]["properties"]["event"]["enum"].append(
                        "followup_observed_item")
                self.assertEqual(expected, json.loads(now.read_bytes()))

    def test_every_reader_of_the_exit_reason_handles_every_reason_and_refuses_an_unruled_one(self) -> None:
        from datetime import datetime, timezone
        from floati.errors import IntegrityFailure, ProtocolRefusal
        from floati.records import validate_record
        from floati.wake_exit import WAKE_EXIT_REASONS, WakeExitLedger
        from floati.wake_health import WakeHealthProjection

        exit_enum = json.loads(EXIT_SCHEMA.read_bytes())["properties"]["reason_code"]["enum"]
        health_enum = json.loads(HEALTH_SCHEMA.read_bytes())["properties"]["waiter_exit_reason"]["enum"]
        self.assertIn("observed_item", WAKE_EXIT_REASONS)
        self.assertEqual(set(WAKE_EXIT_REASONS), set(exit_enum))
        self.assertEqual(set(WAKE_EXIT_REASONS) | {None}, set(health_enum))
        digest = hashlib.sha256(b"seat").hexdigest()
        for reason in sorted(WAKE_EXIT_REASONS):
            with self.subTest(reason=reason):
                row = WakeExitLedger(self.fleet.root).record(node_id=SEAT, session_digest=digest, reason_code=reason,
                                                             waited_seconds=1, idempotency_key="vocabulary-" + reason)
                validate_record(row, self.fleet.root.tenant_id, {"wake_waiter_exit_receipt"}, integrity=False)
                validate_json_schema(row, EXIT_SCHEMA)
                fact = WakeHealthProjection(self.fleet.root).fact(SEAT, datetime.now(timezone.utc))
                self.assertEqual(reason, fact["waiter_exit_reason"])
                validate_json_schema(fact, HEALTH_SCHEMA)
        with self.assertRaises(ProtocolRefusal):
            WakeExitLedger(self.fleet.root).record(node_id=SEAT, session_digest=digest, reason_code="unruled_reason",
                                                   waited_seconds=1, idempotency_key="vocabulary-unruled")
        ledger = self.fleet.bus_home / "receipts" / "wake-waiter-exit" / (SEAT + ".jsonl")
        planted = dict(json.loads(ledger.read_text().splitlines()[-1]), reason_code="unruled_reason")
        planted["id"] = planted["id"][:-1] + ("0" if planted["id"][-1] != "0" else "1")
        with ledger.open("a") as handle:
            handle.write(json.dumps(planted, sort_keys=True, separators=(",", ":")) + "\n")
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)) as caught:
            WakeHealthProjection(self.fleet.root).fact(SEAT, datetime.now(timezone.utc))
        self.assertEqual("reason_code_invalid", caught.exception.code, "the reader names the unruled reason")


class CursorWaiterTests(unittest.TestCase):
    """The same condition on the Cursor Stop waiter (cursor_wait), on its native read path."""

    def setUp(self) -> None:
        from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
        from floati.waiter_consent import arm_waiter_consent
        from tests import test_f2_readiness_hold as readiness
        self.f = readiness.NativeStopHoldReadinessTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.fixture("Cursor")
        self.root, self.node = self.f.root, self.f.node
        arm_waiter_consent(self.root.path, self.f.workspace, self.node, "Cursor", 30, 20)
        consent = CodexWaitConsentLedger(self.root).require_armed(self.f.binding)
        CodexWaitSessionLedger(self.root).arm(self.f.binding, consent, self.f.session,
                                              idempotency_key="needs-you-cursor-claim")
        scratch = Path(tempfile.mkdtemp(dir=REAL_TEMP_ROOT)).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(scratch, ignore_errors=True))
        self.orchestrator = Orchestrator(scratch)
        self.wall = time.time()
        from floati import cursor_wait
        self.journal = cursor_wait._journal_path(self.root.path, self.node)

    def declare(self) -> None:
        path = self.root.path / "config" / "needs-observed" / (self.node + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "orchestrator": "t3-code", "cli": str(self.orchestrator.cli),
                                    "home": str(self.orchestrator.home), "project": PROJECT}))

    def wait(self, *, after: float = 0.0):
        from floati import cursor_wait
        self.wall += after
        elapsed = [0.0]

        def sleep(seconds: float) -> None:
            elapsed[0] += seconds

        output = io.StringIO()
        with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.f.workspace),\
                mock.patch("floati.cursor_wait._needs_you_wall", lambda: self.wall + elapsed[0]):
            cursor_wait.run_cursor_stop_wait(
                root=self.root.path, node=self.node, runtime=self.f.runtime, deadline_seconds=20, poll_seconds=1,
                hook_timeout_seconds=30,
                payload={"status": "completed", "conversation_id": self.f.session, "loop_count": 1,
                         "cwd": str(self.f.workspace)},
                stdout=output, stderr=io.StringIO(), clock=lambda: elapsed[0], sleep=sleep,
                wait_readable=scripted_readiness(sleep, interval=1), last_seen_root=self.f.workspace)
        rows = [json.loads(line) for line in self.journal.read_text().splitlines()]
        return json.loads(output.getvalue()), rows[-1]

    def test_a_declared_cursor_seat_wakes_once_per_key_through_a_journaled_followup(self) -> None:
        from datetime import datetime, timedelta, timezone
        from floati.wake_health import _cursor_publications
        self.declare()
        self.orchestrator.threads([thread("t-1", "completed", "run-1")])
        body, row = self.wait()
        self.assertEqual(("followup_observed_item", "observed_item"), (row["event"], row["reason_code"]))
        (item,) = [entry["record"] for entry in run_cli_artifact(
            ["needs", "show", "--root", str(self.root.path)])[1]["evidence"]["records"]]
        self.assertIn(item["event_key"], body["followup_message"])
        self.assertIn(json.dumps(item["coordinates"][0], sort_keys=True), body["followup_message"])
        body, row = self.wait(after=61)
        self.assertEqual("followup_rearm", row["event"], "a seat is never woken twice for one key")
        self.orchestrator.threads([thread("t-1", "completed", "run-2")])
        body, row = self.wait(after=61)
        self.assertEqual("followup_observed_item", row["event"], "a second completion is a new key")
        now = datetime.now(timezone.utc)
        publications = _cursor_publications(self.root, self.node, now - timedelta(days=1), now + timedelta(days=1))
        self.assertEqual(("complete_read", 0), (publications["coverage"], publications["invalid_row_count"]),
                         "the wake-health reader names the new publication event")

    def test_without_a_source_file_the_cursor_waiter_is_unchanged(self) -> None:
        from floati import needs_observed
        self.orchestrator.threads([thread("t-1", "completed", "run-1")])
        outcomes = []
        for patched in (True, False):
            with mock.patch.object(needs_observed, "observe", side_effect=AssertionError("pulled")):
                if patched:
                    with mock.patch("floati.needs_you_condition.NeedsYouCondition.for_seat", return_value=None):
                        body, row = self.wait()
                else:
                    body, row = self.wait(after=61)
            outcomes.append((body, row["event"]))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual("followup_rearm", outcomes[0][1])
        self.assertFalse(any("needs" in path.relative_to(self.root.path).as_posix()
                             for path in self.root.path.rglob("*")))
        self.assertEqual([], self.orchestrator.calls())


if __name__ == "__main__":
    unittest.main()
