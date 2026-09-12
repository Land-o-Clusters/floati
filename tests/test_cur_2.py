"""CUR-2: Cursor stop waiter as the Codex waiter's twin.

Five exits, fixture-proved, with a fake stdin payload and a scratch root:

(1) aborted -> stdout is ``{}``, and nothing else prints ``{}``
(2) mail -> drain followup_message (never ``{}``)
(3) an unreadable inbox -> a followup naming node, root and the typed
    reason (never ``{}``; CUR-2 Am.1)
(4) deadline -> one-line re-arm followup (never ``{}``)
(5) a configuration-time refusal -> typed artifact on stdout AND one line
    on stderr (CUR-2 Am.1)

Empty inbox is typed silence and keeps waiting until mail or deadline.

``floati hook install --harness cursor`` writes the seat's project
``.cursor/hooks.json`` (timeout 1800, explicit numeric loop_limit) with
explicit root and node.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.errors import ProtocolRefusal
from floati.mcp import run_cli_artifact
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


NODE = public_ids.builder("a")
CONV_A = "8f3c2b1a-4d5e-6f70-8192-a3b4c5d6e7f8"
CONV_B = "9a4d3c2b-5e6f-7081-92a3-b4c5d6e7f809"


DRAIN_MARKER = "Drain it now:"


def _drain_command(message: str) -> str:
    """The line the drain followup hands the seat, and nothing else.

    Am.2/N4: the command is a LINE, not a clause inside a sentence. This
    helper is deliberately the only extractor in the file so a future
    reshaping of the message reds one place rather than drifting silently.
    """

    after = message.split(DRAIN_MARKER, 1)[1]
    return after.split("\n", 1)[1].split("\n", 1)[0]


def _fallback_session_name() -> str:
    from floati.cursor_wait import _fallback_session

    return _fallback_session(NODE)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


class CursorWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register(NODE, "worker")
        self.ledger = self.root.path / "events.jsonl"
        self.ledger.write_text("", encoding="utf-8")
        self.journal = (
            self.root.path / "state" / "cursor-wait" / NODE / "journal.jsonl"
        )
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def _run(self, payload: dict, *, unread, clock=None, mtime_after=None):
        from floati.cursor_wait import run_cursor_stop_wait

        calls = {"n": 0}

        def peek() -> int:
            calls["n"] += 1
            if callable(unread):
                return unread(calls["n"])
            return int(unread)

        stdout = io.StringIO()
        clock = clock or _Clock()
        ledger_stat = {"mtime": 1.0}

        def ledger_mtime() -> float:
            if mtime_after is not None and clock.now >= mtime_after:
                return 2.0
            return ledger_stat["mtime"]

        code = run_cursor_stop_wait(
            root=self.root.path,
            node=NODE,
            runtime=self.runtime,
            deadline_seconds=15.0,
            poll_seconds=3.0,
            hook_timeout_seconds=1800.0,
            payload=payload,
            stdout=stdout,
            clock=clock.monotonic,
            sleep=clock.sleep,
            peek_unread=peek,
            ledger_mtime=ledger_mtime,
        )
        body = json.loads(stdout.getvalue() or "null")
        rows = []
        if self.journal.is_file():
            rows = [
                json.loads(line)
                for line in self.journal.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return code, body, rows

    def test_aborted_prints_empty_object(self) -> None:
        """Am.3: the first abort re-arms; {} is the second consecutive abort."""

        code, body, rows = self._run(
            {
                "status": "aborted",
                "loop_count": 0,
                "conversation_id": CONV_A,
            },
            unread=1,
        )
        self.assertEqual(0, code)
        self.assertNotEqual({}, body)
        self.assertEqual("followup_after_abort", rows[-1]["event"])

    def test_mail_prints_drain_followup_never_empty_object(self) -> None:
        code, body, rows = self._run({"status": "completed", "loop_count": 0}, unread=1)
        self.assertEqual(0, code)
        self.assertNotEqual({}, body)
        self.assertIn("followup_message", body)
        self.assertIn("Drain it now", body["followup_message"])
        self.assertIn(str(self.root.path), body["followup_message"])
        self.assertIn(NODE, body["followup_message"])
        self.assertEqual("followup_mail_at_start", rows[-1]["event"])

    def test_deadline_prints_rearm_followup_never_empty_object(self) -> None:
        code, body, rows = self._run(
            {"status": "completed", "loop_count": 0}, unread=0
        )
        self.assertEqual(0, code)
        self.assertNotEqual({}, body)
        self.assertIn("followup_message", body)
        self.assertIn("Reply exactly: armed.", body["followup_message"])
        self.assertNotEqual("{}", json.dumps(body))
        self.assertEqual("followup_rearm", rows[-1]["event"])

    def test_empty_inbox_is_silence_and_mail_after_wait_followups(self) -> None:
        def unread(n: int) -> int:
            return 1 if n >= 2 else 0

        clock = _Clock()
        code, body, rows = self._run(
            {"status": "completed", "loop_count": 0},
            unread=unread,
            clock=clock,
            mtime_after=3.0,
        )
        self.assertEqual(0, code)
        self.assertIn("Drain it now", body["followup_message"])
        self.assertEqual("followup_mail", rows[-1]["event"])
        self.assertGreater(rows[-1]["waited_seconds"], 0)

    def test_deadline_refused_unless_under_hook_timeout(self) -> None:
        from floati.cursor_wait import run_cursor_stop_wait

        with self.assertRaises(ProtocolRefusal) as raised:
            run_cursor_stop_wait(
                root=self.root.path,
                node=NODE,
                runtime=self.runtime,
                deadline_seconds=1800.0,
                poll_seconds=3.0,
                hook_timeout_seconds=1800.0,
                payload={"status": "completed"},
                stdout=io.StringIO(),
            )
        self.assertEqual("cursor_wait_deadline_invalid", raised.exception.code)


class CursorHookInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register(NODE, "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.runtime = self.base / "runtime"
        (self.runtime / "scripts").mkdir(parents=True)
        (self.runtime / "scripts" / "floati").write_text("#!/bin/sh\n", encoding="utf-8")

    def test_install_writes_project_hooks_json_with_explicit_root_and_node(self) -> None:
        code, artifact = run_cli_artifact(
            [
                "hook",
                "install",
                "--harness",
                "cursor",
                "--root",
                str(self.root.path),
                "--as",
                NODE,
                "--workspace",
                str(self.workspace),
                "--runtime",
                str(self.runtime),
            ]
        )
        self.assertEqual(0, code, artifact)
        self.assertEqual("ok", artifact["status"])
        hooks = json.loads(
            (self.workspace / ".cursor" / "hooks.json").read_text(encoding="utf-8")
        )
        stop = hooks["hooks"]["stop"]
        self.assertEqual(1, len(stop))
        self.assertEqual(1800, stop[0]["timeout"])
        # Am.1: this pinned None, and null is read by Cursor as absent, so
        # its documented default cap of 5 applied and a live seat went
        # dormant at loop_count 5. The pin now says the number.
        self.assertEqual(1000, stop[0]["loop_limit"])
        command = stop[0]["command"]
        self.assertIn(str(self.root.path), command)
        self.assertIn(NODE, command)
        self.assertIn("wake wait", command)
        self.assertIn("--harness cursor", command)

    def test_install_refuses_when_node_is_omitted(self) -> None:
        from floati.cli import _parser

        parser = _parser()
        with self.assertRaises(ProtocolRefusal) as raised:
            parser.parse_args(
                [
                    "hook",
                    "install",
                    "--harness",
                    "cursor",
                    "--root",
                    str(self.root.path),
                    "--workspace",
                    str(self.workspace),
                    "--runtime",
                    str(self.runtime),
                ]
            )
        self.assertEqual("arguments_invalid", raised.exception.code)


class CursorWaitAmendmentOneTests(unittest.TestCase):
    """CUR-2 Am.1 — the five clean-room gate findings, each constructed.

    Every case here builds a real root (or a deliberately broken one) and
    calls the waiter through its real inbox read, not an injected peek: the
    findings are all about what the waiter does when the *real* read fails,
    and an injected peek cannot see any of them.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register(NODE, "worker")
        (self.root.path / "events.jsonl").write_text("", encoding="utf-8")
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def _wait(self, **overrides):
        """Call the waiter for real and capture both channels.

        stderr is captured with ``redirect_stderr`` rather than passed as a
        keyword on purpose: the waiter must resolve ``sys.stderr`` when it
        writes, not bind it at import, and a test that passed a sink would
        RED on a missing parameter instead of on the defect it is here for.
        """

        from floati.cursor_wait import run_cursor_stop_wait

        stdout = io.StringIO()
        stderr = io.StringIO()
        arguments = dict(
            root=self.root.path,
            node=NODE,
            runtime=self.runtime,
            deadline_seconds=15.0,
            poll_seconds=3.0,
            hook_timeout_seconds=1800.0,
            payload={"status": "completed", "loop_count": 0},
            stdout=stdout,
        )
        arguments.update(overrides)
        with contextlib.redirect_stderr(stderr):
            code = run_cursor_stop_wait(**arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def _journal_rows(self, root: Path, node: str = NODE):
        path = root / "state" / "cursor-wait" / node / "journal.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # F1 — an unreadable inbox must never be `{}`.

    def test_f1_unregistered_node_gets_a_followup_naming_node_root_and_reason(
        self,
    ) -> None:
        code, out, _err = self._wait(node="ghost-node")
        body = json.loads(out or "null")
        self.assertEqual(0, code)
        self.assertNotEqual({}, body, "an unreadable inbox printed {} and the seat stops forever")
        message = body["followup_message"]
        self.assertIn("ghost-node", message)
        self.assertIn(str(self.root.path), message)
        self.assertIn("unknown_node", message)
        self.assertIn("stop", message)
        rows = self._journal_rows(self.root.path, "ghost-node")
        self.assertEqual("followup_inbox_unreadable", rows[-1]["event"])
        self.assertIn("unknown_node", rows[-1]["reason"])

    def test_f1_missing_root_gets_a_followup_naming_the_typed_reason(self) -> None:
        absent = self.base / "nope-root"
        code, out, _err = self._wait(root=absent)
        body = json.loads(out or "null")
        self.assertEqual(0, code)
        self.assertNotEqual({}, body, "a missing root printed {} and the seat stops forever")
        message = body["followup_message"]
        self.assertIn(str(absent), message)
        self.assertIn("direct_home_missing", message)

    def test_f1_empty_object_is_printed_for_aborted_and_for_nothing_else(self) -> None:
        """Am.3: {} is the second consecutive abort, not the first."""

        first = self._wait(
            payload={
                "status": "aborted",
                "loop_count": 0,
                "conversation_id": CONV_A,
            }
        )
        self.assertNotEqual({}, json.loads(first[1]))
        aborted = self._wait(
            payload={
                "status": "aborted",
                "loop_count": 1,
                "conversation_id": CONV_A,
            }
        )
        self.assertEqual({}, json.loads(aborted[1]))
        for case in (
            {"node": "ghost-node"},
            {"root": self.base / "nope-root"},
            {"root": self._regular_file_root()},
        ):
            with self.subTest(case=sorted(case)):
                body = json.loads(self._wait(**case)[1] or "null")
                self.assertNotEqual({}, body)
                self.assertIn("followup_message", body)

    # F2 — no exit path may leave stdout empty.

    def _regular_file_root(self) -> Path:
        path = self.base / "afile"
        if not path.exists():
            path.write_text("not a directory\n", encoding="utf-8")
        return path

    def _unwritable_journal_root(self) -> Path:
        """A REAL fleet root whose journal directory cannot be created.

        Am.2/N1 stops the waiter journaling under a root it cannot read, so
        a regular-file root no longer reaches the journal write at all and
        cannot construct F2 any more. The property F2 exists for — an OSError
        from the journal never suppresses the hook body, and names its cause
        on stderr — needs a root that IS readable and a journal path that is
        not writable, which is this: ``state/cursor-wait`` as a regular file.
        """

        (self.root.path / "state").mkdir(parents=True, exist_ok=True)
        blocker = self.root.path / "state" / "cursor-wait"
        if not blocker.exists():
            blocker.write_text("not a directory\n", encoding="utf-8")
        return self.root.path

    def test_f2_journal_failure_still_prints_a_hook_body_and_names_the_cause(
        self,
    ) -> None:
        code, out, err = self._wait(
            root=self._unwritable_journal_root(), node="ghost-node"
        )
        self.assertNotEqual("", out, "stdout was empty; Cursor reads that as {}")
        body = json.loads(out)
        self.assertIn("followup_message", body)
        self.assertEqual(0, code)
        self.assertIn("NotADirectoryError", err)
        self.assertIn("journal", err)

    # N1 — a refusal must not write into the root it has just refused to read.

    def test_n1_a_missing_root_is_never_created_by_the_refusal_path(self) -> None:
        absent = self.base / "nope-root"
        code, out, err = self._wait(root=absent)
        self.assertEqual(0, code)
        self.assertIn("direct_home_missing", json.loads(out)["followup_message"])
        self.assertFalse(
            absent.exists(),
            "the waiter created the root it had just reported missing",
        )
        lines = [line for line in err.splitlines() if line.strip()]
        self.assertEqual(
            1, len(lines), "the un-journaled row owes stderr exactly one line: %r" % err
        )
        self.assertIn("followup_inbox_unreadable", lines[0])

    def test_n1_the_typed_reason_is_stable_across_repeated_wakes(self) -> None:
        """The second wake on a broken configuration must not change its story."""

        absent = self.base / "nope-root"
        first = json.loads(self._wait(root=absent)[1])["followup_message"]
        second = json.loads(self._wait(root=absent)[1])["followup_message"]
        self.assertIn("direct_home_missing", first)
        self.assertEqual(
            first,
            second,
            "the second wake named a different cause than the first",
        )
        self.assertNotIn(
            "unknown_node",
            second,
            "the waiter's own journal turned a missing root into an unregistered node",
        )

    def test_n1_a_readable_root_is_still_journaled(self) -> None:
        """The control: N1 must not disarm the journal where it belongs."""

        self._wait(node="ghost-node")
        rows = self._journal_rows(self.root.path, "ghost-node")
        self.assertEqual("followup_inbox_unreadable", rows[-1]["event"])

    # F3 — the waiter must return before the hook timeout.

    def test_f3_admitted_deadline_reserves_the_poll_interval_and_a_margin(self) -> None:
        from floati.cursor_wait import run_cursor_stop_wait

        for deadline in (1799.5, 1800.0, 1797.0, 0.0, -1.0):
            with self.subTest(deadline=deadline):
                with self.assertRaises(ProtocolRefusal) as raised:
                    run_cursor_stop_wait(
                        root=self.root.path,
                        node=NODE,
                        runtime=self.runtime,
                        deadline_seconds=deadline,
                        poll_seconds=3.0,
                        hook_timeout_seconds=1800.0,
                        # aborted, so that a deadline this test expects to be
                        # REFUSED cannot instead be admitted and block the
                        # suite for half an hour: the guard runs first.
                        payload={"status": "aborted"},
                        stdout=io.StringIO(),
                    )
                self.assertEqual(
                    "cursor_wait_deadline_invalid", raised.exception.code
                )
        self.assertEqual(0, self._wait(deadline_seconds=1500.0, payload={"status": "aborted"})[0])

        from floati.cursor_wait import DEADLINE_MARGIN_SECONDS

        self.assertGreater(DEADLINE_MARGIN_SECONDS, 0)

    def test_f3_the_loop_sleep_never_overshoots_the_deadline(self) -> None:
        started = time.monotonic()
        code, out, _err = self._wait(deadline_seconds=5.0, poll_seconds=3.0)
        elapsed = time.monotonic() - started
        self.assertEqual(0, code)
        self.assertIn("Reply exactly: armed.", json.loads(out)["followup_message"])
        self.assertLessEqual(
            elapsed, 5.5, "a 5 s deadline returned at %.2f s" % elapsed
        )

    # F4 — the command the followup hands the seat must run verbatim.

    def test_f4_drain_command_runs_verbatim_and_acknowledges(self) -> None:
        sender = public_ids.builder("b")
        Registry(self.root).register(sender, "worker")
        code, artifact = run_cli_artifact(
            [
                "send",
                "--root", str(self.root.path),
                "--from", sender,
                "--to", NODE,
                "--repo", "floati",
                "--sha", "0" * 40,
                "--doc", "docs/evidence/cur-2-am1-2026-09-11.md",
                "--note", "CUR-2 Am.1 drain construction",
            ]
        )
        self.assertEqual(0, code, artifact)

        conversation = "8f3c2b1a-4d5e-6f70-8192-a3b4c5d6e7f8"
        _rc, out, _err = self._wait(
            runtime=REPOSITORY_ROOT,
            payload={"status": "completed", "loop_count": 0, "conversation_id": conversation},
        )
        message = json.loads(out)["followup_message"]
        self.assertIn("--session", message)
        self.assertIn(conversation, message)

        command = _drain_command(message)
        self.assertTrue(command.startswith("cd "))
        argv = shlex.split(command.split("&&", 1)[1])
        self.assertEqual("python3", argv[0])
        drained = subprocess.run(
            [sys.executable] + argv[1:],
            cwd=str(REPOSITORY_ROOT),
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            0,
            drained.returncode,
            "the command the followup hands the seat refused: " + drained.stdout,
        )
        decoded = json.loads(drained.stdout)
        self.assertEqual("ok", decoded["status"])
        self.assertIsNotNone(decoded["evidence"]["acknowledgment"])

    def test_n4_the_drain_command_is_a_line_by_itself_that_bash_runs(self) -> None:
        """N1/N4: an agent pastes a LINE, so the line must be the command.

        The Am.1 followup read ``Drain it now: cd … --session S (then ack …)``
        and an agent that pasted the line handed the shell a stray
        ``(then ack …)``. Asserted on the shape first, so this reds on the
        sentence rather than on an extractor raising.
        """

        sender = public_ids.builder("c")
        Registry(self.root).register(sender, "worker")
        code, artifact = run_cli_artifact(
            [
                "send",
                "--root", str(self.root.path),
                "--from", sender,
                "--to", NODE,
                "--repo", "floati",
                "--sha", "0" * 40,
                "--doc", "docs/evidence/cur-2-am1-2026-09-11.md",
                "--note", "CUR-2 Am.2 N4 construction",
            ]
        )
        self.assertEqual(0, code, artifact)

        _rc, out, _err = self._wait(runtime=REPOSITORY_ROOT)
        message = json.loads(out)["followup_message"]
        after = message.split(DRAIN_MARKER, 1)[1]
        self.assertTrue(
            after.startswith("\n"),
            "the command is still inside the sentence that announces it: %r" % message,
        )
        command = _drain_command(message)
        self.assertNotIn(
            "(then",
            command,
            "pasting the line hands the shell a stray parenthetical: %r" % command,
        )
        self.assertTrue(command.endswith(_fallback_session_name()), command)

        run = subprocess.run(
            ["bash", "-c", command],
            cwd=str(REPOSITORY_ROOT),
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            0,
            run.returncode,
            "the pasted line refused: " + run.stdout + run.stderr,
        )
        self.assertEqual("ok", json.loads(run.stdout)["status"])

    def test_f4_absent_conversation_id_falls_back_to_a_stable_per_node_session(
        self,
    ) -> None:
        from floati.cursor_wait import _drain_session

        first = _drain_session({"status": "completed"}, NODE)
        second = _drain_session({"conversation_id": 17}, NODE)
        self.assertEqual(first, second)
        self.assertIn(NODE, first)
        from floati.wake_control import validate_session_id

        self.assertEqual(first, validate_session_id(first))

    # The larger finding — Cursor's auto-follow-up cap.

    def test_loop_limit_is_an_explicit_number_in_the_installed_hook(self) -> None:
        runtime = self.base / "runtime-with-launcher"
        (runtime / "scripts").mkdir(parents=True)
        (runtime / "scripts" / "floati").write_text("#!/bin/sh\n", encoding="utf-8")
        code, artifact = run_cli_artifact(
            [
                "hook", "install",
                "--harness", "cursor",
                "--root", str(self.root.path),
                "--as", NODE,
                "--workspace", str(self.workspace),
                "--runtime", str(runtime),
            ]
        )
        self.assertEqual(0, code, artifact)
        hooks = json.loads(
            (self.workspace / ".cursor" / "hooks.json").read_text(encoding="utf-8")
        )
        limit = hooks["hooks"]["stop"][0]["loop_limit"]
        self.assertIsInstance(
            limit, int, "loop_limit null is read as absent and Cursor applies its default cap of 5"
        )

        from floati.cursor_wait import CURSOR_LOOP_LIMIT

        self.assertEqual(CURSOR_LOOP_LIMIT, limit)
        self.assertGreater(limit, 5, "Cursor's documented default cap is 5 follow-ups")
        self.assertEqual(CURSOR_LOOP_LIMIT, artifact["evidence"]["loop_limit"])

    def test_aborted_at_the_loop_limit_is_named_on_stderr_and_journaled(self) -> None:
        from floati.cursor_wait import CURSOR_LOOP_LIMIT

        code, out, err = self._wait(
            payload={"status": "aborted", "loop_count": 5},
            loop_limit=5,
        )
        self.assertEqual(0, code)
        self.assertEqual({}, json.loads(out), "aborted must still be {}; never fight a Ctrl-C")
        self.assertIn("dormant", err)
        self.assertIn("5", err)
        rows = self._journal_rows(self.root.path)
        self.assertEqual(5, rows[-1]["loop_count"])
        self.assertEqual(5, rows[-1]["loop_limit"])

        _code, _out, quiet = self._wait(
            payload={"status": "aborted", "loop_count": 1},
            loop_limit=CURSOR_LOOP_LIMIT,
        )
        self.assertEqual("", quiet, "a human Ctrl-C must not be reported as the cap")

    # F5 — one documented channel policy for configuration-time refusals.

    def test_f5_configuration_refusal_writes_one_line_to_stderr(self) -> None:
        from floati.help_copy import HELP_COPY

        notes = " ".join(HELP_COPY["wake wait"]["notes"])
        self.assertIn("stderr", notes)

        completed = subprocess.run(
            [
                sys.executable, "-m", "floati", "wake", "wait",
                "--harness", "cursor",
                "--root", str(self.root.path),
                "--as", NODE,
                "--runtime", str(self.runtime),
                "--deadline-seconds", "1799.5",
            ],
            cwd=str(REPOSITORY_ROOT),
            input='{"status":"completed","loop_count":0}',
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(20, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("cursor_wait_deadline_invalid", completed.stdout)
        self.assertIn("cursor_wait_deadline_invalid", completed.stderr)

    def test_module_docstring_states_the_real_exit_count(self) -> None:
        from floati import cursor_wait

        self.assertNotIn("Four exits", cursor_wait.__doc__ or "")
        self.assertIn("Five exits", cursor_wait.__doc__ or "")
        self.assertIn(
            "if a person stopped this turn, reply exactly: stop",
            cursor_wait.__doc__ or "",
        )


HUMAN_ESCAPE = "if a person stopped this turn, reply exactly: stop"
SCOPE_NOTICE_TEMPLATE = (
    "this hook is scoped to {workspace}; re-rooting the chat leaves it behind "
    "— work other directories by absolute path"
)
QUIRK_MOVE_ROOT = (
    "Cursor's `move_agent_to_root` aborts the running turn and is delivered "
    "to the stop hook as `status: aborted`; a project-scoped hook does not "
    "follow the chat to the new root"
)


class CursorWaitAmendmentThreeTests(CursorWaitTests):
    """CUR-2 Am.3 — four fixture payloads against floati/cursor_wait.py."""

    def test_error_status_rearms_with_drain_followup(self) -> None:
        payload = {
            "status": "error",
            "loop_count": 0,
            "conversation_id": CONV_A,
        }
        code, body, rows = self._run(payload, unread=1)
        self.assertEqual(0, code)
        self.assertNotEqual({}, body)
        self.assertIn("followup_message", body)
        self.assertIn("Drain it now", body["followup_message"])
        self.assertEqual("followup_after_error", rows[-1]["event"])
        self.assertEqual(CONV_A, rows[-1]["conversation_id"])
        self.assertEqual(sorted(payload), rows[-1]["payload_keys"])

    def test_first_abort_rearms_with_human_escape(self) -> None:
        payload = {
            "status": "aborted",
            "loop_count": 0,
            "conversation_id": CONV_A,
        }
        code, body, rows = self._run(payload, unread=1)
        self.assertEqual(0, code)
        self.assertNotEqual({}, body)
        self.assertIn("followup_message", body)
        self.assertIn(HUMAN_ESCAPE, body["followup_message"])
        self.assertEqual("followup_after_abort", rows[-1]["event"])
        self.assertEqual(CONV_A, rows[-1]["conversation_id"])
        self.assertEqual(sorted(payload), rows[-1]["payload_keys"])

    def test_second_consecutive_abort_same_conversation_yields_empty_object(
        self,
    ) -> None:
        first = {
            "status": "aborted",
            "loop_count": 0,
            "conversation_id": CONV_A,
        }
        second = {
            "status": "aborted",
            "loop_count": 1,
            "conversation_id": CONV_A,
        }
        self._run(first, unread=0)
        code, body, rows = self._run(second, unread=0)
        self.assertEqual(0, code)
        self.assertEqual({}, body)
        self.assertEqual("exit_empty_aborted_twice", rows[-1]["event"])
        self.assertEqual(CONV_A, rows[-1]["conversation_id"])
        self.assertEqual(sorted(second), rows[-1]["payload_keys"])

    def test_abort_in_a_new_conversation_resets_consecutive_memory(self) -> None:
        self._run(
            {
                "status": "aborted",
                "loop_count": 0,
                "conversation_id": CONV_A,
            },
            unread=0,
        )
        payload = {
            "status": "aborted",
            "loop_count": 0,
            "conversation_id": CONV_B,
        }
        code, body, rows = self._run(payload, unread=0)
        self.assertEqual(0, code)
        self.assertNotEqual({}, body)
        self.assertIn(HUMAN_ESCAPE, body["followup_message"])
        self.assertEqual("followup_after_abort", rows[-1]["event"])
        self.assertEqual(CONV_B, rows[-1]["conversation_id"])


class CursorHookInstallAmendmentThreeTests(CursorHookInstallTests):
    def test_install_prints_the_scope_line_after_writing(self) -> None:
        code, artifact = run_cli_artifact(
            [
                "hook",
                "install",
                "--harness",
                "cursor",
                "--root",
                str(self.root.path),
                "--as",
                NODE,
                "--workspace",
                str(self.workspace),
                "--runtime",
                str(self.runtime),
            ]
        )
        self.assertEqual(0, code, artifact)
        expected = SCOPE_NOTICE_TEMPLATE.format(workspace=self.workspace)
        self.assertEqual(expected, artifact["evidence"]["scope_notice"])


class CursorDoctorAmendmentThreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register(NODE, "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def test_doctor_names_a_cursor_seat_when_hook_workspace_differs_from_journal_root(
        self,
    ) -> None:
        from floati.doctor import project_cursor_hook_workspace_findings

        node_dir = self.root.path / "state" / "cursor-wait" / NODE
        node_dir.mkdir(parents=True)
        (node_dir / "installed-hook.json").write_text(
            json.dumps(
                {
                    "workspace": str(self.workspace),
                    "root": str(self.root.path),
                    "node": NODE,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        seen = self.base / "other-root"
        seen.mkdir()
        (node_dir / "journal.jsonl").write_text(
            json.dumps(
                {
                    "event": "followup_after_abort",
                    "node": NODE,
                    "last_seen_root": str(seen),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        findings = project_cursor_hook_workspace_findings(self.root)
        self.assertEqual(1, len(findings), findings)
        self.assertEqual("cursor_hook_workspace_diverged", findings[0]["code"])
        self.assertIn(NODE, findings[0]["detail"])
        self.assertIn(str(self.workspace), findings[0]["detail"])
        self.assertIn(str(seen), findings[0]["detail"])

    def test_doctor_is_silent_when_it_cannot_tell(self) -> None:
        from floati.doctor import project_cursor_hook_workspace_findings

        self.assertEqual([], project_cursor_hook_workspace_findings(self.root))

    def test_readme_and_quirks_carry_the_authored_cursor_sentences(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        # The README sentence is copy (the architect's) and may not be pinned verbatim;
        # this arm pins the guard's three identifiers: the hook is workspace-scoped,
        # re-rooting loses it, absolute paths are the remedy. The doctor line keeps
        # SCOPE_NOTICE_TEMPLATE verbatim in its own arm above.
        items = [item for item in readme.split("\n- ") if "re-rooted" in item]
        self.assertEqual(1, len(items), "exactly one README list item names re-rooting")
        self.assertIn("scoped to the workspace", items[0])
        self.assertIn("absolute path", items[0])
        quirks = (REPOSITORY_ROOT / "docs/evidence/gauntlet/QUIRKS.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Q-cursor-move-root", quirks)
        self.assertIn(QUIRK_MOVE_ROOT, quirks)
        self.assertIn("02:54", quirks)
        self.assertIn("13:42", quirks)


if __name__ == "__main__":
    unittest.main()
