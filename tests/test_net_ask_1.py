"""NET-ASK-1: one local command correlates a question with its answer.

The command writes the question on this machine's ledger, waits a bounded
time, and returns the linked answer or decline. A timeout and an unreadable
answer are typed UNKNOWN. The outcome answered is printed only with the
answer record's id. Nothing listens and nothing leaves the machine.
"""

from __future__ import annotations

import io
from tests.readiness import READY_SECONDS, Readiness, communicate

import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from hashlib import sha256
from types import SimpleNamespace
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
        communicate(process, "collect the owned ask command after group shutdown")

    def finish(self, process: subprocess.Popen[str]) -> dict:
        readiness = Readiness("ask command returns its terminal receipt")
        try:
            stdout, stderr = process.communicate(timeout=readiness.remaining())
        except subprocess.TimeoutExpired:
            self.reap(process)
            raise readiness.failure() from None
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
        artifact = self.finish(process)

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

    def test_live_board_shows_the_selected_nodes_answer_and_receipt(self) -> None:
        """The board must send to the selected row and display its real reply."""
        from floati.ask import perform_reply, wait_for_question
        from floati.tui import _read_terminal_input, run_board
        from tests import readiness as readiness_clock

        class Terminal(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return input_stream.fileno()

            def write(self, value):
                if result_markers and any(marker in value for marker in result_markers):
                    result_markers.clear()
                    keyboard.write(b"\rq")
                return super().write(value)

        note = "How deep is the harbor?"
        answer = "twelve fathoms"
        previous_cwd = Path.cwd()

        def run_fixture_board(**kwargs):
            self.assertEqual(["red", "blue", "everyone"], [row["node_id"] for row in kwargs["model_loader"]().nodes])
            prior_handler = signal.signal(signal.SIGALRM, stalled_board)
            try:
                signal.setitimer(signal.ITIMER_REAL, readiness.remaining())
                return run_board(
                    **kwargs, input_stream=Terminal(), output_stream=screen,
                    terminal_response=b"\x1b[?1;2c",
                )
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, prior_handler)

        added, artifact = self.invoke_in_process(
            "node", "add", "--root", str(self.fleet), "--node", "everyone",
            "--harness", "Codex", "--lifetime", "permanent",
        )
        self.assertEqual(0, added.returncode, artifact)
        records_before = self.events()

        for outcome in ("stalled", "cancel", "pasted_newlines", "overflow", "refused", "timeout", "declined", "answered", "exact_node"):
            with self.subTest(outcome=outcome):
                screen = Terminal()
                recipient = "everyone" if outcome == "exact_node" else "blue"
                sha = "0" * 40 if outcome == "refused" else self.sha
                wait = "5" if outcome in {"answered", "declined"} else "0"
                authored = "x" * 1025 if outcome == "overflow" else note
                fields = f"red\rfloati\r{sha}\rnote.md\r{wait}\r{authored}\r"
                if outcome == "stalled":
                    keys = "q"
                    result_markers = []
                elif outcome in {"cancel", "pasted_newlines", "overflow"}:
                    extra = {"cancel": "", "pasted_newlines": fields + "\r", "overflow": fields + "\r\x04"}[outcome]
                    keys = "j?" + extra + "\x07q"
                    result_markers = []
                else:
                    keys = ("jj" if recipient == "everyone" else "j") + "?" + fields + "\r\x04"
                    result_markers = ["sha_absent", "address_preview_required", "ask_timeout", "ask-decline-", "ask-answer-"]

                question_published = threading.Event()
                key_read = threading.Event()
                delayed_by = 0.0
                clock = SimpleNamespace(monotonic=lambda: time.monotonic() + delayed_by)

                def stalled_board(_signum, _frame):
                    self.fail(f"{readiness.failure()}\nLast screen: {screen.getvalue()[-1800:]}")

                def read_terminal(stream, timeout, *args, **kwargs):
                    nonlocal delayed_by
                    if outcome == "stalled":
                        # Stop the terminal before it can read any queued keys.
                        delayed_by = READY_SECONDS
                        signal.raise_signal(signal.SIGALRM)
                    readiness.remaining()
                    event = _read_terminal_input(stream, timeout, *args, **kwargs)
                    if question_published.is_set() and event == " ":
                        key_read.set()
                    if outcome == "overflow":
                        # Model a descheduled terminal reader without sleeping.
                        delayed_by = 4.0
                    return event

                def answer_request(root, question, seconds, **kwargs):
                    if outcome == "answered":
                        question_published.set()
                        keyboard.write(b" ")
                        readiness.event(key_read)
                        perform_reply(
                            root, sender="blue", recipient="red", question_id=question["id"],
                            disposition="answer", session="sess-blue", idempotency_key="board-answer",
                            note=answer, repo="floati", sha=self.sha, doc="note.md",
                        )
                    if outcome == "declined":
                        perform_reply(
                            root, sender="blue", recipient="red", question_id=question["id"],
                            disposition="decline", session="sess-blue", idempotency_key="board-decline",
                            reason="survey unavailable",
                        )
                    return wait_for_question(root, question, seconds, **kwargs)

                read_fd, write_fd = os.pipe()
                with (
                    os.fdopen(read_fd, "r") as input_stream,
                    os.fdopen(write_fd, "wb", buffering=0) as keyboard,
                    mock.patch.object(readiness_clock, "time", clock),
                    mock.patch.dict(os.environ, {**self.environment(), "TERM": "xterm", "NO_COLOR": "1"}, clear=True),
                    mock.patch("floati.tui.run_board", side_effect=run_fixture_board),
                    mock.patch("floati.tui._read_terminal_input", side_effect=read_terminal),
                    mock.patch("floati.tui.termios.tcgetattr", return_value=[]),
                    mock.patch("floati.tui.termios.tcsetattr"),
                    mock.patch("floati.tui.tty.setcbreak"),
                    mock.patch("floati.tui.shutil.get_terminal_size", return_value=os.terminal_size((80, 30))),
                    mock.patch("floati.ask.wait_for_question", side_effect=answer_request),
                ):
                    progress = (
                        "board reads a key before displaying the reply"
                        if outcome == "answered" else "board terminal progress"
                    )
                    readiness = Readiness(progress)
                    keyboard.write(keys.encode())
                    try:
                        os.chdir(self.repository)
                        argv = ["board", "--root", str(self.fleet), "--session", "sess-red"]
                        if outcome == "stalled":
                            with self.assertRaisesRegex(AssertionError, "board terminal progress: readiness bound="):
                                main(argv)
                            continue
                        status = main(argv)
                    finally:
                        os.chdir(previous_cwd)

                self.assertEqual(0, status)
                records = self.events()
                new_rows = records[len(records_before):]
                records_before = records
                if outcome in {"cancel", "pasted_newlines", "overflow", "refused"}:
                    self.assertFalse(new_rows, new_rows)
                    if outcome == "refused":
                        self.assertIn("sha_absent", screen.getvalue())
                    continue
                questions = [row for row in new_rows if row["kind"] == "ask_question"]
                self.assertTrue(questions, "submitting from the live board wrote no question receipt")
                question, = questions
                self.assertEqual(("red", recipient, "sess-red", note), tuple(
                    question[key] for key in ("sender", "recipient", "acting_session_id", "note")
                ))
                kind, shown = {
                    "timeout": ("ask_timeout", "ask_timeout"),
                    "exact_node": ("ask_timeout", "ask_timeout"),
                    "declined": ("ask_decline", "survey unavailable"),
                    "answered": ("ask_answer", answer),
                }[outcome]
                receipt, = [row for row in new_rows if row["kind"] == kind]
                self.assertIn(receipt["id"], screen.getvalue())
                self.assertIn(shown, screen.getvalue())

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
            "outcome": "UNKNOWN",
        }
        for artifact in (answer, answer_seen):
            with self.subTest(evidence=artifact["evidence"]):
                self.assertEqual(expected, artifact["evidence"].get("cost"))

    def test_measured_cost_joins_the_exact_pair_for_both_parties_and_replay(self):
        question_argv = self.question_argv(note="check costs", key="measured", wait=0)
        _, timed_out = self.invoke_in_process(*question_argv)
        question_id = timed_out["evidence"]["question_id"]
        reply_argv = self.reply_argv(
            question_id, disposition="answer", key="measured-reply", note="checked",
        )
        rc, answer = self.invoke_in_process(*reply_argv)
        self.assertEqual(0, rc.returncode, answer)
        answer_id = answer["evidence"]["answer_id"]
        tenant = resolve_command_root(str(self.fleet)).tenant_id
        row = {
            "tenant_id": tenant, "question_id": question_id, "answer_id": answer_id,
            "basis": "MEASURED", "total_tokens": 73, "measurement_id": "sample-1",
            "observed_at": "2026-11-24T00:00:00Z",
        }
        export = self.base / "ask-cost-v1.json"
        export.touch(mode=0o600)
        replay_argv = ("replay", "--root", str(self.fleet), "--question", question_id)
        expected = {
            "status": "measured", "attributed_to": "red", "total_tokens": 73,
            "measurement_id": "sample-1", "observed_at": row["observed_at"],
        }
        with mock.patch.dict(os.environ, {"FLOATI_PUDDLE_ASK_COST_EXPORT": str(export)}):
            export.write_text(json.dumps({"schema_version": 1, "records": [
                dict(row, tenant_id="foreign"), row,
            ]}), encoding="utf-8")
            for argv in (reply_argv, question_argv, replay_argv):
                with self.subTest(command=argv):
                    result, artifact = self.invoke_in_process(*argv)
                    self.assertEqual(0, result.returncode, artifact)
                    self.assertEqual(expected, artifact["evidence"]["cost"])
            for records, reason in (
                ([dict(row, answer_id=question_id)], "puddle_ask_cost_not_found"),
                ([dict(row, basis="ESTIMATED")], "puddle_ask_cost_not_measured"),
                ([dict(row, total_tokens=True)], "puddle_ask_cost_invalid"),
                ([row, dict(row, total_tokens=74)], "puddle_ask_cost_ambiguous"),
            ):
                with self.subTest(reason=reason):
                    export.write_text(json.dumps({"schema_version": 1, "records": records}),
                                      encoding="utf-8")
                    result, artifact = self.invoke_in_process(*replay_argv)
                    self.assertEqual(0, result.returncode, artifact)
                    self.assertEqual(
                        {"status": "unmeasured", "reason": reason, "attributed_to": "red",
                         "outcome": "UNKNOWN"},
                        artifact["evidence"]["cost"],
                    )
            export.write_text(json.dumps({"schema_version": 1, "records": [
                dict(row, total_tokens=0),
            ]}), encoding="utf-8")
            _, zero = self.invoke_in_process(*replay_argv)
            self.assertEqual(dict(expected, total_tokens=0), zero["evidence"]["cost"])
            export.unlink()
            _, missing = self.invoke_in_process(*replay_argv)
            self.assertEqual(
                {"status": "unmeasured", "reason": "puddle_measured_cost_seam_unavailable",
                 "attributed_to": "red", "outcome": "UNKNOWN"},
                missing["evidence"]["cost"],
            )

    def test_execution_projection_refuses_unmapped_native_coordinates(self):
        from floati.ask_execution import AnswerExecutionProjection, NativeCoordinate

        export = self.base / "execution"
        export.mkdir(mode=0o700)
        runtime = AnswerExecutionProjection(resolve_command_root(str(self.fleet)), export)
        _, timed_out = self.invoke_in_process(*self.question_argv(note="cost", key="unmapped", wait=0))
        presentation = dict(sender="blue", recipient="red", session="sess-blue",
                            question_id=timed_out["evidence"]["question_id"],
                            idempotency_key="unmapped-present", sequence=0)
        session = "019f7000-0000-7000-8000-000000000000"
        turn = "019f7000-0001-7000-8000-000000000000"

        with self.subTest("unknown harness"):
            with self.assertRaises((ValueError, KeyError)) as refused:
                runtime.present("other", **presentation)
            self.assertIs(type(refused.exception), ValueError)
            self.assertEqual("ask_execution_source_unsupported", str(refused.exception))
        scope = runtime.present("codex", **presentation)
        coordinate = NativeCoordinate("openai", session, session, turn)
        scope.bind_turn(coordinate)
        scope.issued(1, coordinate, "provider-issue")
        with self.subTest("Puddle display ordinal is not a final selector"):
            with self.assertRaisesRegex(ValueError, "^ask_execution_final_coordinate_invalid$"):
                scope.ended(2, 1, "completed", final_call_id="1")
        with self.subTest("lineage from another source"):
            with self.assertRaisesRegex(ValueError, "^ask_execution_coordinate_invalid$"):
                scope.bind_turn(NativeCoordinate("openai", "copied-header", session, turn))

    def test_execution_projection_recovers_committed_scopes_without_reexecuting(self):
        from floati.ask_execution import AnswerExecutionProjection, NativeCoordinate
        from floati.ask_cost_v2 import session_reference

        export = self.base / "execution"
        export.mkdir(mode=0o700)
        root = resolve_command_root(str(self.fleet))
        codex_session = "019f7000-0000-7000-8000-000000000000"
        codex_turn = "019f7000-0001-7000-8000-000000000000"
        counters = (1000, 600, 120, 40, 1120)
        for harness, coordinate in (
            ("codex", NativeCoordinate("openai", codex_session, codex_session, codex_turn)),
            ("claude-code", NativeCoordinate("anthropic", "s", "s", "user-1")),
            ("codex-without-final", NativeCoordinate("openai", codex_session, codex_session, codex_turn)),
        ):
            with self.subTest(harness=harness):
                runtime = AnswerExecutionProjection(root, export)
                question_argv = self.question_argv(note="check execution", key=harness, wait=0)
                _, timed_out = self.invoke_in_process(*question_argv)
                qid = timed_out["evidence"]["question_id"]
                scope = runtime.present(
                    "codex" if harness == "codex-without-final" else harness,
                    sender="blue", recipient="red", question_id=qid,
                    session="sess-blue", idempotency_key=harness + "-present", sequence=0,
                )
                scope.bind_turn(coordinate)
                if harness == "claude-code":
                    scope.issued(1, coordinate, "req_dup", message_id="msg_dup")
                    scope.ended(2, 1, "completed")
                else:
                    scope.issued(1, coordinate, "1")
                    if harness == "codex":
                        scope.ended(2, 1, "completed", codex_final_usage=(counters, counters))
                    else:
                        scope.ended(2, 1, "completed")
                close_sequence = 3
                if harness == "codex":
                    scope.issued(3, coordinate, "2")
                    scope.ended(4, 3, "completed", codex_final_usage=(
                        (1020, 605, 125, 42, 1145), (20, 5, 5, 2, 25),
                    ))
                    close_sequence = 5
                elif harness == "codex-without-final":
                    scope.issued(4, coordinate, "2", parent_attempt=1)
                    scope.ended(5, 4, "completed", codex_final_usage=(counters, counters))
                    close_sequence = 6
                result = scope.answer(
                    sequence=close_sequence, through_sequence=close_sequence,
                    idempotency_key=harness + "-reply", note="checked",
                    repo="floati", sha=self.sha, doc="note.md",
                )
                answer_id = result["evidence"]["answer_id"]
                # A fresh publisher recovers the staged evidence after the durable append.
                self.assertFalse((export / "ask-cost-manifest-v1.json").exists())
                if harness != "codex-without-final":
                    continue

                AuthorityGrantStore(root).grant_exact("bus-epoch-roll", "red", 1, datetime.now(timezone.utc))
                roll_bus_epoch(root, actor="red", idempotency_key="archive-execution")
                restarted = AnswerExecutionProjection(root, export)
                restarted.publish()
                restarted.publish()
                manifest = json.loads((export / "ask-cost-manifest-v1.json").read_bytes())
                documents = {}
                for name, digest in manifest["files"].items():
                    raw = (export / name).read_bytes()
                    self.assertEqual(digest, sha256(raw).hexdigest())
                    documents[name] = json.loads(raw)
                    self.assertEqual(manifest["generation_id"], documents[name]["generation_id"])
                scopes = documents["ask-cost-execution-v1.json"]["scopes"]
                self.assertEqual(3, len(scopes))
                codex, = (row for row in scopes if row["completeness"]["status"] == "complete")
                self.assertEqual("codex", codex["harness"])
                self.assertEqual(
                    (codex_session, codex_session, codex_turn),
                    tuple(codex[field] for field in ("source_lineage_id", "native_session_id", "native_turn_id")),
                )
                self.assertEqual(
                    ["ed3685a1b320098b06eac056b80c1726ff42c5e979307e3099c3ec6e2ef34ac6",
                     "e4d81013101d94a852245d98c421f45a80b6c23932581fdc67607d9ea5ca55b6"],
                    [attempt["native_call_id"] for attempt in codex["attempts"]],
                )
                claude, = (row for row in scopes if row["harness"] == "claude-code")
                self.assertEqual(("s", "s", "user-1"),
                                 tuple(claude[field] for field in ("source_lineage_id", "native_session_id",
                                                                  "native_turn_id")))
                self.assertEqual("incomplete", claude["completeness"]["status"])
                self.assertEqual("req_dup", claude["attempts"][0]["native_call_id"])
                unresolved, = (row for row in scopes if row["answer_id"] == answer_id)
                self.assertEqual("incomplete", unresolved["completeness"]["status"])
                self.assertEqual([], unresolved["attempts"])
                self.assertEqual(session_reference(root.tenant_id, answer_id, "sess-blue"),
                                 unresolved["answering_session_ref"])
                self.assertEqual(3, len(documents["ask-cost-receipts-v1.json"]["pairs"]))
                for document in documents.values():
                    self.assertNotIn("checked", json.dumps(document))
                    self.assertNotIn("note.md", json.dumps(document))

    def test_wait_refuses_cost_when_the_answer_chain_contradicts_a_presentation(self):
        from floati.ask import wait_for_question
        from floati.ids import uuid7_hex

        self.invoke_in_process(*self.question_argv(note="check chain", key="chain", wait=0))
        question = next(row for row in self.events() if row["kind"] == "ask_question")
        result, answer = self.invoke_in_process(*self.reply_argv(
            question["id"], disposition="answer", key="chain-reply", note="checked",
        ))
        self.assertEqual(0, result.returncode, answer)
        root = resolve_command_root(str(self.fleet))
        # A schema-valid presentation after the terminal contradicts the chain.
        with root.resolve_relative("events.jsonl").open("ab") as stream:
            stream.write(encode_frame({
                "schema_version": 1, "kind": "ask_presentation",
                "id": "ask-presentation-" + uuid7_hex(), "timestamp": utc_now(),
                "tenant_id": root.tenant_id, "sender": "blue", "recipient": "red",
                "acting_session_id": "sess-blue", "reply_to": question["id"],
                "idempotency_key": "late-presentation",
            }))
        observed = wait_for_question(root, question, 0)
        self.assertEqual("ask_answer_unreadable", observed["evidence"].get("code"))
        self.assertNotIn("cost", observed["evidence"])

    def test_v2_cost_agrees_on_answer_retry_and_retained_replay(self):
        class CostClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 26, 12, 0, 30, tzinfo=tz)

        question_argv = self.question_argv(note="check costs", key="measured-v2", wait=0)
        with mock.patch("floati.ask._stamp", return_value="2026-11-26T11:59:58Z"):
            _, before = self.invoke_in_process(*question_argv)
        question_id = before["evidence"]["question_id"]
        root = resolve_command_root(str(self.fleet))
        replay_argv = ("replay", "--root", str(self.fleet), "--question", question_id)
        _, pending = self.invoke_in_process(*replay_argv)
        self.assertEqual("ask_answer_absent", pending["evidence"]["cost"]["reason"])
        reply_argv = self.reply_argv(
            question_id, disposition="answer", key="measured-v2-reply", note="checked",
        )
        native_id = "019b0000000070008000000000000003"
        receipts = [row for row in self.events() if row["id"] == question_id]
        q = receipts[0]
        def reference(rid, session):
            return sha256("\0".join(("floati.ask-session.v2", root.tenant_id, rid, session))
                          .encode("utf-8")).hexdigest()

        row = {
            "question_id": question_id, "answer_id": "ask-answer-" + native_id,
            "asking_node": "red", "asking_session_ref": reference(question_id, q["acting_session_id"]),
            "answering_node": "blue", "answering_session_ref": reference("ask-answer-" + native_id, "sess-blue"),
            "observed_at": "2026-11-26T12:00:10Z", "checked_at": "2026-11-26T12:00:10Z",
            "expires_at": "2026-11-27T12:00:10Z",
            "usage": {"basis": "MEASURED", "measurement_id": "m-" + "1" * 32, "total_tokens": 73},
        }
        export = self.base / "ask-cost-v2.json"
        export.touch(mode=0o600)
        export.write_text(json.dumps({"schema_version": 2, "tenant_id": root.tenant_id,
                                      "records": [row]}))
        with mock.patch.dict(os.environ, {"FLOATI_PUDDLE_ASK_COST_EXPORT": str(export)}),\
                mock.patch("floati.ask_cost.datetime", CostClock),\
                mock.patch("floati.ask._stamp", return_value="2026-11-26T12:00:08Z"),\
                mock.patch("floati.ask.uuid7_hex", return_value=native_id):
            answer_cost = None
            for argv in (reply_argv, question_argv, replay_argv):
                with self.subTest(command=argv[0], mode=argv):
                    result, artifact = self.invoke_in_process(*argv)
                    self.assertEqual(0, result.returncode, artifact)
                    cost = artifact["evidence"]["cost"]
                    self.assertEqual("measured", cost["status"], cost)
                    self.assertEqual(73, cost["total_tokens"])
                    self.assertEqual("red", cost["attributed_to"])
                    self.assertEqual("sess-red", cost["attributed_to_session"])
                    self.assertEqual(row["usage"]["measurement_id"], cost["measurement_id"])
                    if answer_cost is None:
                        answer_cost = cost
                    self.assertEqual(answer_cost, cost)
            AuthorityGrantStore(root).grant_exact("bus-epoch-roll", "red", 1, datetime.now(timezone.utc))
            roll_bus_epoch(root, actor="red", idempotency_key="archive-v2-cost")
            self.assertNotIn(question_id, [row["id"] for row in self.events()])
            _, archived = self.invoke_in_process(*replay_argv)
            self.assertEqual(answer_cost, archived["evidence"]["cost"])
            self.assertEqual("2026-11-26T12:00:30Z", answer_cost["evaluated_at"])

    def test_recipient_can_present_an_ask_and_replay_the_durable_receipt(self):
        _, timed_out = self.invoke_in_process(*self.question_argv(
            note="read the banked note", key="presentation", wait=0,
        ))
        question_id = timed_out["evidence"]["question_id"]
        present_argv = (
            "ask", "--root", str(self.fleet), "--from", "blue", "--to", "red",
            "--reply-to", question_id, "--disposition", "present", "--session",
            "sess-blue", "--idempotency-key", "present-question",
        )
        wrong, _ = self.invoke_in_process(
            *("red" if arg == "blue" else "blue" if arg == "red" else arg for arg in present_argv)
        )
        self.assertEqual(20, wrong.returncode)
        result, artifact = self.invoke_in_process(*present_argv)
        self.assertEqual(0, result.returncode, artifact)
        self.assertEqual("read the banked note", artifact["evidence"]["question"]["note"])
        presentation_id = artifact["evidence"]["presentation_id"]
        again, repeated = self.invoke_in_process(*present_argv)
        self.assertEqual(0, again.returncode, repeated)
        self.assertEqual(presentation_id, repeated["evidence"]["presentation_id"])

        root = resolve_command_root(str(self.fleet))
        AuthorityGrantStore(root).grant_exact("bus-epoch-roll", "red", 1, datetime.now(timezone.utc))
        self.roll_with_pending_asks(root)
        repeated_result, repeated = self.invoke_in_process(*present_argv)
        self.assertEqual(0, repeated_result.returncode, repeated)
        self.assertEqual(presentation_id, repeated["evidence"]["presentation_id"])
        result, answer = self.invoke_in_process(*self.reply_argv(
            question_id, disposition="answer", key="after-presentation", note="done",
        ))
        self.assertEqual(0, result.returncode, answer)
        answer_id = answer["evidence"]["answer_id"]
        roll_bus_epoch(root, actor="red", idempotency_key="archive-presentation")
        replayed, replay = self.invoke_in_process(
            "replay", "--root", str(self.fleet), "--question", question_id,
        )
        self.assertEqual(0, replayed.returncode, replay)
        evidence = replay["evidence"]
        self.assertEqual(presentation_id, evidence["presented"]["receipt_id"])
        self.assertEqual("blue", evidence["presented"]["node"])
        self.assertEqual(question_id, evidence["presented"]["question_id"])
        self.assertEqual(answer_id, evidence["acted"]["receipt_id"])
        self.assertEqual(answer["evidence"]["cost"], evidence["cost"])
        self.assertEqual([question_id, timed_out["evidence"]["timeout_id"], presentation_id, answer_id], [
            entry["receipt_id"] for entry in evidence["receipts"]
        ])

    def roll_with_pending_asks(self, root):
        def carry_pending_question(fleet, old):
            plan = build_carry_plan(fleet, old)
            return plan.live_payloads, {
                "trigger": "ack",
                "policy": {"max_bytes": 1, "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": len(next(raw for path, _plane, raw in old if path == "events.jsonl")),
                "observed_at": utc_now(), "epoch_started_at": None, "due_reasons": ["bytes"],
            }

        roll_automatic_epoch(root, actor="red", planner=carry_pending_question)

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

        self.roll_with_pending_asks(root)
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
        verified, git_replay = self.invoke_in_process(
            *replay_argv, "--repository", str(self.repository),
        )
        self.assertEqual(0, verified.returncode, git_replay)
        change = git_replay["evidence"]["git_change"]["comparison"]
        self.assertEqual("tree_changed", change["status"])
        for side, sha in (("question", self.sha), ("answer", answer_sha)):
            with self.subTest(side=side):
                tree, blob = subprocess.check_output(
                    ["/usr/bin/git", "rev-parse", sha + "^{tree}", sha + ":note.md"],
                    cwd=self.repository, text=True,
                ).splitlines()
                self.assertEqual({"tree": tree, "document_object": blob}, change[side])
        self.assertEqual("modified", change["document_change"])
        missing, missing_git = self.invoke_in_process(
            *replay_argv, "--repository", str(self.base / "missing"),
        )
        self.assertEqual(0, missing.returncode, missing_git)
        self.assertEqual(
            {"status": "missing", "reason": "ask_git_objects_unavailable"},
            missing_git["evidence"]["git_change"]["comparison"],
        )

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
        artifact = self.finish(process)

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
        artifact = self.finish(process)

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
        """The local command can publish and wait without opening a socket."""
        from floati import ask

        elapsed = 0.0

        def advance(seconds):
            nonlocal elapsed
            elapsed += seconds

        def forbid_socket(*args, **kwargs):
            self.fail("Local ask opened a socket")

        clock = SimpleNamespace(monotonic=lambda: elapsed, sleep=advance)
        with mock.patch.object(ask, "time", clock),\
                mock.patch.object(socket, "socket", forbid_socket),\
                mock.patch.object(socket, "create_connection", forbid_socket),\
                mock.patch.object(socket, "getaddrinfo", forbid_socket):
            completed, artifact = self.invoke_in_process(*self.question_argv(
                note="hold this watch locally", key="question-local", wait=2,
            ))
        self.assertEqual(34, completed.returncode, artifact)
        self.assertEqual("ask_timeout", artifact["evidence"]["code"])
        records = self.events()
        question, = (row for row in records if row["kind"] == "ask_question")
        timeout, = (row for row in records if row["kind"] == "ask_timeout")
        self.assertEqual(question["id"], timeout["reply_to"])
        self.assertFalse(any(row["kind"] == "message_envelope" for row in records))


class NetAsk1PureTests(unittest.TestCase):
    def test_v2_cost_frozen_vectors(self):
        from floati.ask_cost import project_ask_cost

        fixture = REPOSITORY_ROOT / "tests/fixtures/ask-cost-execution/complete/ask-cost-receipts-v1.json"
        source = json.loads(fixture.read_bytes())
        pair = source["pairs"][0]
        q, a = (dict(pair[kind], tenant_id=source["tenant_id"], kind="ask_" + kind)
                for kind in ("question", "answer"))
        # Exact Example C field values, independently emitted by Puddle's
        # testV2ExactCostUsesFinalsOnceAndKeepsRevisionOnRecheck.
        frozen = (fixture.parents[1] / "ask-cost-v2.json").read_text()
        generation = json.loads(frozen)
        row = generation["records"][0]
        fresh = "2026-11-26T12:00:30Z"

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory)
            export = root / "ask-cost-v2.json"
            legacy = root / "ask-cost-v1.json"
            legacy.touch(mode=0o600)
            legacy_row = {
                "tenant_id": q["tenant_id"], "question_id": q["id"], "answer_id": a["id"],
                **row["usage"], "observed_at": row["observed_at"],
            }
            legacy.write_text(json.dumps({"schema_version": 1, "records": [legacy_row]}))

            def lookup(*, data=generation, when=fresh, question=q, answer=a,
                       selected=export, require_v2=False, raw=None):
                if selected == export and not export.is_symlink():
                    export.touch(mode=0o600)
                    export.write_text(json.dumps(data) if raw is None else raw)
                with mock.patch.dict(os.environ, {
                    "FLOATI_PUDDLE_ASK_COST_EXPORT": str(selected) if selected else "",
                }):
                    return project_ask_cost(
                        question, answer, now=lambda: datetime.fromisoformat(when[:-1] + "+00:00"),
                        require_v2=require_v2,
                    )

            def expect_reason(reason, **kwargs):
                cost = lookup(**kwargs)
                self.assertEqual(("unmeasured", "UNKNOWN", reason),
                                 (cost["status"], cost["outcome"], cost["reason"]), cost)
                self.assertEqual("red", cost["attributed_to"])
                self.assertNotIn("total_tokens", cost)
                return cost

            def with_row(**fields):
                return dict(generation, records=[dict(row, **fields)])

            cost = lookup(raw=frozen)
            self.assertEqual(
                ("measured", 73, "m-" + "1" * 32, "red", "sess-red"),
                (cost["status"], cost["total_tokens"], cost["measurement_id"],
                 cost["attributed_to"], cost["attributed_to_session"]),
            )
            for when, expected in (
                ("2026-11-26T12:00:10Z", None),
                ("2026-11-26T12:00:09.999999Z", "puddle_ask_cost_future"),
                ("2026-11-27T12:00:10Z", "puddle_ask_cost_stale"),
                ("2026-11-27T12:00:09.999999Z", None),
            ):
                with self.subTest(when=when):
                    if expected:
                        expect_reason(expected, when=when)
                    else:
                        self.assertEqual(73, lookup(when=when)["total_tokens"])
            renewed = lookup(data=with_row(checked_at="2026-11-27T12:00:11Z",
                                           expires_at="2026-11-28T12:00:11Z"),
                             when="2026-11-27T12:00:11Z")
            self.assertEqual(row["usage"]["measurement_id"], renewed["measurement_id"])
            self.assertEqual(row["observed_at"], renewed["observed_at"])
            self.assertEqual(73, renewed["total_tokens"])
            for total, marker in ((0, "2"), (53, "3")):
                usage = dict(row["usage"], total_tokens=total, measurement_id="m-" + marker * 32)
                result = lookup(data=with_row(usage=usage))
                self.assertEqual(total, result["total_tokens"])
                self.assertEqual(usage["measurement_id"], result["measurement_id"])
            for usage in (
                {"basis": "DERIVED", "reason": "bounded_population", "lower_tokens": 48, "upper_tokens": 73},
                {"basis": "ESTIMATE", "reason": "assumption_required"},
                *({"basis": "UNKNOWN", "reason": reason} for reason in (
                    "usage_missing", "execution_unbound", "population_incomplete", "usage_conflict",
                    "source_unsupported", "session_conflict", "counter_invalid",
                )),
            ):
                with self.subTest(usage=usage):
                    self.assertEqual(usage, {key: value for key, value in
                                             expect_reason(usage["reason"], data=with_row(usage=usage)).items()
                                             if key in usage})
            for change in (
                {"asking_node": "green"}, {"answering_node": "green"},
                {"asking_session_ref": row["answering_session_ref"],
                 "answering_session_ref": row["asking_session_ref"]},
            ):
                expect_reason("puddle_ask_cost_binding_mismatch", data=with_row(**change))
            expect_reason("puddle_ask_cost_not_found", question=dict(q, tenant_id="other"),
                          answer=dict(a, tenant_id="other"))
            expect_reason("puddle_ask_cost_not_found", answer=dict(a, id=a["id"][:-1] + "4"))
            for duplicate in (row, dict(row, question_id=q["id"][:-1] + "8", answer_id=a["id"][:-1] + "9")):
                expect_reason("puddle_ask_cost_ambiguous", data=dict(generation, records=[row, duplicate]))
            for bad in (-1, True, 1.0, 2**63):
                expect_reason("puddle_ask_cost_invalid", data=with_row(usage=dict(row["usage"], total_tokens=bad)))
            expect_reason("puddle_ask_cost_invalid", data=with_row(checked_at=row["expires_at"]))
            expect_reason("puddle_ask_cost_invalid", raw='{"schema_version":2,"schema_version":2}')
            expect_reason("puddle_ask_cost_version_unsupported", data=dict(generation, schema_version=3))
            expect_reason("ask_answer_absent", answer=None)
            expect_reason("puddle_measured_cost_seam_unavailable", selected=None)
            self.assertEqual(73, lookup(selected=legacy)["total_tokens"])
            expect_reason("v2_evidence_required", selected=legacy, require_v2=True)
            expect_reason("puddle_ask_cost_stale", when=row["expires_at"])
            legacy.write_text(json.dumps(generation))
            expect_reason("puddle_ask_cost_invalid", selected=legacy)
            expect_reason("puddle_ask_cost_invalid", selected=legacy, require_v2=True)
            export.unlink()
            expect_reason("puddle_measured_cost_seam_unavailable", selected=str(export))
            os.link(legacy, export)
            expect_reason("puddle_ask_cost_unreadable", selected=str(export))
            export.unlink()
            export.symlink_to(legacy)
            expect_reason("puddle_ask_cost_unreadable")

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
