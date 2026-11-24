"""NET-ASK-1: one local command correlates a question with its answer.

The command writes the question on this machine's ledger, waits a bounded
time, and returns the linked answer or decline. A timeout and an unreadable
answer are typed UNKNOWN. The outcome answered is printed only with the
answer record's id. Nothing listens and nothing leaves the machine.
"""

from __future__ import annotations

import io
from tests.readiness import READY_SECONDS, Readiness

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

from floati.bus_epoch import roll_automatic_epoch, roll_bus_epoch
from floati.cli import main
from floati.epoch_carry import build_carry_plan
from floati.events import EVENT_KINDS
from floati.framing import encode_frame
from floati.jsonl import read_records
from floati.planes import AuthorityGrantStore
from floati.registry import utc_now
from floati.root import resolve_command_root
from floati import wake_timeout_forensics
from tests.banked_sha_fixture import banked_send_repository
from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UNREADABLE_ANSWER_ID = "ask-answer-018f7e9b3c117abc8def0123456789ab"


class NetAsk1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.fleet = self.base / "fleet"
        self.repository, self.sha = banked_send_repository(self.base)
        self.initialize_fleet()

    def initialize_fleet(self) -> None:
        initialized, _artifact = self.invoke_in_process("init", "--root", str(self.fleet))
        self.assertEqual(0, initialized.returncode, initialized.stdout)
        for node in ("red", "blue"):
            added, artifact = self.invoke_in_process(
                "node", "add", "--root", str(self.fleet), "--node", node,
                "--harness", "Codex", "--lifetime", "permanent",
            )
            self.assertEqual(0, added.returncode, artifact)

    def environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(
            value for value in (str(REPOSITORY_ROOT), existing) if value
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["HOME"] = str(self.home)
        return environment

    def invoke(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        completed = subprocess.run(
            ["/usr/bin/python3", "-B", "-m", "floati", *arguments],
            cwd=str(self.repository),
            env=self.environment(),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual("", completed.stderr, completed.stderr)
        self.assertTrue(completed.stdout.endswith("\n"), completed.stdout)
        self.assertEqual(1, len(completed.stdout.splitlines()), completed.stdout)
        return completed, json.loads(completed.stdout)

    def invoke_in_process(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        previous_cwd = Path.cwd()
        with mock.patch.dict(os.environ, self.environment(), clear=True),\
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                os.chdir(self.repository)
                status = main(list(arguments))
            finally:
                os.chdir(previous_cwd)
        completed = subprocess.CompletedProcess(
            list(arguments), status, stdout.getvalue(), stderr.getvalue(),
        )
        self.assertEqual("", completed.stderr, completed.stderr)
        self.assertTrue(completed.stdout.endswith("\n"), completed.stdout)
        self.assertEqual(1, len(completed.stdout.splitlines()), completed.stdout)
        return completed, json.loads(completed.stdout)

    def spawn(self, *arguments: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            ["/usr/bin/python3", "-B", "-m", "floati", *arguments],
            cwd=str(self.repository),
            env=self.environment(),
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.reap, process)
        return process

    def reap(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    def finish(self, process: subprocess.Popen[str], *, timeout: float) -> dict:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.reap(process)
            self.fail("ask did not return before the test budget")
        self.assertEqual("", stderr, stderr)
        self.assertTrue(stdout.endswith("\n"), stdout)
        self.assertEqual(1, len(stdout.splitlines()), stdout)
        return json.loads(stdout)

    def question_argv(self, *, note: str, key: str, wait: int) -> list[str]:
        return [
            "ask", "--root", str(self.fleet),
            "--from", "red", "--to", "blue",
            "--repo", "floati", "--sha", self.sha,
            "--repository", str(self.repository),
            "--doc", "note.md", "--note", note,
            "--session", "sess-red", "--idempotency-key", key,
            "--wait-seconds", str(wait),
        ]

    def reply_argv(
        self, question_id: str, *, disposition: str, key: str,
        note: str = "", reason: str = "",
    ) -> list[str]:
        arguments = [
            "ask", "--root", str(self.fleet),
            "--from", "blue", "--to", "red",
            "--reply-to", question_id,
            "--disposition", disposition,
            "--repo", "floati", "--sha", self.sha,
            "--repository", str(self.repository),
            "--doc", "note.md",
            "--session", "sess-blue", "--idempotency-key", key,
        ]
        if note:
            arguments.extend(["--note", note])
        if reason:
            arguments.extend(["--reason", reason])
        return arguments

    def events(self) -> list[dict]:
        root = resolve_command_root(str(self.fleet))
        return read_records(root, "events.jsonl", allowed_kinds=set(EVENT_KINDS))

    def wait_for_question(self, process: subprocess.Popen[str]) -> dict:
        readiness = Readiness("ask question publication")
        while True:
            readiness.remaining()
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"ask exited {process.returncode} before recording a question: "
                    f"{stdout}{stderr}"
                )
            try:
                rows = self.events()
            except Exception:
                rows = []
            questions = [row for row in rows if row.get("kind") == "ask_question"]
            if questions:
                return questions[0]
            time.sleep(0.05)

    def test_ask_returns_the_correlated_answer_with_its_receipt(self) -> None:
        """The waiting command returns the answer text and that answer's ledger id."""

        process = self.spawn(*self.question_argv(
            note="what depth is the harbor", key="question-correlated", wait=int(READY_SECONDS * 2),
        ))
        question = self.wait_for_question(process)
        replied, reply_artifact = self.invoke(*self.reply_argv(
            question["id"], disposition="answer", key="answer-correlated",
            note="twelve fathoms",
        ))
        self.assertEqual(0, replied.returncode, reply_artifact)
        artifact = self.finish(process, timeout=5)

        self.assertEqual(0, process.returncode)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("answered", artifact["evidence"]["outcome"])
        self.assertEqual("twelve fathoms", artifact["evidence"]["answer_note"])
        answer_id = artifact["evidence"]["answer_id"]
        stored = next(row for row in self.events() if row["id"] == answer_id)
        self.assertEqual("ask_answer", stored["kind"])
        self.assertEqual(question["id"], stored["reply_to"])
        self.assertEqual("twelve fathoms", stored["note"])
        self.assertEqual(question["recipient"], stored["sender"])
        self.assertEqual(question["sender"], stored["recipient"])
        self.assertEqual("red", question["sender"])
        self.assertEqual("blue", question["recipient"])

    def test_timeout_is_unknown_and_links_a_timeout_record(self) -> None:
        """No terminal in the final snapshot is UNKNOWN, never answered."""

        completed, artifact = self.invoke(*self.question_argv(
            note="is the tide still rising", key="question-timeout", wait=0,
        ))

        self.assertEqual(34, completed.returncode)
        self.assertEqual("orchestration_deadline", artifact["status"])
        self.assertEqual("UNKNOWN", artifact["evidence"]["outcome"])
        self.assertEqual("ask_timeout", artifact["evidence"]["code"])
        self.assertNotIn('"outcome":"answered"', completed.stdout)
        question = next(row for row in self.events() if row["kind"] == "ask_question")
        timeout = next(row for row in self.events() if row["kind"] == "ask_timeout")
        self.assertEqual(question["id"], timeout["reply_to"])
        self.assertEqual(question["id"], artifact["evidence"]["question_id"])
        self.assertEqual(timeout["id"], artifact["evidence"]["timeout_id"])

    def test_answer_attributes_unmeasured_cost_to_the_asker(self) -> None:
        question_argv = self.question_argv(note="what changed", key="cost", wait=0)
        _, timed_out = self.invoke_in_process(*question_argv)
        question_id = timed_out["evidence"]["question_id"]
        replied, answer = self.invoke_in_process(*self.reply_argv(
            question_id, disposition="answer", key="cost-reply", note="the marker moved",
        ))
        observed, answer_seen = self.invoke_in_process(*question_argv)
        self.assertEqual(0, replied.returncode, answer)
        self.assertEqual(0, observed.returncode, answer_seen)
        expected = {
            "status": "unmeasured",
            "reason": "puddle_measured_cost_seam_unavailable",
            "attributed_to": "red",
        }
        for artifact in (answer, answer_seen):
            with self.subTest(evidence=artifact["evidence"]):
                self.assertEqual(expected, artifact["evidence"].get("cost"))

    def test_replay_keeps_receipt_order_and_citations_after_epoch_roll(self) -> None:
        _, timed_out = self.invoke_in_process(*self.question_argv(
            note="check the channel", key="replay", wait=0,
        ))
        question_id = timed_out["evidence"]["question_id"]
        timeout_id = timed_out["evidence"]["timeout_id"]
        replay_argv = ("replay", "--root", str(self.fleet), "--question", question_id)
        open_run, open_replay = self.invoke_in_process(*replay_argv)
        self.assertEqual(0, open_run.returncode, open_replay)
        self.assertEqual([question_id, timeout_id], [
            entry["receipt_id"] for entry in open_replay["evidence"]["receipts"]
        ])
        self.assertEqual("missing", open_replay["evidence"]["acted"]["status"])
        self.assertEqual("ask_answer_absent", open_replay["evidence"]["cost"]["reason"])

        root = resolve_command_root(str(self.fleet))
        AuthorityGrantStore(root).grant_exact("bus-epoch-roll", "red", 1, datetime.now(timezone.utc))

        def carry_pending_question(fleet, old):
            plan = build_carry_plan(fleet, old)
            return plan.live_payloads, {
                "trigger": "ack",
                "policy": {"max_bytes": 1, "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": len(next(raw for path, _plane, raw in old if path == "events.jsonl")),
                "observed_at": utc_now(), "epoch_started_at": None, "due_reasons": ["bytes"],
            }

        roll_automatic_epoch(root, actor="red", planner=carry_pending_question)
        self.assertIn(question_id, [row["id"] for row in self.events()])

        (self.repository / "note.md").write_text("the channel was checked\n", encoding="utf-8")
        subprocess.run(
            ["/usr/bin/git", "commit", "--quiet", "-am", "Record the channel check."],
            cwd=self.repository, env=self.environment(), check=True,
        )
        answer_sha = subprocess.check_output(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=self.repository, text=True,
        ).strip()
        subprocess.run(
            ["/usr/bin/git", "update-ref", "refs/remotes/origin/main", answer_sha],
            cwd=self.repository, check=True,
        )
        answer_argv = self.reply_argv(
            question_id, disposition="answer", key="replay-reply", note="checked the channel",
        )
        answer_argv[answer_argv.index("--sha") + 1] = answer_sha
        replied, answer = self.invoke_in_process(*answer_argv)
        self.assertEqual(0, replied.returncode, answer)
        answer_id = answer["evidence"]["answer_id"]
        active, replay = self.invoke_in_process(*replay_argv)
        self.assertEqual(0, active.returncode, replay)
        evidence = replay["evidence"]
        self.assertEqual("red", evidence["asker"])
        self.assertEqual([question_id, timeout_id, answer_id], [
            entry["receipt_id"] for entry in evidence["receipts"]
        ])
        self.assertEqual(question_id, evidence["asked"]["receipt_id"])
        self.assertEqual("missing", evidence["presented"]["status"])
        self.assertEqual("ask_presentation_receipt_absent", evidence["presented"]["reason"])
        self.assertEqual(answer_id, evidence["acted"]["receipt_id"])
        self.assertEqual("blue", evidence["acted"]["node"])
        self.assertEqual(answer["evidence"]["cost"], evidence["cost"])
        self.assertEqual("different_commit_references", evidence["git_change"]["status"])
        for side, receipt, sha in (("question", question_id, self.sha), ("answer", answer_id, answer_sha)):
            with self.subTest(side=side):
                self.assertEqual(
                    {"repo": "floati", "sha": sha, "doc": "note.md", "receipt_id": receipt},
                    evidence["git_change"][side],
                )

        roll_bus_epoch(root, actor="red", idempotency_key="archive-replay")
        self.assertNotIn(question_id, [row["id"] for row in self.events()])
        archived, archived_replay = self.invoke_in_process(*replay_argv)
        self.assertEqual(0, archived.returncode, archived_replay)
        self.assertEqual(evidence, archived_replay["evidence"])

    def test_live_question_replays_after_native_archive_expiry(self):
        from floati.archive_retention import REMOVAL_KIND, REMOVAL_LEDGER
        from floati.epoch_rotation import maintenance
        from floati.jsonl import read_records_snapshot
        from floati.root import FloatiRoot

        self.invoke_in_process(*self.question_argv(note="before roll", key="old", wait=0))
        root = FloatiRoot.open_direct_home(self.fleet)
        AuthorityGrantStore(root).grant_exact("bus-epoch-roll", "red", 1, datetime.now(timezone.utc))
        rolled = roll_bus_epoch(root, actor="red", idempotency_key="before-live-question")
        _, question = self.invoke_in_process(*self.question_argv(note="after roll", key="live", wait=0))
        question_id = question["evidence"]["question_id"]
        argv = ("replay", "--root", str(self.fleet), "--question", question_id)
        before, evidence = self.invoke_in_process(*argv)
        self.assertEqual(0, before.returncode, evidence)
        receipt = self.events()[0]
        rolled_at = datetime.fromisoformat(receipt["timestamp"].replace("Z", "+00:00"))

        class ExpiryClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return (rolled_at + timedelta(days=31)).astimezone(tz)

        with mock.patch("floati.epoch_rotation.datetime", ExpiryClock):
            maintenance(root, actor="red", trigger="send")
        self.assertFalse(Path(receipt["archive_path"]).exists(), rolled)
        after, replay = self.invoke_in_process(*argv)
        self.assertEqual(0, after.returncode, replay)
        self.assertEqual(evidence["evidence"]["receipts"], replay["evidence"]["receipts"])
        removal, = read_records_snapshot(root, REMOVAL_LEDGER, allowed_kinds={REMOVAL_KIND})
        self.assertEqual({
            "reason": "epoch_archive_removed", "epoch_id": receipt["epoch_id"],
            "removal_receipt_id": removal["id"],
        }, replay["evidence"]["history_gap"])

    def test_unreadable_answer_is_unknown(self) -> None:
        """A bound answer this reader cannot accept is UNKNOWN, never answered."""

        process = self.spawn(*self.question_argv(
            note="can you read the mark", key="question-unreadable", wait=int(READY_SECONDS * 2),
        ))
        question = self.wait_for_question(process)
        root = resolve_command_root(str(self.fleet))
        frame = encode_frame({
            "schema_version": 99,
            "id": UNREADABLE_ANSWER_ID,
            "tenant_id": question["tenant_id"],
            "timestamp": question["timestamp"],
            "kind": "ask_answer",
            "reply_to": question["id"],
            "sender": question["recipient"],
            "recipient": question["sender"],
            "note": "garbled mark",
            "repo": "floati",
            "sha": self.sha,
            "doc": "note.md",
            "idempotency_key": "unreadable-answer",
            "acting_session_id": "sess-blue",
        })
        with root.resolve_relative("events.jsonl").open("ab") as handle:
            handle.write(frame)
        artifact = self.finish(process, timeout=5)

        self.assertEqual(35, process.returncode)
        self.assertEqual("degraded", artifact["status"])
        self.assertEqual("UNKNOWN", artifact["evidence"]["outcome"])
        self.assertEqual("ask_answer_unreadable", artifact["evidence"]["code"])
        self.assertEqual(question["id"], artifact["evidence"]["question_id"])
        self.assertNotIn("answer_id", artifact["evidence"])
        self.assertNotIn('"outcome":"answered"', json.dumps(artifact))

    def test_decline_links_a_decline_record_and_is_not_answered(self) -> None:
        """A peer decline is its own linked record and is not an answer."""

        process = self.spawn(*self.question_argv(
            note="will you take the watch", key="question-decline", wait=int(READY_SECONDS * 2),
        ))
        question = self.wait_for_question(process)
        declined, _artifact = self.invoke(*self.reply_argv(
            question["id"], disposition="decline", key="decline-1",
            reason="off watch",
        ))
        self.assertEqual(0, declined.returncode)
        artifact = self.finish(process, timeout=5)

        self.assertEqual(0, process.returncode)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("declined", artifact["evidence"]["outcome"])
        self.assertNotEqual("answered", artifact["evidence"]["outcome"])
        stored = next(row for row in self.events() if row["kind"] == "ask_decline")
        self.assertEqual(stored["id"], artifact["evidence"]["decline_id"])
        self.assertEqual(question["id"], stored["reply_to"])
        self.assertEqual(question["recipient"], stored["sender"])
        self.assertEqual(question["sender"], stored["recipient"])
        self.assertEqual("off watch", stored["reason"])
        replayed, replay = self.invoke_in_process(
            "replay", "--root", str(self.fleet), "--question", question["id"],
        )
        self.assertEqual(0, replayed.returncode, replay)
        self.assertEqual("declined", replay["evidence"]["outcome"])
        self.assertEqual(stored["id"], replay["evidence"]["acted"]["receipt_id"])
        self.assertEqual("ask_answer_absent", replay["evidence"]["cost"]["reason"])

    def test_ask_opens_no_listener_and_sends_nothing(self) -> None:
        """While the command waits, its process has no TCP listener or connection."""

        lsof = wake_timeout_forensics._lsof_executable()
        self.assertTrue(lsof, "lsof unavailable: none of the product candidates exists")
        process = self.spawn(*self.question_argv(
            note="hold this watch locally", key="question-local", wait=2,
        ))
        self.wait_for_question(process)
        listed = subprocess.run(
            [lsof, "-nP", "-a", "-p", str(process.pid), "-iTCP"],
            check=False, capture_output=True, text=True,
        )
        self.finish(process, timeout=5)
        self.assertIn(listed.returncode, (0, 1), listed.stderr)
        self.assertEqual("", listed.stderr)
        self.assertNotIn("LISTEN", listed.stdout)
        self.assertNotIn("ESTABLISHED", listed.stdout)


class NetAsk1PureTests(unittest.TestCase):
    def test_answered_is_not_projected_without_the_answer_receipt(self) -> None:
        """A terminal with no answer id cannot be shown as answered."""

        from floati.ask import project_terminal

        missing = project_terminal({"kind": "ask_answer", "note": "twelve fathoms"})
        self.assertEqual("UNKNOWN", missing["outcome"])
        self.assertNotIn("answer_id", missing)

        receipt = "ask-answer-018f7e9b3c127abc8def0123456789ab"
        present = project_terminal({
            "kind": "ask_answer", "id": receipt, "note": "twelve fathoms",
        })
        self.assertEqual("answered", present["outcome"])
        self.assertEqual(receipt, present["answer_id"])



if __name__ == "__main__":
    unittest.main()
